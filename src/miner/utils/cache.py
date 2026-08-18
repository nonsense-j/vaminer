"""Typed workspace cache persistence with pure acceptance."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .log import logger


class AgentCache:
    def __init__(
        self,
        agent_name: str,
        cache_dir: Path,
        suffix: str = "json",
        *,
        runtime: str | None = None,
        model: str | None = None,
    ) -> None:
        identity = [self._part(agent_name)]
        if runtime is not None:
            identity.append(self._part(runtime))
        if model is not None:
            identity.append(self._part(model))
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = cache_dir / f"{'.'.join(identity)}.{suffix}"

    @staticmethod
    def _part(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9._-]+", "_", value.strip().lower())
        return normalized.strip("._-") or "unknown"

    def get(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else None

    def set(self, result: str | BaseModel) -> None:
        if isinstance(result, BaseModel):
            text = result.model_dump_json(indent=2, by_alias=True)
        else:
            text = str(result)
            if self.path.suffix == ".json":
                with suppress(json.JSONDecodeError):
                    text = json.dumps(json.loads(text), indent=2)
        self.path.write_text(text, encoding="utf-8")


def load_agent_cache[CachedT: BaseModel](
    cache: AgentCache,
    output_type: type[CachedT],
    validate: Callable[[CachedT], Sequence[str]],
    *,
    label: str,
) -> CachedT | None:
    """Load one typed value only when its pure deterministic acceptance passes."""

    raw = cache.get()
    if raw is None:
        return None
    try:
        value = output_type.model_validate_json(raw)
    except ValidationError as exc:
        logger.warning("%s cache is invalid and will be regenerated: %s", label, exc)
        return None
    errors = tuple(validate(value))
    if errors:
        logger.warning("%s cache failed acceptance: %s", label, "; ".join(errors))
        return None
    return value


__all__ = ["AgentCache", "load_agent_cache"]
