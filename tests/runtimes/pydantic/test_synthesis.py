"""Deterministic tests for runtime-neutral AST-Grep task delegation."""

from __future__ import annotations

import asyncio
import json

from src.miner.agent import AgentPhase, AgentRunResult, RuntimeUsage
from src.miner.mining.tasks import make_rule_generation_task
from src.miner.models import Anchor, AnchorSynthesisRequest, AnchorSynthesisRunResult
from src.miner.runtimes.shared.synthesis import (
    AnchorSynthesisContext,
    AnchorSynthesisDelegator,
)
from tests.support.factories import (
    BEHAVIOR,
    INSPECT_HINT,
    SOURCE,
    analysis_subject,
    root_cause,
)


def _request(rca, *, include_guard: bool = False) -> AnchorSynthesisRequest:
    intents = [
        {
            "id": "danger-call",
            "behavior_weight": 5,
            "behavior": BEHAVIOR,
            "inspect_hint": INSPECT_HINT,
            "required_cases": ["case1.c"],
        }
    ]
    if include_guard:
        intents.append(
            {
                "id": "guard-check",
                "behavior_weight": 4,
                "behavior": "Evaluates a guard before the dangerous operation.",
                "inspect_hint": "Inspect whether the guard establishes the invariant.",
                "required_cases": ["case1.c"],
            }
        )
    return AnchorSynthesisRequest.model_validate(
        {
            "root_cause": rca.model_dump(mode="json"),
            "summary": "Dangerous operations require their guarding invariant.",
            "anchor_intents": intents,
        }
    )


def test_grounding_policy_is_input_specific_child_task_data(tmp_path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    rca = root_cause()
    request = _request(rca)

    issue_parent = make_rule_generation_task(
        rca,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
    )
    issue_task = AnchorSynthesisContext.from_task(issue_parent).child_task(
        request,
        request.anchor_intents[0],
    )
    example_parent = make_rule_generation_task(
        rca,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=None,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(
            source_root,
            cases_dir,
            source_type="example_suite",
        ),
    )
    example_task = AnchorSynthesisContext.from_task(example_parent).child_task(
        request,
        request.anchor_intents[0],
    )

    issue = json.loads(issue_task.prompt)["grounding"]
    example_suite = json.loads(example_task.prompt)["grounding"]
    assert issue["policy"] == "repo_evidence"
    assert "applicable source span" in issue["requirement"]
    assert example_suite["policy"] == "bad_span_coverage"
    assert "applicable bad source span" in example_suite["requirement"]
    assert "good-example-only site" in example_suite["requirement"]


async def test_shared_delegator_fans_out_and_preserves_plan_order(tmp_path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    (source_root / "bug.c").write_text(SOURCE, encoding="utf-8")
    (cases_dir / "case1.c").write_text(SOURCE, encoding="utf-8")
    rca = root_cause()
    request_rca = rca.model_copy(
        update={
            "root_cause_summary": (
                "The generated RCA uses a concise equivalent summary."
            )
        }
    )
    task = make_rule_generation_task(
        rca,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
    )
    active = 0
    maximum_active = 0
    both_started = asyncio.Event()

    async def execute(child):
        nonlocal active, maximum_active
        assert child.phase is AgentPhase.AST_GREP_SYNTHESIS
        assert child.context.root_cause == request_rca
        payload = json.loads(child.prompt)
        assert set(payload) == {"anchor_synthesis_run_request", "grounding"}
        request_payload = payload["anchor_synthesis_run_request"]
        assert (
            request_payload["root_cause"]["root_cause_summary"]
            == request_rca.root_cause_summary
        )
        target_id = request_payload["target_anchor_id"]
        intent = next(
            item
            for item in request_payload["anchor_plan"]
            if item["id"] == target_id
        )
        active += 1
        maximum_active = max(maximum_active, active)
        if active >= 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active -= 1
        return AgentRunResult(
            output=AnchorSynthesisRunResult(
                anchor=Anchor(
                    id=intent["id"],
                    behavior_weight=intent["behavior_weight"],
                    query_weight=intent["behavior_weight"],
                    type="pattern",
                    query="danger($ARG);",
                    behavior=intent["behavior"],
                    inspect_hint=intent["inspect_hint"],
                ),
                adjustments=[],
                plan_suggestion="",
            ),
            runtime_id="test-runtime",
            model_id="test-model",
            usage=RuntimeUsage(requests=1),
        )

    request = _request(request_rca, include_guard=True)
    batch = await AnchorSynthesisDelegator(
        context=AnchorSynthesisContext.from_task(task),
        execute=execute,
    ).synthesize(request)

    assert maximum_active == 2
    assert [result.anchor.id for result in batch.results] == [
        "danger-call",
        "guard-check",
    ]
    assert [event["intent_id"] for event in batch.events] == [
        "danger-call",
        "guard-check",
    ]
    assert {event["runtime_id"] for event in batch.events} == {"test-runtime"}
