"""Closed Phase Authority and task construction for Miner Agents."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from ..agent.contracts import (
    AgentPhase,
    AgentTask,
    AnchorSynthesisAuthority,
    IssueCollectionAuthority,
    PhaseDefinition,
    RootCauseAuthority,
    RuleGenerationAuthority,
    RunLimits,
)
from ..models.analysis import GroundingPolicy, RootCauseAnalysis
from ..models.anchors import (
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
)
from ..models.issue import IssueCollectionInfo
from ..models.vas import VASCoreInfo
from ..utils.config import (
    MINER_MAX_TURNS_ISSUE_COLLECTION,
    MINER_MAX_TURNS_PER_ANCHOR,
    MINER_MAX_TURNS_ROOT_CAUSE,
    MINER_MAX_TURNS_RULE_GENERATION,
)
from .examples import ExampleSuiteIntake
from .validation.analysis import validate_issue_checkout, validate_root_cause_analysis
from .validation.vas import validate_vas_core

_MINER_DIR = Path(__file__).resolve().parents[1]
_INSTRUCTIONS_DIR = _MINER_DIR / "instructions"
AST_GREP_SKILL_ROOT = _MINER_DIR / "skills" / "ast-grep"


def _instructions(name: str) -> str:
    return (_INSTRUCTIONS_DIR / name).read_text(encoding="utf-8")


def _validate_issue_collection(
    value: IssueCollectionInfo,
    authority: object,
    workspace_root: Path,
) -> list[str]:
    if not isinstance(authority, IssueCollectionAuthority):  # pragma: no cover - AgentTask enforces this.
        return ["Issue Collection output received the wrong Phase Authority"]
    return validate_issue_checkout(value, workspace_root=workspace_root)


def _validate_root_cause(
    value: RootCauseAnalysis,
    authority: object,
    _workspace_root: Path,
) -> list[str]:
    if not isinstance(authority, RootCauseAuthority):  # pragma: no cover - AgentTask enforces this.
        return ["RCA output received the wrong Phase Authority"]
    return validate_root_cause_analysis(
        value,
        source_root=authority.source_root,
        cases_dir=authority.cases_dir,
    )


def _validate_rule_generation(
    value: VASCoreInfo,
    authority: object,
    _workspace_root: Path,
) -> list[str]:
    if not isinstance(authority, RuleGenerationAuthority):  # pragma: no cover - AgentTask enforces this.
        return ["Rule Generation output received the wrong Phase Authority"]
    return validate_vas_core(
        value,
        source_root=authority.source_root,
        cases_dir=authority.cases_dir,
        root_cause=authority.root_cause,
        grounding_policy=authority.grounding_policy,
    )


def _validate_anchor_synthesis(
    value: AnchorSynthesisDelta,
    authority: object,
    _workspace_root: Path,
) -> tuple[str, ...]:
    if not isinstance(authority, AnchorSynthesisAuthority):  # pragma: no cover - AgentTask enforces this.
        return ("Anchor Synthesis output received the wrong Phase Authority",)
    intent = next(
        (item for item in authority.plan.intents if item.id == authority.target_anchor_id),
        None,
    )
    if intent is None:  # pragma: no cover - task factory constructs both together.
        return (f"unknown target intent {authority.target_anchor_id!r}",)
    if value.target_anchor_id != intent.id or value.query_weight > intent.behavior_weight:
        return (f"synthesis output drifted from target intent {intent.id!r}",)
    return ()


ISSUE_COLLECTION = PhaseDefinition(
    phase=AgentPhase.ISSUE_COLLECTION,
    agent_name="Issue Collector",
    description="Collect issue evidence, resolve revisions, and prepare one verified checkout.",
    instructions=_instructions("issue_collector.md"),
    output_type=IssueCollectionInfo,
    tools=(
        "fetch_cve",
        "fetch_github_issue",
        "parse_commit",
        "clone_repo",
        "search_commit_by_tag",
        "search_commit_by_time",
        "web_search",
        "web_fetch",
    ),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_ISSUE_COLLECTION, output_retries=2),
    validator=_validate_issue_collection,
)

ROOT_CAUSE = PhaseDefinition(
    phase=AgentPhase.ROOT_CAUSE,
    agent_name="Root Cause Analyzer",
    description="Establish the causal chain and write the declared Case Artifacts.",
    instructions=_instructions("root_cause_analyzer.md"),
    output_type=RootCauseAnalysis,
    tools=(
        "list_src_files",
        "search_src_files",
        "read_src_file",
        "list_case_artifacts",
        "read_case_artifact",
        "write_case_artifact",
    ),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_ROOT_CAUSE, output_retries=2),
    validator=_validate_root_cause,
)

RULE_GENERATION = PhaseDefinition(
    phase=AgentPhase.RULE_GENERATION,
    agent_name="Rule Generator",
    description="Own rule semantics and a complete queryless Anchor Plan.",
    instructions=_instructions("rule_generator.md"),
    output_type=VASCoreInfo,
    tools=("list_case_artifacts", "read_case_artifact", "synthesize_anchor_plan"),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_RULE_GENERATION, output_retries=2),
    validator=_validate_rule_generation,
)

AST_GREP_SYNTHESIS = PhaseDefinition(
    phase=AgentPhase.AST_GREP_SYNTHESIS,
    agent_name="AST-Grep Synthesizer",
    description="Compile one target intent into query-only fields.",
    instructions=_instructions("ast_grep_synthesizer.md"),
    output_type=AnchorSynthesisDelta,
    tools=(
        "list_src_files",
        "search_src_files",
        "read_src_file",
        "list_case_artifacts",
        "read_case_artifact",
        "list_skill_resources",
        "read_skill_resource",
        "run_ast_grep_query",
    ),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_PER_ANCHOR, output_retries=2),
    validator=_validate_anchor_synthesis,
)

PHASE_DEFINITIONS = MappingProxyType(
    {definition.phase: definition for definition in (ISSUE_COLLECTION, ROOT_CAUSE, RULE_GENERATION, AST_GREP_SYNTHESIS)}
)


def _root_cause_input_policy(
    *,
    source: IssueCollectionInfo | ExampleSuiteIntake,
    source_root: Path,
) -> str:
    bound_root = source_root.resolve().as_posix()
    if isinstance(source, ExampleSuiteIntake):
        manifest = (
            f" The suite manifest is `{source.manifest_path}`."
            if source.manifest_path is not None
            else " No suite manifest is present."
        )
        return f"""# Input Context

