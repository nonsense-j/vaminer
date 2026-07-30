"""Runtime-neutral task specifications for the deterministic Miner phases."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from ..agent.contracts import (
    AgentPhase,
    AgentTask,
    FileAccess,
    OutputValidator,
    RunLimits,
    RuntimeCapability,
    SkillSpec,
    TaskContext,
    WorkspacePolicy,
)
from ..utils.config import (
    MINER_MAX_TURNS_ISSUE_COLLECTION,
    MINER_MAX_TURNS_PER_ANCHOR,
    MINER_MAX_TURNS_ROOT_CAUSE,
    MINER_MAX_TURNS_RULE_GENERATION,
)
from ..models.analysis import AnalysisSubject, RootCauseAnalysis
from ..models.issue import IssueCollectionInfo
from ..models.vas import VASCoreInfo
from .examples import ExampleSuiteIntake
from .validation.analysis import validate_issue_checkout, validate_root_cause_analysis
from .validation.vas import validate_vas_core

_MINER_DIR = Path(__file__).resolve().parents[1]
_INSTRUCTIONS_DIR = _MINER_DIR / "instructions"
_SKILLS_DIR = _MINER_DIR / "skills"

ISSUE_COLLECTION_LIMITS = RunLimits(
    request_limit=MINER_MAX_TURNS_ISSUE_COLLECTION,
    output_retries=2,
)
ROOT_CAUSE_LIMITS = RunLimits(
    request_limit=MINER_MAX_TURNS_ROOT_CAUSE,
    output_retries=2,
)
RULE_GENERATION_LIMITS = RunLimits(
    request_limit=MINER_MAX_TURNS_RULE_GENERATION,
    output_retries=2,
)
DEFAULT_SYNTHESIZER_LIMITS = RunLimits(
    request_limit=MINER_MAX_TURNS_PER_ANCHOR,
    output_retries=2,
)


def _instructions(name: str) -> str:
    return (_INSTRUCTIONS_DIR / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class PhaseSpec[OutputT: BaseModel]:
    """Static semantic contract shared by all adapters for one phase."""

    phase: AgentPhase
    agent_name: str
    description: str
    instructions: str
    output_type: type[OutputT]
    required_capabilities: frozenset[RuntimeCapability]
    limits: RunLimits
    skills: tuple[SkillSpec, ...] = ()


ISSUE_COLLECTION_SPEC = PhaseSpec(
    phase=AgentPhase.ISSUE_COLLECTION,
    agent_name="Issue Collector",
    description="Collects issue evidence, resolves commits, and prepares the repository checkout.",
    instructions=_instructions("issue_collector.md"),
    output_type=IssueCollectionInfo,
    required_capabilities=frozenset(
        {
            RuntimeCapability.STRUCTURED_OUTPUT,
            RuntimeCapability.ISSUE_RESEARCH,
            RuntimeCapability.WEB_RESEARCH,
            RuntimeCapability.REPOSITORY_CHECKOUT,
        }
    ),
    limits=ISSUE_COLLECTION_LIMITS,
)

ROOT_CAUSE_SPEC = PhaseSpec(
    phase=AgentPhase.ROOT_CAUSE,
    agent_name="Root Cause Analyzer",
    description="Finds the code-level root cause and writes persistent minimal cases.",
    instructions=_instructions("root_cause_analyzer.md"),
    output_type=RootCauseAnalysis,
    required_capabilities=frozenset(
        {
            RuntimeCapability.STRUCTURED_OUTPUT,
            RuntimeCapability.WORKSPACE_READ,
            RuntimeCapability.WORKSPACE_WRITE,
        }
    ),
    limits=ROOT_CAUSE_LIMITS,
)

RULE_GENERATION_SPEC = PhaseSpec(
    phase=AgentPhase.RULE_GENERATION,
    agent_name="Rule Generator",
    description="Generates a complete VAS core and delegates every query to an isolated Synthesizer.",
    instructions=_instructions("rule_generator.md"),
    output_type=VASCoreInfo,
    required_capabilities=frozenset(
        {
            RuntimeCapability.STRUCTURED_OUTPUT,
            RuntimeCapability.WORKSPACE_READ,
            RuntimeCapability.AST_GREP,
            RuntimeCapability.SKILLS,
            RuntimeCapability.AGENT_DELEGATION,
        }
    ),
    limits=RULE_GENERATION_LIMITS,
    skills=(SkillSpec(name="ast-grep", root=_SKILLS_DIR / "ast-grep"),),
)

PHASE_SPECS = MappingProxyType(
    {
        spec.phase: spec
        for spec in (
            ISSUE_COLLECTION_SPEC,
            ROOT_CAUSE_SPEC,
            RULE_GENERATION_SPEC,
        )
    }
)


def _issue_output_errors(value: IssueCollectionInfo, context: TaskContext) -> list[str]:
    return validate_issue_checkout(value, workspace_root=context.workspace_root)


def _root_cause_output_errors(value: RootCauseAnalysis, context: TaskContext) -> list[str]:
    errors: list[str] = []
    if context.source_root is None:
        errors.append("source_root is required to validate root-cause output")
    if context.cases_dir is None:
        errors.append("cases_dir is required to validate root-cause output")
    if errors:
        return errors
    assert context.source_root is not None
    assert context.cases_dir is not None
    return validate_root_cause_analysis(
        value,
        source_root=context.source_root,
        cases_dir=context.cases_dir,
    )


def _rule_output_errors(value: VASCoreInfo, context: TaskContext) -> list[str]:
    errors: list[str] = []
    if context.source_root is None:
        errors.append("source_root is required to validate rule output")
    if context.cases_dir is None:
        errors.append("cases_dir is required to validate rule output")
    if context.root_cause is None:
        errors.append("root_cause is required to validate rule output")
    if errors:
        return errors
    assert context.source_root is not None
    assert context.cases_dir is not None
    assert context.root_cause is not None
    return validate_vas_core(
        value,
        source_root=context.source_root,
        cases_dir=context.cases_dir,
        root_cause=context.root_cause,
        analysis_subject=context.analysis_subject,
    )


def _task[OutputT: BaseModel](
    spec: PhaseSpec[OutputT],
    *,
    task_id: str,
    prompt: str,
    input_instructions: str = "",
    context: TaskContext,
    workspace: WorkspacePolicy,
    output_validator: OutputValidator[OutputT],
    required_capabilities: frozenset[RuntimeCapability] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentTask[OutputT]:
    return AgentTask(
        task_id=task_id,
        phase=spec.phase,
        agent_name=spec.agent_name,
        description=spec.description,
        instructions=spec.instructions,
        input_instructions=input_instructions,
        prompt=prompt,
        output_type=spec.output_type,
        context=context,
        workspace=workspace,
        required_capabilities=required_capabilities or spec.required_capabilities,
        limits=spec.limits,
        skills=spec.skills,
        metadata=metadata or {},
        output_validator=output_validator,
    )


def _root_cause_input_instructions(*, source_type: str, fixed_revision: bool) -> str:
    if source_type == "example_suite":
        return """## Example-suite input policy

