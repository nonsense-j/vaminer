"""Result types shared by VAMiner preflight checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    summary: str
    detail: str | None = None
    duration_ms: int | None = None

    @classmethod
    def passed(
        cls,
        name: str,
        summary: str,
        *,
        detail: str | None = None,
        duration_ms: int | None = None,
    ) -> CheckResult:
        return cls(name, CheckStatus.PASS, summary, detail, duration_ms)

    @classmethod
    def warning(cls, name: str, summary: str, *, detail: str | None = None) -> CheckResult:
        return cls(name, CheckStatus.WARN, summary, detail)

    @classmethod
    def failed(cls, name: str, summary: str, *, detail: str | None = None) -> CheckResult:
        return cls(name, CheckStatus.FAIL, summary, detail)

    @classmethod
    def skipped(cls, name: str, summary: str) -> CheckResult:
        return cls(name, CheckStatus.SKIP, summary)

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "name": self.name,
                "status": self.status.value,
                "summary": self.summary,
                "detail": self.detail,
                "duration_ms": self.duration_ms,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    runtime_id: str
    live: bool
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime_id,
            "live": self.live,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
        }


__all__ = ["CheckResult", "CheckStatus", "PreflightReport"]