## Example Suite snapshot

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `intake.files` is the exhaustive, authoritative list of files in the immutable Example Suite snapshot.{manifest}
- Use only paths present in `intake.files`; never infer, invent, or probe another filename. Read file content through `read_src_file`; `full_file=true` may be used to return a complete file within the tool's byte limit.
- The suite is a flexible collection of demonstrations that share one defect pattern. Files may be flat or nested, and there may be many bad/unsafe cases.
- The suite may distinguish bad/unsafe from good/safe cases through filenames, directories, comments, labels, or its manifest. Treat those markers as navigation hints and verify behavior in the source.
- Analyze and record every concrete bad/unsafe span. Use good/safe cases only as contrastive evidence for isolating the violated invariant; do not emit them as `buggy_components` or Case Artifacts.
- Mark the fixing invariant as inferred unless the suite directly demonstrates the corresponding safe behavior.
"""

    fixed_context = ""
    if source.fixed_commit is not None:
        fixed_context = f"""
- A verified comparison branch `fixed` exists at commit `{source.fixed_commit}`. The `read_patch_diff` tool compares `buggy` to `fixed`; call it without a path for a changed-file overview, then inspect the narrowest relevant path. Use the change to locate the cause quickly, and confirm the causal behavior in the buggy source.
"""
    else:
        fixed_context = """
- No verified fixed revision or patch-diff tool is available. Establish the causal chain from bounded source exploration and mark the fixing invariant explicitly as inferred.
"""
    return f"""# Input Context

