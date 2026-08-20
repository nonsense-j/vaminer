from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.miner.runtimes.claude import tracing


@pytest.mark.asyncio
async def test_emit_session_trace_calls_bundled_api_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    session_id = str(uuid.uuid4())
    transcript = tmp_path / ".claude" / "projects" / "project" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    client = object()
    calls = []

    def emit(fake_client, **kwargs):
        calls.append((fake_client, kwargs))
        return 1

    monkeypatch.setattr(tracing, "configure_tracing", lambda: client)
    monkeypatch.setattr(tracing.langfuse_hook, "emit_vaminer_session", emit)

    result = await tracing.emit_session_trace(
        session_id,
        environment={
            "HOME": str(tmp_path),
            "CC_LANGFUSE_TRACEPARENT": "00-0123456789abcdef0123456789abcdef-fedcba9876543210-01",
        },
        state_dir=tmp_path / "state",
        executable="/usr/local/bin/codeagent",
    )

    assert result.ok
    assert (result.transcript_count, result.invoked_count, result.emitted_turn_count) == (1, 1, 1)
    assert calls[0][0] is client
    assert calls[0][1]["transcript_path"] == transcript.resolve()
    assert calls[0][1]["state_dir"] == tmp_path / "state"
    assert calls[0][1]["cli_name"] == "codeagent"


@pytest.mark.asyncio
async def test_emit_session_trace_skips_without_parent_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        tracing.langfuse_hook,
        "emit_vaminer_session",
        lambda *_args, **_kwargs: pytest.fail("hook must not run without a traceparent"),
    )

    result = await tracing.emit_session_trace(
        str(uuid.uuid4()),
        environment={"HOME": str(tmp_path)},
        state_dir=tmp_path / "state",
        executable="claude",
    )

    assert result == tracing.HookRunResult()


def test_bundled_api_applies_cli_identity_and_parent_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    captured = {}

    def emit(_client, config, session_id, transcript_path, *, flush_deferred_agent_turns):
        captured.update(
            trace_name=tracing.langfuse_hook.TRACE_NAME,
            cli_slug=tracing.langfuse_hook.CLI_SLUG,
            parent_context=config.parent_context,
            session_id=session_id,
            transcript_path=transcript_path,
            flush=flush_deferred_agent_turns,
        )
        return 0

    monkeypatch.setattr(tracing.langfuse_hook, "emit_new_turns_from_transcript", emit)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    emitted = tracing.langfuse_hook.emit_vaminer_session(
        SimpleNamespace(),
        session_id="session",
        transcript_path=transcript,
        state_dir=tmp_path / "state",
        cli_name="codeagent",
        traceparent="00-0123456789abcdef0123456789abcdef-fedcba9876543210-01",
    )

    assert emitted == 0
    assert captured == {
        "trace_name": "CodeAgent Turn",
        "cli_slug": "codeagent",
        "parent_context": (
            "0123456789abcdef0123456789abcdef",
            "fedcba9876543210",
        ),
        "session_id": "session",
        "transcript_path": transcript,
        "flush": True,
    }
