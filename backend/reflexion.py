"""
Reflexion — Attempt-level context distillation for CTF solvers.

The core problem: after a failed solve attempt, solver.bump() appends sibling
insights to a message history already 40k-80k tokens deep with failed tool calls.
The model's next attempt is cognitively poisoned by its own failures.

The fix: after each failed attempt, run a cheap distillation call that extracts
only what matters into a SolveReflection. Clear the message history. Restart
with the reflection as the sole context (plus the original system prompt).

Research backing:
- Reflexion (Shinn et al. 2023): verbal reflection + fresh attempt → 2-2.8x improvement
- HackSynth Summarizer: per-step output compression → 38% improvement
- Palisade @5: independent (fresh) attempts → +20 percentage points on CTF
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ─── Reflection data model ────────────────────────────────────────────────────

@dataclass
class SolveReflection:
    """Compact distillation of a failed solve attempt.

    Replaces 40k-80k tokens of raw message history with ~400 tokens of
    structured context for the next attempt.
    """
    # What we confirmed is true about this challenge
    confirmed_facts: list[str] = field(default_factory=list)

    # Approaches tried and their specific failure reason
    failed_approaches: list[str] = field(default_factory=list)

    # Files / scripts / data written to sandbox that are still usable
    artifacts_created: list[str] = field(default_factory=list)

    # The most promising partial lead from the attempt
    best_hypothesis: str = ""

    # Things proven to NOT work — explicit blocklist for next attempt
    dead_ends: list[str] = field(default_factory=list)

    # Concrete first step recommended for the next attempt
    next_direction: str = ""

    # Source (which bump iteration this came from)
    bump_index: int = 0

    def to_prompt_block(self, sibling_insights: str = "") -> str:
        """Format the reflection as a compact context block for the next attempt."""
        lines = ["## Reflection from previous attempt"]

        if self.confirmed_facts:
            lines.append("**Confirmed facts:**")
            for f in self.confirmed_facts:
                lines.append(f"  - {f}")

        if self.failed_approaches:
            lines.append("**Already tried (do not repeat):**")
            for a in self.failed_approaches:
                lines.append(f"  - {a}")

        if self.dead_ends:
            lines.append("**Dead ends proven:**")
            for d in self.dead_ends:
                lines.append(f"  - {d}")

        if self.artifacts_created:
            lines.append("**Sandbox artifacts you can reuse:**")
            for a in self.artifacts_created:
                lines.append(f"  - {a}")

        if self.best_hypothesis:
            lines.append(f"**Best lead so far:** {self.best_hypothesis}")

        if self.next_direction:
            lines.append(f"**Recommended first step:** {self.next_direction}")

        if sibling_insights and sibling_insights.strip() != "No sibling insights available yet.":
            lines.append(f"**Sibling agent insights:**\n{sibling_insights}")

        lines.append("")
        lines.append(
            "Start fresh. Do not repeat failed approaches. "
            "Begin with the recommended first step above."
        )
        return "\n".join(lines)

    def token_estimate(self) -> int:
        """Rough token estimate: ~1.3 tokens per character."""
        return int(len(self.to_prompt_block()) * 1.3 / 4)

# ─── Reflexion prompt ─────────────────────────────────────────────────────────

_REFLECTION_SYSTEM = """\
You are a terse CTF analysis assistant. A solver agent failed to find the flag.
Extract a structured JSON reflection from its tool call history.

Return ONLY valid JSON with these fields:
{
  "confirmed_facts": [...],    // things proven true (file type, binary protections, service behavior, etc.)
  "failed_approaches": [...],  // what was tried + brief reason it failed
  "artifacts_created": [...],  // files/scripts written to /challenge/workspace/ that are still useful
  "best_hypothesis": "...",    // most promising partial lead, or "" if none
  "dead_ends": [...],          // things PROVEN to not work (wrong flag format, service doesn't have X, etc.)
  "next_direction": "..."      // single concrete first action for the next attempt
}

