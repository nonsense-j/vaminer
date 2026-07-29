"""Least-privilege stdio MCP server for Claude Code phase tools."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ..core.validation import (
    aggregate_anchor_synthesis_runs,
    validate_anchor_synthesis_request,
    validate_anchor_synthesis_run,
)
from ..tools.ast_grep import run_ast_grep_query
from ..tools.cases import (
    list_case_artifacts as list_case_artifacts_plain,
)
from ..tools.cases import (
    read_case_artifact as read_case_artifact_plain,
)
from ..tools.cases import (
    write_case_artifact as write_case_artifact_plain,
)
from ..tools.cve import fetch_cve as fetch_cve_plain
from ..tools.github import (
    fetch_github_issue as fetch_github_issue_plain,
)
from ..tools.github import (
    parse_commit as parse_commit_plain,
)
from ..tools.github import (
    search_commit_by_tag as search_commit_by_tag_plain,
)
from ..tools.github import (
    search_commit_by_time as search_commit_by_time_plain,
)
from ..tools.repo import (
    clone_repository,
    read_fixed_diff_from_repo,
    read_repository_file,
    search_repository_files,
)
from ..tools.skills import (
    list_skill_resources as list_skill_resources_plain,
)
from ..tools.skills import (
    read_skill_resource as read_skill_resource_plain,
)
from ..utils.models import (
    AnalysisSubject,
    AnchorSynthesisRequest,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
    RootCauseAnalysis,
)

SERVER_NAME = "vaminer"
PROFILE_ENV = "VAMINER_MCP_PROFILE"
WORKSPACE_ROOT_ENV = "VAMINER_MCP_WORKSPACE_ROOT"
REPO_PATH_ENV = "VAMINER_MCP_REPO_PATH"
SOURCE_ROOT_ENV = "VAMINER_MCP_SOURCE_ROOT"
CASES_DIR_ENV = "VAMINER_MCP_CASES_DIR"
FIXED_DIFF_ENV = "VAMINER_MCP_FIXED_DIFF_ENABLED"
TASK_STATE_ENV = "VAMINER_MCP_TASK_STATE"
BATCH_RESULT_ENV = "VAMINER_MCP_BATCH_RESULT"
GITHUB_MIRROR_ENV = "VAMINER_MCP_GITHUB_MIRROR_ENABLED"
SKILL_ROOTS_ENV = "VAMINER_MCP_SKILL_ROOTS_JSON"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class MCPProfile(StrEnum):
    """Tool exposure profiles selected once for one phase subprocess."""

    ISSUE = "issue"
    ROOT_CAUSE = "root_cause"
    RULE_GENERATION = "rule_generation"

    @classmethod
    def parse(cls, value: str) -> MCPProfile:
        aliases = {
            "issue": cls.ISSUE,
            "issue_collection": cls.ISSUE,
            "root_cause": cls.ROOT_CAUSE,
            "rule": cls.RULE_GENERATION,
            "rule_generation": cls.RULE_GENERATION,
        }
        try:
            return aliases[value.strip().lower()]
        except KeyError as exc:
            allowed = ", ".join(profile.value for profile in cls)
            raise ValueError(f"unsupported MCP profile {value!r}; expected one of: {allowed}") from exc


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable is missing: {name}")
    return value


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _existing_directory(value: str, *, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label} is not an existing directory: {path}")
    return path


def _scoped_directory(value: str, *, label: str, workspace_root: Path) -> Path:
    path = _existing_directory(value, label=label)
    try:
        path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the active workspace: {path}") from exc
    return path


def _parse_skill_roots(value: str | None) -> Mapping[str, Path]:
    if value is None or not value.strip():
        return MappingProxyType({})
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{SKILL_ROOTS_ENV} must be valid JSON") from exc
    if not isinstance(decoded, dict) or len(decoded) > 8:
        raise ValueError(f"{SKILL_ROOTS_ENV} must be an object with at most 8 skills")
    roots: dict[str, Path] = {}
    for name, raw_path in decoded.items():
        if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid task skill name: {name!r}")
        if not isinstance(raw_path, str):
            raise TypeError(f"skill root for {name!r} must be a string path")
        source = Path(raw_path).expanduser()
        if source.is_symlink():
            raise ValueError(f"skill root must not be a symbolic link: {name!r}")
        root = source.resolve()
        if not root.is_dir() or not (root / "SKILL.md").is_file():
            raise ValueError(f"skill root must contain SKILL.md: {name!r}")
        roots[name] = root
    return MappingProxyType(roots)


@dataclass(frozen=True)
class MCPServerSettings:
    """Validated environment-derived configuration for one MCP subprocess."""

    profile: MCPProfile
    workspace_root: Path
    source_root: Path | None = None
    repo_path: Path | None = None
    cases_dir: Path | None = None
    fixed_diff_enabled: bool = False
    task_state_path: Path | None = None
    batch_result_path: Path | None = None
    github_mirror_enabled: bool = True
    skill_roots: Mapping[str, Path] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MCPServerSettings:
        values = os.environ if env is None else env
        profile = MCPProfile.parse(_required_env(values, PROFILE_ENV))
        workspace_root = _existing_directory(
            _required_env(values, WORKSPACE_ROOT_ENV),
            label="workspace root",
        )
        source_root: Path | None = None
        repo_path: Path | None = None
        cases_dir: Path | None = None
        fixed_diff_enabled = _parse_bool(values.get(FIXED_DIFF_ENV), default=False)
        task_state_path: Path | None = None
        batch_result_path: Path | None = None

        if profile is MCPProfile.ROOT_CAUSE:
            source_root = _scoped_directory(
                _required_env(values, SOURCE_ROOT_ENV),
                label="source root",
                workspace_root=workspace_root,
            )
            cases_dir = _scoped_directory(
                _required_env(values, CASES_DIR_ENV),
                label="cases directory",
                workspace_root=workspace_root,
            )
            if fixed_diff_enabled:
                repo_path = _scoped_directory(
                    _required_env(values, REPO_PATH_ENV),
                    label="repository path",
                    workspace_root=workspace_root,
                )
        elif profile is MCPProfile.RULE_GENERATION:
            source_root = _scoped_directory(
                _required_env(values, SOURCE_ROOT_ENV),
                label="source root",
                workspace_root=workspace_root,
            )
            cases_dir = _scoped_directory(
                _required_env(values, CASES_DIR_ENV),
                label="cases directory",
                workspace_root=workspace_root,
            )
            raw_state = Path(_required_env(values, TASK_STATE_ENV)).expanduser().resolve()
            if not raw_state.is_file():
                raise ValueError(f"task state is not an existing file: {raw_state}")
            task_state_path = raw_state
            raw_result = Path(_required_env(values, BATCH_RESULT_ENV)).expanduser().resolve()
            if not raw_result.parent.is_dir() or raw_result.is_symlink():
                raise ValueError("batch result parent must be an existing non-symlink directory")
            batch_result_path = raw_result

        return cls(
            profile=profile,
            workspace_root=workspace_root,
            source_root=source_root,
            repo_path=repo_path,
            cases_dir=cases_dir,
            fixed_diff_enabled=fixed_diff_enabled,
            task_state_path=task_state_path,
            batch_result_path=batch_result_path,
            github_mirror_enabled=_parse_bool(values.get(GITHUB_MIRROR_ENV), default=True),
            skill_roots=_parse_skill_roots(values.get(SKILL_ROOTS_ENV)),
        )


def _load_mcp_factory() -> Callable[[str], Any]:
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "Claude MCP support requires the optional dependency `mcp[cli]`"
        ) from exc


def _json_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _register_tool(server: Any, name: str, function: Callable[..., Any]) -> None:
    server.tool(name=name)(function)


def _register_issue_tools(server: Any, settings: MCPServerSettings) -> None:
    def fetch_cve(cve_id: str) -> dict[str, Any]:
        """Fetch normalized CVE evidence from deterministic external sources."""
        return _json_value(fetch_cve_plain(cve_id))

    def fetch_github_issue(issue_url: str, fetch_extra_notes: bool = False) -> dict[str, Any]:
        """Fetch one GitHub issue and its directly linked evidence."""
        return _json_value(fetch_github_issue_plain(issue_url, fetch_extra_notes))

    def parse_commit(commit_url: str) -> dict[str, Any]:
        """Fetch normalized metadata for one GitHub commit URL."""
        return _json_value(parse_commit_plain(commit_url))

    def clone_repo(
        repo_url: str,
        buggy_sha: str,
        fixed_sha: str | None = None,
    ) -> dict[str, Any]:
        """Prepare the task-owned buggy checkout and optional fixed branch."""
        return _json_value(
            clone_repository(
                settings.workspace_root,
                repo_url,
                buggy_sha,
                fixed_sha,
                github_mirror_enabled=settings.github_mirror_enabled,
            )
        )

    def search_commit_by_tag(
        owner: str,
        repo: str,
        tag_prefix: str,
    ) -> list[dict[str, Any]] | str:
        """Search commits by the narrowest evidence-supported tag prefix."""
        return _json_value(search_commit_by_tag_plain(owner, repo, tag_prefix))

    def search_commit_by_time(
        owner: str,
        repo: str,
        since: str,
        until: str,
    ) -> list[dict[str, Any]] | str:
        """Search commits in a narrow ISO-8601 time range as a last resort."""
        return _json_value(search_commit_by_time_plain(owner, repo, since, until))

    _register_tool(server, "fetch_cve", fetch_cve)
    _register_tool(server, "fetch_github_issue", fetch_github_issue)
    _register_tool(server, "parse_commit", parse_commit)
    _register_tool(server, "clone_repo", clone_repo)
    _register_tool(server, "search_commit_by_tag", search_commit_by_tag)
    _register_tool(server, "search_commit_by_time", search_commit_by_time)


def _register_root_cause_tools(server: Any, settings: MCPServerSettings) -> None:
    assert settings.source_root is not None
    assert settings.cases_dir is not None

    def read_source_file(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Read at most 200 lines from one source-root-relative file."""
        return read_repository_file(
            settings.source_root,
            path,
            start_line=start_line,
            end_line=end_line,
        )

    def search_source_files(
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Search the authoritative source tree with bounded regex results."""
        return search_repository_files(
            settings.source_root,
            pattern,
            path=path,
            max_results=max_results,
        )

    def list_case_artifacts() -> list[str]:
        """List valid case artifacts in deterministic filename order."""
        return list_case_artifacts_plain(settings.cases_dir)

    def read_case_artifact(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Read at most 200 lines from one top-level case artifact."""
        return read_case_artifact_plain(
            settings.cases_dir,
            path,
            start_line=start_line,
            end_line=end_line,
        )

    def write_case_artifact(path: str, content: str) -> dict[str, Any]:
        """Atomically write one bounded caseN or caseN_varM artifact."""
        return write_case_artifact_plain(settings.cases_dir, path, content)

    _register_tool(server, "read_source_file", read_source_file)
    _register_tool(server, "search_source_files", search_source_files)
    _register_tool(server, "list_case_artifacts", list_case_artifacts)
    _register_tool(server, "read_case_artifact", read_case_artifact)
    _register_tool(server, "write_case_artifact", write_case_artifact)
    if settings.fixed_diff_enabled:
        assert settings.repo_path is not None

        def read_fixed_diff(path: str | None = None) -> str:
            """Read the verified buggy-to-fixed diff, optionally for one path."""
            assert settings.repo_path is not None
            return read_fixed_diff_from_repo(settings.repo_path, path)

        _register_tool(server, "read_fixed_diff", read_fixed_diff)


class _RuleTaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: RootCauseAnalysis
    analysis_subject: AnalysisSubject


@dataclass
class _AnchorBatchGate:
    """Invocation-local immutable-intent submission and finalization gate."""

    settings: MCPServerSettings
    state: _RuleTaskState
    lock: Lock = field(default_factory=Lock)
    requests: dict[str, AnchorSynthesisRunRequest] = field(default_factory=dict)
    results: dict[str, AnchorSynthesisRunResult] = field(default_factory=dict)
    finalized: bool = False

    def submit(
        self,
        run_request: AnchorSynthesisRunRequest,
        result: AnchorSynthesisRunResult,
    ) -> dict[str, Any]:
        assert self.settings.source_root is not None
        assert self.settings.cases_dir is not None
        if run_request.root_cause != self.state.root_cause:
            raise ValueError(
                "anchor run root_cause differs from the immutable task RCA; "
                "retry with this exact root_cause JSON: "
                + self.state.root_cause.model_dump_json()
            )
        errors = validate_anchor_synthesis_run(
            result,
            source_root=self.settings.source_root,
            cases_dir=self.settings.cases_dir,
            root_cause=self.state.root_cause,
            run_request=run_request,
            analysis_subject=self.state.analysis_subject,
        )
        if errors:
            raise ValueError("anchor synthesis submission failed:\n- " + "\n- ".join(errors))
        anchor_id = run_request.anchor_intent.id
        with self.lock:
            if self.finalized:
                raise ValueError("anchor synthesis batch is already finalized")
            if anchor_id in self.results:
                raise ValueError(f"anchor intent {anchor_id!r} was submitted more than once")
            self.requests[anchor_id] = run_request
            self.results[anchor_id] = result
        return {
            "status": "validated",
            "intent_id": anchor_id,
            "anchor": result.anchor.model_dump(mode="json", by_alias=True),
        }

    def finalize(self, request: AnchorSynthesisRequest) -> dict[str, Any]:
        assert self.settings.source_root is not None
        assert self.settings.cases_dir is not None
        errors = validate_anchor_synthesis_request(request, root_cause=self.state.root_cause)
        if errors:
            raise ValueError("anchor synthesis request failed:\n- " + "\n- ".join(errors))
        intent_ids = [intent.id for intent in request.anchor_intents]
        with self.lock:
            if self.finalized:
                raise ValueError("anchor synthesis batch was finalized more than once")
            submitted_ids = set(self.results)
            expected_ids = set(intent_ids)
            if submitted_ids != expected_ids:
                missing = sorted(expected_ids - submitted_ids)
                unexpected = sorted(submitted_ids - expected_ids)
                details = []
                if missing:
                    details.append("missing: " + ", ".join(missing))
                if unexpected:
                    details.append("unexpected: " + ", ".join(unexpected))
                raise ValueError("anchor synthesis submissions are incomplete (" + "; ".join(details) + ")")
            final_requests = {
                intent.id: AnchorSynthesisRunRequest(
                    root_cause=request.root_cause,
                    summary=request.summary,
                    anchor_intent=intent,
                )
                for intent in request.anchor_intents
            }
            changed = [
                anchor_id
                for anchor_id in intent_ids
                if self.requests[anchor_id] != final_requests[anchor_id]
            ]
            if changed:
                exact_request = AnchorSynthesisRequest(
                    root_cause=self.state.root_cause,
                    summary=self.requests[intent_ids[0]].summary,
                    anchor_intents=[
                        self.requests[anchor_id].anchor_intent
                        for anchor_id in intent_ids
                    ],
                )
                raise ValueError(
                    "finalization changed immutable submitted intents: "
                    + ", ".join(changed)
                    + "; retry with this exact request JSON: "
                    + exact_request.model_dump_json()
                )
            ordered_results = [self.results[anchor_id] for anchor_id in intent_ids]
            synthesis = aggregate_anchor_synthesis_runs(
                request,
                ordered_results,
                source_root=self.settings.source_root,
                cases_dir=self.settings.cases_dir,
                root_cause=self.state.root_cause,
                analysis_subject=self.state.analysis_subject,
            )
            self.finalized = True
            assert self.settings.batch_result_path is not None
            result_path = self.settings.batch_result_path
            temporary = result_path.with_name(f".{result_path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                synthesis.model_dump_json(by_alias=True),
                encoding="utf-8",
            )
            os.replace(temporary, result_path)
        return synthesis.model_dump(mode="json", by_alias=True)


def _load_rule_task_state(settings: MCPServerSettings) -> _RuleTaskState:
    assert settings.task_state_path is not None
    try:
        return _RuleTaskState.model_validate_json(
            settings.task_state_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid immutable Rule Generator task state: {exc}") from exc


def _register_rule_generation_tools(server: Any, settings: MCPServerSettings) -> None:
    assert settings.source_root is not None
    assert settings.cases_dir is not None
    gate = _AnchorBatchGate(settings=settings, state=_load_rule_task_state(settings))

    def list_case_artifacts() -> list[str]:
        """List generated cases in deterministic filename order."""
        return list_case_artifacts_plain(settings.cases_dir)

    def read_case_artifact(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Read at most 200 lines from one top-level case artifact."""
        return read_case_artifact_plain(
            settings.cases_dir,
            path,
            start_line=start_line,
            end_line=end_line,
        )

    def read_source_file(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Read at most 200 lines from one source-root-relative file."""
        return read_repository_file(
            settings.source_root,
            path,
            start_line=start_line,
            end_line=end_line,
        )

    def search_source_files(
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Search the task source tree with bounded regex results."""
        return search_repository_files(
            settings.source_root,
            pattern,
            path=path,
            max_results=max_results,
        )

    def list_skill_resources(skill_name: str) -> dict[str, Any]:
        """List bounded files from one skill explicitly attached to this task."""
        return list_skill_resources_plain(settings.skill_roots, skill_name)

    def read_skill_resource(
        skill_name: str,
        resource: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Read at most 200 lines from one task-declared skill file."""
        return read_skill_resource_plain(
            settings.skill_roots,
            skill_name,
            resource,
            start_line=start_line,
            end_line=end_line,
        )

    def run_ast_grep(
        target: Literal["source", "cases"],
        query_type: Literal["pattern", "rule"],
        query: str,
        output: Literal["count", "sample", "full"] = "sample",
        sample_size: int | None = None,
    ) -> dict[str, Any]:
        """Run a bounded ast-grep query against an authorized task root."""
        target_dir = settings.source_root if target == "source" else settings.cases_dir
        return run_ast_grep_query(
            settings.workspace_root,
            str(target_dir),
            language=gate.state.root_cause.language.value,
            query_type=query_type,
            query=query,
            output=output,
            sample_size=sample_size,
        )

    def submit_anchor_synthesis_run(
        run_request: AnchorSynthesisRunRequest,
        result: AnchorSynthesisRunResult,
    ) -> dict[str, Any]:
        """Validate and record exactly one result for one immutable intent."""
        return gate.submit(run_request, result)

    def finalize_anchor_synthesis_batch(
        request: AnchorSynthesisRequest,
    ) -> dict[str, Any]:
        """Require one validated run per intent and deterministically finalize it."""
        return gate.finalize(request)

    _register_tool(server, "list_case_artifacts", list_case_artifacts)
    _register_tool(server, "read_case_artifact", read_case_artifact)
    _register_tool(server, "read_source_file", read_source_file)
    _register_tool(server, "search_source_files", search_source_files)
    _register_tool(server, "list_skill_resources", list_skill_resources)
    _register_tool(server, "read_skill_resource", read_skill_resource)
    _register_tool(server, "run_ast_grep", run_ast_grep)
    _register_tool(server, "submit_anchor_synthesis_run", submit_anchor_synthesis_run)
    _register_tool(server, "finalize_anchor_synthesis_batch", finalize_anchor_synthesis_batch)


def build_server(
    *,
    settings: MCPServerSettings | None = None,
    env: Mapping[str, str] | None = None,
    fast_mcp_factory: Callable[[str], Any] | None = None,
) -> Any:
    """Build one ``vaminer`` server exposing only the selected profile tools."""
    resolved_settings = settings or MCPServerSettings.from_env(env)
    factory = fast_mcp_factory or _load_mcp_factory()
    server = factory(SERVER_NAME)
    {
        MCPProfile.ISSUE: _register_issue_tools,
        MCPProfile.ROOT_CAUSE: _register_root_cause_tools,
        MCPProfile.RULE_GENERATION: _register_rule_generation_tools,
    }[resolved_settings.profile](server, resolved_settings)
    return server


def main() -> None:
    """Run the selected MCP profile over stdio."""
    try:
        server = build_server()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"VAMiner MCP configuration error: {exc}") from exc
    server.run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = [
    "BATCH_RESULT_ENV",
    "CASES_DIR_ENV",
    "FIXED_DIFF_ENV",
    "GITHUB_MIRROR_ENV",
    "PROFILE_ENV",
    "REPO_PATH_ENV",
    "SERVER_NAME",
    "SKILL_ROOTS_ENV",
    "SOURCE_ROOT_ENV",
    "TASK_STATE_ENV",
    "WORKSPACE_ROOT_ENV",
    "MCPProfile",
    "MCPServerSettings",
    "build_server",
    "main",
]
