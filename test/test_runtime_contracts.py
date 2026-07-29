"""Tests for runtime-neutral tasks, routing, and unified public contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from src.miner.core.tasks import (
    make_issue_collection_task,
    make_root_cause_task,
    make_rule_generation_task,
)
from src.miner.core.validation import validate_vas_core_synthesis
from src.miner.runtime import (
    AgentPhase,
    AgentRunResult,
    AgentTask,
    FileAccess,
    RuntimeCapability,
    RuntimeCapabilityError,
    RuntimeRouter,
    TaskContext,
    WorkspacePolicy,
)
from src.miner.utils.models import (
    AnalysisSubject,
    AnchorSynthesisRequest,
    AnchorSynthesisResult,
    IssueCollectionInfo,
    RootCauseAnalysis,
    VASCoreInfo,
    VASFull,
)


class ProbeOutput(BaseModel):
    value: str


@dataclass
class FakeRuntime:
    runtime_id: str
    capabilities: frozenset[RuntimeCapability]
    calls: list[AgentTask] = field(default_factory=list)

    async def run(self, task):
        self.calls.append(task)
        return AgentRunResult(
            output=ProbeOutput(value=self.runtime_id),
            runtime_id=self.runtime_id,
            model_id="fixture-model",
        )


def _root_cause() -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "language": "c",
            "root_cause_summary": "A dangerous operation executes without its required guard.",
            "analysis": "The operation is reached before the guard establishes the invariant.",
            "buggy_components": [
                {
                    "file": "bug.c",
                    "start_line": 1,
                    "end_line": 1,
                    "role": "Executes the dangerous operation.",
                    "snippet": "void trigger(void) { danger(1); }",
                }
            ],
            "fixing_pattern": "Establish the required guard before invoking danger.",
            "extracted_case_files": ["case1.c"],
        }
    )


def _core() -> VASCoreInfo:
    return VASCoreInfo.model_validate(
        {
            "category": "SECURITY",
            "language": "c",
            "root_cause_summary": _root_cause().root_cause_summary,
            "summary": "Dangerous operations must run only after the required guard is established.",
            "scenarios": {
                "unsafe": ["The dangerous operation executes before its required guard."],
                "safe": ["The required guard is established first."],
            },
            "anchors": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "query_weight": 5,
                    "type": "pattern",
                    "query": "danger($ARG);",
                    "behavior": "Calls the dangerous operation with one argument.",
                    "inspect_hint": "Inspect whether the required guard applies before the matched call.",
                }
            ],
        }
    )


def _subject(root: Path, cases: Path) -> AnalysisSubject:
    return AnalysisSubject(
        type="issue",
        source_root=root.as_posix(),
        cases_dir=cases.as_posix(),
        grounding_policy="repo_evidence",
    )


def _probe_task(*, phase: AgentPhase, required: frozenset[RuntimeCapability]) -> AgentTask[ProbeOutput]:
    root = Path("/tmp/runtime-contract-probe")
    return AgentTask(
        task_id="probe",
        phase=phase,
        agent_name="Probe",
        description="Probe routing.",
        instructions="Return the probe output.",
        prompt="probe",
        output_type=ProbeOutput,
        context=TaskContext(workspace_root=root),
        workspace=WorkspacePolicy(cwd=root),
        required_capabilities=required,
    )


async def test_router_selects_one_runtime_at_the_phase_boundary():
    sdk = FakeRuntime("pydantic-ai", frozenset({RuntimeCapability.STRUCTURED_OUTPUT}))
    claude = FakeRuntime(
        "claude-code",
        frozenset({RuntimeCapability.STRUCTURED_OUTPUT, RuntimeCapability.REPOSITORY_READ}),
    )
    router = RuntimeRouter(
        runtimes={sdk.runtime_id: sdk, claude.runtime_id: claude},
        default_runtime=sdk.runtime_id,
        phase_runtimes={AgentPhase.ROOT_CAUSE: claude.runtime_id},
    )
    task = _probe_task(
        phase=AgentPhase.ROOT_CAUSE,
        required=frozenset({RuntimeCapability.STRUCTURED_OUTPUT, RuntimeCapability.REPOSITORY_READ}),
    )

    result = await router.run(task)

    assert result.runtime_id == "claude-code"
    assert sdk.calls == []
    assert claude.calls == [task]


def test_router_rejects_missing_capabilities_without_fallback():
    sdk = FakeRuntime("pydantic-ai", frozenset({RuntimeCapability.STRUCTURED_OUTPUT}))
    fallback = FakeRuntime("claude-code", frozenset(RuntimeCapability))
    router = RuntimeRouter(
        runtimes={sdk.runtime_id: sdk, fallback.runtime_id: fallback},
        default_runtime=sdk.runtime_id,
    )
    task = _probe_task(
        phase=AgentPhase.ROOT_CAUSE,
        required=frozenset({RuntimeCapability.STRUCTURED_OUTPUT, RuntimeCapability.REPOSITORY_READ}),
    )

    with pytest.raises(RuntimeCapabilityError, match="repository_read"):
        router.resolve(task)
    assert fallback.calls == []


def test_task_factories_use_one_rule_generation_phase_and_conditional_fixed_diff(tmp_path: Path):
    repo_path = tmp_path / "repo"
    cases_dir = tmp_path / "cases"
    repo_path.mkdir()
    cases_dir.mkdir()
    collection = IssueCollectionInfo(
        issue_id="CVE-2099-0001",
        issue_summary="Fixture summary",
        issue_details="Fixture details",
        repo_url="https://github.com/example/project",
        repo_path=repo_path.as_posix(),
        buggy_commit="deadbeef",
        fixed_commit=None,
    )
    issue_task = make_issue_collection_task("CVE-2099-0001", workspace_root=tmp_path)
    rca_task = make_root_cause_task(
        collection,
        workspace_root=tmp_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
    )
    subject = _subject(repo_path, cases_dir)
    rule_task = make_rule_generation_task(
        _root_cause(),
        workspace_root=tmp_path,
        source_root=repo_path,
        repo_path=repo_path,
        cases_dir=cases_dir,
        analysis_subject=subject,
    )

    assert issue_task.workspace.repository is FileAccess.READ_WRITE
    assert rca_task.context.source_root == repo_path
    assert RuntimeCapability.FIXED_DIFF not in rca_task.required_capabilities
    assert rule_task.phase is AgentPhase.RULE_GENERATION
    assert rule_task.output_type is VASCoreInfo
    assert RuntimeCapability.AGENT_DELEGATION in rule_task.required_capabilities
    assert [skill.name for skill in rule_task.skills] == ["ast-grep"]


def test_parent_core_must_consume_exact_finalized_synthesis():
    core = _core()
    request = AnchorSynthesisRequest.model_validate(
        {
            "root_cause": _root_cause().model_dump(mode="json"),
            "summary": core.summary,
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": core.anchors[0].behavior,
                    "inspect_hint": core.anchors[0].inspect_hint,
                    "required_cases": ["case1.c"],
                }
            ],
        }
    )
    synthesis = AnchorSynthesisResult(
        anchors=core.anchors,
        case_coverage=[{"path": "case1.c", "anchor_ids": ["danger-call"]}],
        repo_evidence=[{"anchor_id": "danger-call", "file": "bug.c", "line": 1}],
        adjustments=[],
    )

    assert validate_vas_core_synthesis(core, request=request, synthesis=synthesis) == []


def test_vas_accepts_only_portable_example_suite_source_shape():
    base = {
        "vas_id": "VAS-0001",
        "category": "SECURITY",
        "language": "c",
        "summary": _core().summary,
        "scenarios": _core().scenarios.model_dump(mode="json"),
        "anchors": [item.model_dump(mode="json", by_alias=True) for item in _core().anchors],
    }
    suite = VASFull.model_validate(
        {
            **base,
            "sources": [
                {
                    "type": "example_suite",
                    "registry_key": "example-suite:CWE-2099-fixture",
                    "suite_name": "CWE-2099-fixture",
                    "content_digest": "a" * 64,
                    "snapshot_ref": "input_snapshot",
                    "files": [
                        {
                            "path": "bad.c",
                            "size": 10,
                            "sha256": "b" * 64,
                            "source": True,
                        }
                    ],
                    "root_cause_summary": _root_cause().root_cause_summary,
                }
            ],
        }
    )
    assert suite.sources[0].type == "example_suite"
    assert "/" not in suite.sources[0].snapshot_ref

    with pytest.raises(ValidationError):
        VASFull.model_validate(
            {
                **base,
                "sources": [
                    {
                        "type": "case_bundle",
                        "snapshot_path": "/tmp/input_snapshot",
                    }
                ],
            }
        )
