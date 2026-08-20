"""Run the bundled Claude transcript tracer without a user-installed plugin."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...utils.log import logger
from ...utils.telemetry import configure_tracing
from ._vendor import langfuse_hook
from .config import default_config_dir_name

BUNDLED_HOOK = Path(__file__).resolve().parent / "_vendor" / "langfuse_hook.py"
BUNDLED_HOOK_LICENSE = Path(__file__).resolve().parent / "_vendor" / "LICENSE.langfuse-observability"
BUNDLED_HOOK_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class HookRunResult:
    transcript_count: int = 0
    invoked_count: int = 0
    emitted_turn_count: int = 0
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def session_transcripts(
    session_id: str,
    environment: dict[str, str],
    *,
    executable: str = "claude",
) -> tuple[Path, ...]:
    """Return transcript files owned by one UUID session below the active Claude config."""

    try:
        if str(uuid.UUID(session_id)) != session_id:
            return ()
    except ValueError:
        return ()

    configured_root = environment.get("CLAUDE_CONFIG_DIR")
    if configured_root:
        config_root = Path(configured_root).expanduser().resolve()
    else:
        config_dir_name = default_config_dir_name(executable)
        config_root = Path(environment.get("HOME") or Path.home()).expanduser().resolve() / config_dir_name
    projects_root = config_root / "projects"
    if not projects_root.is_dir():
        return ()

    resolved_projects_root = projects_root.resolve()
    transcripts: list[Path] = []
    for transcript in projects_root.glob(f"*/{session_id}.jsonl"):
        try:
            resolved = transcript.resolve()
            resolved.relative_to(resolved_projects_root)
        except (OSError, ValueError):
            continue
        if resolved.name == f"{session_id}.jsonl" and resolved.is_file():
            transcripts.append(resolved)
    return tuple(sorted(transcripts))


def probe_bundled_hook(
    client: Any,
    *,
    transcript_path: Path,
    state_dir: Path,
    executable: str,
    display_name: str | None = None,
    traceparent: str,
) -> int:
    """Exercise the vendored parser/API without emitting a model observation."""

    return langfuse_hook.emit_vaminer_session(
        client,
        session_id=str(uuid.uuid4()),
        transcript_path=transcript_path,
        state_dir=state_dir,
        cli_name=Path(executable).name,
        cli_display_name=display_name,
        traceparent=traceparent,
    )


async def emit_session_trace(
    session_id: str,
    *,
    environment: dict[str, str],
    state_dir: Path,
    executable: str,
    display_name: str | None = None,
) -> HookRunResult:
    """Flush one completed CLI invocation into Langfuse before its transcript is deleted."""

    if not environment.get("CC_LANGFUSE_TRACEPARENT"):
        return HookRunResult()
    transcripts = session_transcripts(session_id, environment, executable=executable)
    if not transcripts:
        return HookRunResult()

    client = configure_tracing()
    if client is None:
        result = HookRunResult(
            transcript_count=len(transcripts),
            errors=("active trace context exists but the Langfuse client is unavailable",),
        )
        logger.warning("%s", result.errors[0])
        return result

    traceparent = environment["CC_LANGFUSE_TRACEPARENT"]
    user_id = environment.get("LANGFUSE_USER_ID") or environment.get("CC_LANGFUSE_USER_ID")
    trace_seed = environment.get("CC_LANGFUSE_TRACE_SEED")
    errors: list[str] = []
    invoked = 0
    emitted_turns = 0
    for transcript in transcripts:
        invoked += 1
        try:
            emitted_turns += await asyncio.to_thread(
                langfuse_hook.emit_vaminer_session,
                client,
                session_id=session_id,
                transcript_path=transcript,
                state_dir=state_dir,
                cli_name=Path(executable).name,
                cli_display_name=display_name,
                user_id=user_id,
                traceparent=traceparent,
                trace_seed=trace_seed,
            )
        except Exception as exc:  # noqa: BLE001 - tracing is observe-only.
            error = f"bundled Langfuse hook failed: {type(exc).__name__}: {exc}"
            errors.append(error)
            logger.warning("%s", error)
    return HookRunResult(
        transcript_count=len(transcripts),
        invoked_count=invoked,
        emitted_turn_count=emitted_turns,
        errors=tuple(errors),
    )


__all__ = [
    "BUNDLED_HOOK",
    "BUNDLED_HOOK_LICENSE",
    "BUNDLED_HOOK_VERSION",
    "HookRunResult",
    "emit_session_trace",
    "probe_bundled_hook",
    "session_transcripts",
]
