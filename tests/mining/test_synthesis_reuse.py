from pathlib import Path

import pytest
from pydantic import ValidationError

from src.miner.agent import AgentRunResult, RuleGenerationAuthority, RuntimeIdentity
from src.miner.mining import synthesis
from src.miner.mining.synthesis import AnchorPlanError, AnchorSynthesisReceipt, AnchorSynthesisSession
from src.miner.models import AnchorPlan, AnchorPlanRequest, AnchorSynthesisDelta, GroundingPolicy
from tests.mining.test_synthesis import ScriptedRuntime, _plan, _rca


def _authority(tmp_path: Path) -> RuleGenerationAuthority:
    return RuleGenerationAuthority(
        tmp_path / "src", tmp_path / "cases", GroundingPolicy.REPOSITORY_EVIDENCE, _rca(),
        synthesis_cache_path=tmp_path / "caches" / "anchor_synthesis.json",
    )


def _request(*entries) -> AnchorPlanRequest:
    return AnchorPlanRequest(summary="revised complete plan", intents=list(entries))


@pytest.mark.asyncio
async def test_reuse_preserves_full_results_and_caches_once_after_the_batch(tmp_path, monkeypatch):
    authority = _authority(tmp_path)
    calls, saved, lessons = [], [], []
    writer = synthesis.atomic_write_json

    def save(path, payload):
        saved.append(payload)
        writer(path, payload)

    monkeypatch.setattr(synthesis, "atomic_write_json", save)
    monkeypatch.setattr(synthesis, "record_ast_grep_experiences", lambda *_args: lessons.append(_args))
    monkeypatch.setattr(
        synthesis, "_query_errors",
        lambda anchor, *_args: ("invalid query",) if anchor.query == "broken(" else (),
    )

    async def execute(task):
        calls.append(task)
        anchor_id = task.authority.anchor_id
        attempt = sum(item.authority.anchor_id == anchor_id for item in calls)
        query = "broken(" if anchor_id == "b" and attempt == 1 else f"{anchor_id}_{attempt}();"
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=anchor_id, type="pattern", query=query, query_weight=2,
                adjustments=[f"attempt {attempt}"], plan_suggestion="",
                experiences=[{"mode": "ADD", "lesson_id": "C-1", "lesson": "Reusable parsing lesson."}],
            ),
            identity=RuntimeIdentity("fake", "model"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    original = AnchorPlan(summary="original", intents=[
        _plan().intents[0].model_copy(update={"id": anchor_id}) for anchor_id in ("a", "b", "c")
    ])
    first = await session.synthesize(original)
    assert len(calls) == 4  # Three children plus one query repair.
    assert len(saved) == 1
    assert len(lessons) == 3
    original_bytes = authority.synthesis_cache_path.read_bytes()

    revised_b = original.intents[1].model_copy(update={"draft_query": "improved($A);"})
    second = await session.synthesize(_request(
        {"reuse_anchor_id": "c"}, revised_b, {"reuse_anchor_id": "a"},
    ))
    assert len(calls) == 5
    assert calls[-1].authority.anchor_id == "b"
    assert "improved($A);" in calls[-1].prompt
    assert [item.anchor.id for item in second] == ["c", "b", "a"]
    assert second[0] == first[2]
    assert second[2] == first[0]
    assert second[1].anchor.query != first[1].anchor.query
    assert len(saved) == 2
    assert len(lessons) == 4  # Reused experiences are not recorded again.
    assert authority.synthesis_cache_path.read_bytes() != original_bytes
    cached = AnchorSynthesisReceipt.model_validate_json(authority.synthesis_cache_path.read_text())
    assert cached == session.receipt
    assert [item.id for item in cached.plan.intents] == ["c", "b", "a"]

    # Restoring the canonical receipt supports reuse-only calls and explicit deletion.
    restored = AnchorSynthesisSession(
        authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute), initial_receipt=cached,
    )
    third = await restored.synthesize(_request({"reuse_anchor_id": "b"}))
    assert third == [second[1]]
    assert len(calls) == 5
    assert len(saved) == 3
    assert len(lessons) == 4
    assert [item["anchor"]["id"] for item in saved[-1]["results"]] == ["b"]
    with pytest.raises(AnchorPlanError, match="unknown anchor 'a'"):
        await restored.synthesize(_request({"reuse_anchor_id": "a"}))
    assert len(saved) == 3