Rules:
- Be extremely terse (each item ≤ 15 words)
- confirmed_facts: only things actually observed/verified, not assumptions
- failed_approaches: include the specific command/technique and why it failed
- dead_ends: only things where failure was conclusively confirmed
- next_direction: one specific actionable command or technique, not vague advice
- If nothing useful was learned, return empty lists and ""
"""


def _extract_tool_history(messages: list[Any]) -> str:
    """Extract a compressed trace of tool calls and results from message history.

    Strips system prompts and model chatter. Keeps tool calls + truncated results.
    Target: ~8k tokens max to feed into the cheap reflection model.
    """
    try:
        from pydantic_ai.messages import (
            ModelRequest, ModelResponse,
            ToolCallPart, ToolReturnPart, TextPart, UserPromptPart,
        )
    except ImportError:
        # Fallback: just stringify
        return str(messages)[:8000]

    lines: list[str] = []
    char_budget = 12000  # ~3k tokens for the reflection input

    for msg in messages:
        if char_budget <= 0:
            break

        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    result = str(part.content)[:400]
                    entry = f"TOOL_RESULT({part.tool_name}): {result}"
                    lines.append(entry)
                    char_budget -= len(entry)
                elif isinstance(part, UserPromptPart):
                    # Only include non-"continue solving" user prompts
                    content = str(part.content)
                    if len(content) > 50 and "Continue solving" not in content:
                        entry = f"USER: {content[:300]}"
                        lines.append(entry)
                        char_budget -= len(entry)

        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    args = json.dumps(part.args)[:300] if hasattr(part, 'args') else ""
                    entry = f"TOOL_CALL: {part.tool_name}({args})"
                    lines.append(entry)
                    char_budget -= len(entry)
                elif isinstance(part, TextPart) and part.content.strip():
                    entry = f"MODEL: {part.content[:200]}"
                    lines.append(entry)
                    char_budget -= len(entry)

    return "\n".join(lines)


# ─── Core reflection function ─────────────────────────────────────────────────

async def reflect(
    messages: list[Any],
    bump_index: int = 0,
    cheap_model: str = "openai:gpt-4o-mini",
    timeout_seconds: float = 30.0,
) -> SolveReflection:
    """Distill a failed solve attempt into a SolveReflection.

    Uses a cheap/fast model (gpt-4o-mini by default, ~$0.001 per call).
    Falls back to a heuristic extraction if the LLM call fails.

    Args:
        messages: The solver's pydantic-ai message history from the failed attempt.
        bump_index: Which bump iteration this is (for tracing).
        cheap_model: Model spec for the reflection call. Should be fast + cheap.
        timeout_seconds: Max time to wait for the reflection.

    Returns:
        SolveReflection with structured insight, or a minimal reflection on failure.
    """
    compressed_history = _extract_tool_history(messages)

    if not compressed_history.strip():
        logger.debug("reflect: no tool history to distill")
        return SolveReflection(bump_index=bump_index)

    try:
        reflection = await asyncio.wait_for(
            _call_reflection_model(compressed_history, cheap_model),
            timeout=timeout_seconds,
        )
        reflection.bump_index = bump_index
        logger.info(
            f"reflect: distilled {len(messages)} messages → "
            f"{reflection.token_estimate()} reflection tokens "
            f"(facts={len(reflection.confirmed_facts)}, "
            f"failed={len(reflection.failed_approaches)}, "
            f"dead_ends={len(reflection.dead_ends)})"
        )
        return reflection

    except asyncio.TimeoutError:
        logger.warning(f"reflect: timed out after {timeout_seconds}s, using heuristic")
        return _heuristic_reflection(compressed_history, bump_index)

    except Exception as e:
        logger.warning(f"reflect: LLM call failed ({e}), using heuristic")
        return _heuristic_reflection(compressed_history, bump_index)


async def _call_reflection_model(history: str, model_spec: str) -> SolveReflection:
    """Call the cheap model to produce a structured JSON reflection."""
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models import Model

        agent: Agent[None, str] = Agent(
            model_spec,
            system_prompt=_REFLECTION_SYSTEM,
            output_type=str,
        )
        result = await agent.run(
            f"Analyze this failed CTF solve attempt:\n\n{history}"
        )
        raw = result.output

    except Exception:
        # Fallback to raw openai client if pydantic-ai doesn't support the model
        import os
        import httpx
        api_key = os.environ.get("OPENAI_API_KEY", "")
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": _REFLECTION_SYSTEM},
                        {"role": "user", "content": f"Analyze this failed CTF solve attempt:\n\n{history}"},
                    ],
                    "max_tokens": 600,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]

    return _parse_reflection_json(raw)


def _parse_reflection_json(raw: str) -> SolveReflection:
    """Parse the JSON output from the reflection model."""
    # Strip markdown fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from somewhere in the text
        import re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except Exception:
                return SolveReflection()
        else:
            return SolveReflection()

    def _coerce_list(val: Any) -> list[str]:
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        return []

    return SolveReflection(
        confirmed_facts=_coerce_list(data.get("confirmed_facts")),
        failed_approaches=_coerce_list(data.get("failed_approaches")),
        artifacts_created=_coerce_list(data.get("artifacts_created")),
        best_hypothesis=str(data.get("best_hypothesis", "")).strip(),
        dead_ends=_coerce_list(data.get("dead_ends")),
        next_direction=str(data.get("next_direction", "")).strip(),
    )


def _heuristic_reflection(history: str, bump_index: int) -> SolveReflection:
    """Extract a minimal reflection heuristically when the LLM call fails.

    Looks for TOOL_CALL patterns and submit_flag results in the compressed history.
    """
    import re
    lines = history.splitlines()

    failed: list[str] = []
    artifacts: list[str] = []

    for line in lines:
        # Capture wrong flag submissions
        if "TOOL_RESULT(submit_flag)" in line and "INCORRECT" in line:
            m = re.search(r"submit_flag[^:]*:\s*(.+)", line)
            if m:
                failed.append(f"submit_flag: {m.group(1)[:80]}")

        # Capture written files
        if "TOOL_CALL: write_file" in line:
            m = re.search(r"path['\"]?\s*:\s*['\"]([^'\"]+)", line)
            if m:
                artifacts.append(m.group(1))

    return SolveReflection(
        failed_approaches=failed[:5],
        artifacts_created=artifacts[:5],
        bump_index=bump_index,
    )
