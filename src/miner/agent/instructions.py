"""Compose runtime-neutral, input-specific, and runtime-specific instructions."""

from __future__ import annotations


def compose_instructions(
    shared: str,
    *,
    input_policy: str = "",
    runtime_binding: str,
) -> str:
    """Join instruction layers in authority order without changing their scope."""

    sections = [shared.strip()]
    if input_policy.strip():
        sections.append(input_policy.strip())
    sections.append(runtime_binding.strip())
    return "\n\n".join(sections) + "\n"


__all__ = ["compose_instructions"]