- Treat comments and good/bad/CWE labels as comparison and navigation hints, not conclusions.
- Compare good and bad examples by code behavior and record the complete set of concrete bad spans.
- Describe an observed good-example fix, or label the fixing invariant explicitly as inferred.
"""
    if fixed_revision:
        return """## Fixed-issue input policy

- Inspect the narrowest useful buggy-to-fixed diff before broad exploration.
- Treat diff and source behavior as stronger evidence than issue prose.
"""
    return """## Unfixed-issue input policy

- Use bounded source exploration to establish the causal chain.
- Label the fixing invariant explicitly as inferred because no fixed revision is available.
"""


def _rule_input_instructions(subject: AnalysisSubject) -> str:
    if subject.type == "example_suite":
        return """## Example-suite grounding policy

- Design intents from the RCA-declared bad spans.
- The finalized anchor batch must collectively cover every RCA-declared bad span.
"""
    return """## Issue grounding policy

- Design intents from the RCA-declared repository spans.
- Every synthesized query must overlap its applicable RCA-declared repository span.
"""


def make_issue_collection_task(
    issue_input: str,
    *,
    workspace_root: Path,
    output_root: Path | None = None,
    input_id: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
) -> AgentTask[IssueCollectionInfo]:
    """Build one evidence collection and checkout task."""

    issue_input = issue_input.strip()
    if not issue_input:
        raise ValueError("issue_input must be non-empty")
    return _task(
        ISSUE_COLLECTION_SPEC,
        task_id=task_id or f"issue-collection:{issue_input}",
        prompt=f"issue_input: {issue_input}",
        context=TaskContext(
            workspace_root=workspace_root,
            output_root=output_root,
            input_id=input_id,
            trace_id=trace_id,
        ),
        workspace=WorkspacePolicy(
            cwd=workspace_root,
            native_workspace_access=FileAccess.NONE,
            allow_network=True,
        ),
        output_validator=_issue_output_errors,
        metadata={"issue_input": issue_input},
    )


def make_root_cause_task(
    collection: IssueCollectionInfo,
    *,
    workspace_root: Path,
    repo_path: Path,
    cases_dir: Path,
    output_root: Path | None = None,
    input_id: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
) -> AgentTask[RootCauseAnalysis]:
    """Build one issue-backed RCA task over a real Git checkout."""

    subject = AnalysisSubject(
        type="issue",
        source_root=repo_path.resolve().as_posix(),
        cases_dir=cases_dir.resolve().as_posix(),
        grounding_policy="repo_evidence",
        provenance={
            "issue_id": collection.issue_id,
            "repo_url": collection.repo_url,
            "buggy_commit": collection.buggy_commit,
            "fixed_commit": collection.fixed_commit,
        },
    )
    prompt = json.dumps(
        {
            "intake": {
                "type": "issue",
                "issue_summary": collection.issue_summary,
                "issue_details": collection.issue_details,
                "fixed_revision_available": collection.fixed_commit is not None,
            },
            "analysis_subject": subject.model_dump(mode="json"),
        },
        indent=2,
    )
    capabilities = set(ROOT_CAUSE_SPEC.required_capabilities)
    if collection.fixed_commit is not None:
        capabilities.add(RuntimeCapability.FIXED_DIFF)
    return _task(
        ROOT_CAUSE_SPEC,
        task_id=task_id or "root-cause",
        prompt=prompt,
        input_instructions=_root_cause_input_instructions(
            source_type="issue",
            fixed_revision=collection.fixed_commit is not None,
        ),
        context=TaskContext(
            workspace_root=workspace_root,
            output_root=output_root,
            input_id=input_id,
            trace_id=trace_id,
            source_root=repo_path,
            repo_path=repo_path,
            cases_dir=cases_dir,
            analysis_subject=subject,
        ),
        workspace=WorkspacePolicy(
            cwd=workspace_root,
            native_workspace_access=FileAccess.READ_WRITE,
        ),
        required_capabilities=frozenset(capabilities),
        output_validator=_root_cause_output_errors,
        metadata={
            "source_type": "issue",
            "fixed_revision_available": collection.fixed_commit is not None,
        },
    )


def make_example_suite_root_cause_task(
    intake: ExampleSuiteIntake,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    output_root: Path | None = None,
    input_id: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
) -> AgentTask[RootCauseAnalysis]:
    """Build one RCA task over a copied example-suite snapshot."""

    portable_provenance = {
        "registry_key": intake.registry_key,
        "suite_name": intake.suite_name,
        "content_digest": intake.content_digest,
        "snapshot_ref": intake.snapshot_ref,
        "files": [item.model_dump(mode="json") for item in intake.files],
    }
    subject = AnalysisSubject(
        type="example_suite",
        source_root=source_root.resolve().as_posix(),
        cases_dir=cases_dir.resolve().as_posix(),
        grounding_policy="bad_span_coverage",
        provenance=portable_provenance,
    )
    prompt = json.dumps(
        {
            "intake": {
                "type": "example_suite",
                **portable_provenance,
                "language": intake.language.value,
                "source_files": intake.source_files,
                "manifest_path": intake.manifest_path,
            },
            "analysis_subject": subject.model_dump(mode="json"),
        },
        indent=2,
    )
    return _task(
        ROOT_CAUSE_SPEC,
        task_id=task_id or f"example-suite-root-cause:{intake.suite_name}",
        prompt=prompt,
        input_instructions=_root_cause_input_instructions(
            source_type="example_suite",
            fixed_revision=False,
        ),
        context=TaskContext(
            workspace_root=workspace_root,
            output_root=output_root,
            input_id=input_id,
            trace_id=trace_id,
            source_root=source_root,
            repo_path=None,
            cases_dir=cases_dir,
            analysis_subject=subject,
        ),
        workspace=WorkspacePolicy(
            cwd=workspace_root,
            native_workspace_access=FileAccess.READ_WRITE,
        ),
        output_validator=_root_cause_output_errors,
        metadata={
            "source_type": "example_suite",
            "example_suite": portable_provenance,
            "fixed_revision_available": False,
        },
    )


def make_rule_generation_task(
    root_cause: RootCauseAnalysis,
    *,
    workspace_root: Path,
    source_root: Path,
    repo_path: Path | None,
    cases_dir: Path,
    analysis_subject: AnalysisSubject,
    output_root: Path | None = None,
    input_id: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
) -> AgentTask[VASCoreInfo]:
    """Build the complete Rule Generator task with isolated query delegation."""

    prompt = json.dumps(
        {
            "task": "generate_complete_vas_core",
            "analysis_subject": analysis_subject.model_dump(mode="json"),
            "root_cause_analysis": root_cause.model_dump(mode="json"),
            "available_directories": {
                "source_root": source_root.resolve().as_posix(),
                "repository": repo_path.resolve().as_posix() if repo_path is not None else None,
                "cases": cases_dir.resolve().as_posix(),
            },
        },
        indent=2,
    )
    return _task(
        RULE_GENERATION_SPEC,
        task_id=task_id or "rule-generation",
        prompt=prompt,
        input_instructions=_rule_input_instructions(analysis_subject),
        context=TaskContext(
            workspace_root=workspace_root,
            output_root=output_root,
            input_id=input_id,
            trace_id=trace_id,
            source_root=source_root,
            repo_path=repo_path,
            cases_dir=cases_dir,
            root_cause=root_cause,
            analysis_subject=analysis_subject,
        ),
        workspace=WorkspacePolicy(
            cwd=workspace_root,
            native_workspace_access=FileAccess.READ_ONLY,
        ),
        output_validator=_rule_output_errors,
        metadata={
            "source_type": analysis_subject.type,
            "synthesizer_limits": {
                "request_limit": DEFAULT_SYNTHESIZER_LIMITS.request_limit,
                "output_retries": DEFAULT_SYNTHESIZER_LIMITS.output_retries,
            },
        },
    )


__all__ = [
    "DEFAULT_SYNTHESIZER_LIMITS",
    "ISSUE_COLLECTION_SPEC",
    "ISSUE_COLLECTION_LIMITS",
    "PHASE_SPECS",
    "ROOT_CAUSE_SPEC",
    "ROOT_CAUSE_LIMITS",
    "RULE_GENERATION_SPEC",
    "RULE_GENERATION_LIMITS",
    "PhaseSpec",
    "make_example_suite_root_cause_task",
    "make_issue_collection_task",
    "make_root_cause_task",
    "make_rule_generation_task",
]
