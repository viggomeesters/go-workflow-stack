"""Built-in Codex and Hermes adapter command contracts."""

from __future__ import annotations

import shlex

from .adapter_protocol import ADAPTER_PHASES, ADAPTER_RESULT_SCHEMA


def native_agent_prompt(phase: str, instructions: str = "") -> str:
    if phase not in ADAPTER_PHASES:
        raise ValueError(f"unsupported adapter phase: {phase}")
    result_example = (
        '{"schema":"' + ADAPTER_RESULT_SCHEMA + '","phase":"' + phase
        + '","status":"success","summary":"concise evidence-backed result"}'
    )
    parts = [
        instructions.strip(),
        "Read GO_ADAPTER_REQUEST_JSON as the canonical versioned request.",
        "Your final non-empty line must be exactly one compact JSON object matching this shape:",
        result_example,
        "Use status failure or blocked instead of success when the requested phase is not safely complete.",
        "Do not put the final JSON object in a Markdown code fence.",
    ]
    return " ".join(part for part in parts if part)


def native_agent_command(agent: str, phase: str, instructions: str = "") -> str:
    prompt = shlex.quote(native_agent_prompt(phase, instructions))
    if agent == "codex":
        sandbox = "read-only" if phase == "critic" else "workspace-write"
        return f"codex exec --sandbox {sandbox} --ephemeral -C {{repo}} {prompt}"
    if agent == "hermes":
        return f"hermes -p {prompt}"
    raise ValueError(f"unsupported native adapter: {agent}")