@pytest.mark.asyncio
async def test_invalid_reuse_and_failed_batches_preserve_latest_cache(tmp_path, monkeypatch):
    authority = _authority(tmp_path)
    monkeypatch.setattr(synthesis, "_query_errors", lambda *_args: ())
    calls = []

    async def execute(task):
        calls.append(task.authority.anchor_id)
        if task.authority.anchor_id == "failed":
            raise RuntimeError("child failed")
        return AgentRunResult(
            output=AnchorSynthesisDelta(
                anchor_id=task.authority.anchor_id, type="pattern", query="copy();",
                query_weight=2, adjustments=[], plan_suggestion="",
            ),
            identity=RuntimeIdentity("fake", "model"),
        )

    session = AnchorSynthesisSession(authority, workspace_root=tmp_path, runtime=ScriptedRuntime(execute))
    with pytest.raises(AnchorPlanError, match="unknown anchor"):
        await session.synthesize(_request({"reuse_anchor_id": "missing"}))
    assert not authority.synthesis_cache_path.exists()
    assert not calls

    first_plan = AnchorPlan(summary="original", intents=[
        _plan().intents[0].model_copy(update={"id": "a", "required_cases": ["case1.c"]}),
        _plan().intents[0].model_copy(update={"id": "b", "required_cases": ["case1_var1.c"]}),
    ])
    await session.synthesize(first_plan)
    receipt = session.receipt
    original_bytes = authority.synthesis_cache_path.read_bytes()
    with pytest.raises(AnchorPlanError, match="does not assign every declared"):
        await session.synthesize(_request({"reuse_anchor_id": "a"}))
    assert len(calls) == 2

    with pytest.raises(RuntimeError, match="child failed"):
        await session.synthesize(_request(
            {"reuse_anchor_id": "a"},
            first_plan.intents[1].model_copy(update={"id": "failed"}),
        ))
    assert session.receipt is receipt
    assert authority.synthesis_cache_path.read_bytes() == original_bytes

    def fail_save(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(synthesis, "atomic_write_json", fail_save)
    with pytest.raises(OSError, match="disk full"):
        await session.synthesize(_request({"reuse_anchor_id": "a"}, {"reuse_anchor_id": "b"}))
    assert session.receipt is receipt
    assert authority.synthesis_cache_path.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_reused_query_must_still_cover_its_required_cases(tmp_path, monkeypatch):
    from src.miner.models import AnchorSynthesisResult

    intent = _plan().intents[0]
    delta = AnchorSynthesisDelta(
        anchor_id=intent.id, type="pattern", query="copy();", query_weight=2,
        adjustments=[], plan_suggestion="",
    )
    receipt = AnchorSynthesisReceipt(plan=_plan(), results=[AnchorSynthesisResult(
        anchor=synthesis._assemble_anchor(intent, delta), adjustments=[], plan_suggestion="",
    )])
    monkeypatch.setattr(synthesis, "_query_errors", lambda *_args: ("query misses required Case Artifacts: case1.c",))
    session = AnchorSynthesisSession(
        _authority(tmp_path), workspace_root=tmp_path, runtime=ScriptedRuntime(None), initial_receipt=receipt,
    )
    with pytest.raises(AnchorPlanError, match="submit a complete intent"):
        await session.synthesize(_request({"reuse_anchor_id": intent.id}))
    assert session.receipt is receipt


@pytest.mark.parametrize("entries", [
    [{"reuse_anchor_id": "copy-site"}, {"reuse_anchor_id": "copy-site"}],
    [_plan().intents[0], {"reuse_anchor_id": "copy-site"}],
    [{"reuse_anchor_id": "copy-site", "draft_query": "copy();"}],
    [{"reuse_anchor_id": "copy-site", **_plan().intents[0].model_dump()}],
])
def test_request_rejects_duplicate_ids_and_ambiguous_reuse(entries):
    with pytest.raises(ValidationError):
        _request(*entries)
