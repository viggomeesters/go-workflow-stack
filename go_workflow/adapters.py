"""Built-in Codex and Hermes adapter command contracts."""

from __future__ import annotations

import re
import shlex
import subprocess

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


def detect_hermes_prompt_flag(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--help"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    help_text = f"{result.stdout}\n{result.stderr}"
    for flag in ("-z", "-p"):
        pattern = rf"(?:^|[\s\[,]){re.escape(flag)}(?:,\s*--[a-z0-9-]+)?\s+PROMPT(?=$|[\s,\]])"
        if re.search(pattern, help_text, re.IGNORECASE | re.MULTILINE):
            return flag
    return None


def native_agent_command(
    agent: str,
    phase: str,
    instructions: str = "",
    *,
    hermes_prompt_flag: str = "-z",
) -> str:
    prompt = shlex.quote(native_agent_prompt(phase, instructions))
    if agent == "codex":
        sandbox = "read-only" if phase == "critic" else "workspace-write"
        return f"codex exec --sandbox {sandbox} --ephemeral -C {{repo_shell}} {prompt}"
    if agent == "hermes":
        if hermes_prompt_flag not in {"-z", "-p"}:
            raise ValueError(f"unsupported Hermes prompt flag: {hermes_prompt_flag}")
        return f"hermes {hermes_prompt_flag} {prompt}"
    raise ValueError(f"unsupported native adapter: {agent}")