## Repository checkout

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `src` is a verified Git repository checkout. Its active `buggy` branch is commit `{source.buggy_commit}`; source reads therefore show the affected revision.
{fixed_context}"""


def _rule_input_policy(grounding: GroundingPolicy) -> str:
    if grounding is GroundingPolicy.BAD_SPAN_COVERAGE:
        return """# Input Policy

## Example Suite grounding

- Design intents from RCA-declared bad spans.
- The accepted Anchor Plan must collectively cover every declared bad span.
"""
    return """# Input Policy

## Issue grounding

- Design intents from RCA-declared repository spans.
- Every enabled synthesized query must overlap an applicable declared span.
"""


def _synthesis_input_policy(
    *,
    source_root: Path,
    grounding_policy: GroundingPolicy,
) -> str:
    bound_root = source_root.resolve().as_posix()
    if grounding_policy is GroundingPolicy.BAD_SPAN_COVERAGE:
        return f"""# Input Context

## Example Suite snapshot

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `src` is the complete immutable Example Suite snapshot analyzed by RCA. Files may be flat or nested and may contain multiple bad/unsafe and good/safe demonstrations.
- Ground the query against the applicable RCA-declared bad/unsafe spans. Good/safe source is contrastive evidence only and is not a required positive match.
- Interpret additional `src` matches as other suite examples; accept them only when they remain plausible instances of the target behavior.
"""
    return f"""# Input Context

## Repository checkout

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `src` is the affected repository source corpus analyzed by RCA.
- Ground the query by overlapping at least one applicable RCA-declared source span for this intent.
- Treat other repository matches as precision evidence, not automatically as required positives or confirmed defects.
"""


def _workspace_layout(workspace_root: Path, source_root: Path, cases_dir: Path) -> dict[str, str]:
    return {
        "workspace": workspace_root.resolve().as_posix(),
        "src": source_root.resolve().as_posix(),
        "cases": cases_dir.resolve().as_posix(),
    }


def make_issue_collection_task(
    issue_reference: str,
    *,
    workspace_root: Path,
    task_id: str | None = None,
) -> AgentTask[IssueCollectionInfo]:
    reference = issue_reference.strip()
    if not reference:
        raise ValueError("issue_reference must be non-empty")
    return AgentTask(
        task_id=task_id or f"issue-collection:{reference}",
        definition=ISSUE_COLLECTION,
        authority=IssueCollectionAuthority(reference),
        prompt=json.dumps({"issue_reference": reference}, ensure_ascii=False, indent=2),
        workspace_root=workspace_root,
    )


def make_root_cause_task(
    source: IssueCollectionInfo | ExampleSuiteIntake,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    grounding_policy: GroundingPolicy,
    task_id: str | None = None,
) -> AgentTask[RootCauseAnalysis]:
    is_issue = isinstance(source, IssueCollectionInfo)
    fixed_revision = is_issue and source.fixed_commit is not None
    repo_path = source_root if is_issue else None
    if is_issue:
        intake: dict[str, object] = {
            "type": "issue",
            "source_layout": "repository_checkout",
            "issue_id": source.issue_id,
            "issue_summary": source.issue_summary,
            "issue_details": source.issue_details,
            "repo_url": source.repo_url,
            "buggy_commit": source.buggy_commit,
            "fixed_commit": source.fixed_commit,
            "fixed_revision_available": fixed_revision,
        }
    else:
        intake = {
            "type": "example_suite",
            "source_layout": "example_suite_snapshot",
            "registry_key": source.registry_key,
            "suite_name": source.suite_name,
            "content_digest": source.content_digest,
            "snapshot_ref": source.snapshot_ref,
            "file_count": source.file_count,
            "source_file_count": len(source.source_files),
            "manifest_path": source.manifest_path,
            "files": [metadata.path for metadata in source.files],
        }
    authority = RootCauseAuthority(
        source_root=source_root,
        cases_dir=cases_dir,
        grounding_policy=grounding_policy,
        repo_path=repo_path,
        fixed_diff=bool(fixed_revision),
    )
    return AgentTask(
        task_id=task_id or ("root-cause" if is_issue else f"example-suite-root-cause:{source.suite_name}"),
        definition=ROOT_CAUSE,
        authority=authority,
        prompt=json.dumps(
            {
                "intake": intake,
                "grounding_policy": grounding_policy.value,
                "src_tools": {
                    "root": source_root.resolve().as_posix(),
                    "path_arguments": "relative_to_root",
                },
                "workspace_layout": _workspace_layout(workspace_root, source_root, cases_dir),
            },
            ensure_ascii=False,
            indent=2,
        ),
        workspace_root=workspace_root,
        input_policy=_root_cause_input_policy(
            source=source,
            source_root=source_root,
        ),
        extra_tools=("read_patch_diff",) if authority.fixed_diff else (),
    )


def make_rule_generation_task(
    root_cause: RootCauseAnalysis,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    grounding_policy: GroundingPolicy,
    task_id: str | None = None,
) -> AgentTask[VASCoreInfo]:
    authority = RuleGenerationAuthority(
        source_root=source_root,
        cases_dir=cases_dir,
        grounding_policy=grounding_policy,
        root_cause=root_cause,
    )
    return AgentTask(
        task_id=task_id or "rule-generation",
        definition=RULE_GENERATION,
        authority=authority,
        prompt=json.dumps(
            {
                "authoritative_root_cause": root_cause.model_dump(mode="json"),
                "workspace_layout": _workspace_layout(workspace_root, source_root, cases_dir),
                "authority": {"src": "synthesizer_only", "cases": "read_only"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        workspace_root=workspace_root,
        input_policy=_rule_input_policy(grounding_policy),
    )


def make_ast_grep_synthesis_task(
    plan: AnchorPlan,
    intent: AnchorIntent,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    grounding_policy: GroundingPolicy,
    root_cause: RootCauseAnalysis,
    task_id: str | None = None,
    limits: RunLimits | None = None,
) -> AgentTask[AnchorSynthesisDelta]:
    position = next(
        (index for index, item in enumerate(plan.intents, start=1) if item.id == intent.id),
        None,
    )
    if position is None:
        raise ValueError(f"intent {intent.id!r} is not present in the Anchor Plan")
    authority = AnchorSynthesisAuthority(
        source_root=source_root,
        cases_dir=cases_dir,
        grounding_policy=grounding_policy,
        root_cause=root_cause,
        plan=plan,
        target_anchor_id=intent.id,
        skill_root=AST_GREP_SKILL_ROOT,
    )
    requirement = (
        "Match at least one applicable RCA-declared bad span."
        if grounding_policy is GroundingPolicy.BAD_SPAN_COVERAGE
        else "Overlap at least one applicable RCA-declared source span."
    )
    return AgentTask(
        task_id=task_id or f"ast-grep-synthesis:{intent.id}",
        definition=replace(
            AST_GREP_SYNTHESIS,
            agent_name=f"AST-Grep Synthesizer [{position}/{len(plan.intents)}]",
        ),
        authority=authority,
        prompt=json.dumps(
            {
                "request": {
                    "root_cause": root_cause.model_dump(mode="json"),
                    "plan": plan.model_dump(mode="json"),
                    "target_anchor_id": intent.id,
                },
                "grounding": {"policy": grounding_policy.value, "requirement": requirement},
            },
            ensure_ascii=False,
            indent=2,
        ),
        workspace_root=workspace_root,
        input_policy=_synthesis_input_policy(
            source_root=source_root,
            grounding_policy=grounding_policy,
        ),
        limit_override=limits,
    )


__all__ = [
    "AST_GREP_SKILL_ROOT",
    "AST_GREP_SYNTHESIS",
    "ISSUE_COLLECTION",
    "PHASE_DEFINITIONS",
    "ROOT_CAUSE",
    "RULE_GENERATION",
    "make_ast_grep_synthesis_task",
    "make_issue_collection_task",
    "make_root_cause_task",
    "make_rule_generation_task",
]
