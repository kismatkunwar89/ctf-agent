"""Ollama solver — free local models, zero API cost.

Speaks the OpenAI-compatible API served by Ollama at localhost:11434.
Uses manual function-calling loop (omits tool_choice so models that
don't support it don't 404). Falls back to thought-action parsing for
models that don't emit structured tool_calls at all.

Integrates reflect_and_reset() — on bump, distills history into a compact
SolveReflection and starts fresh instead of poisoning the context.

Recommended models (set OLLAMA_MODEL in .env):
  qwen2.5-coder:32b   best for CTF (needs ~20GB VRAM)
  qwen2.5-coder:14b   good balance (~10GB VRAM)
  qwen2.5-coder:7b    fast, lower quality (~5GB VRAM)
  deepseek-r1:14b     strong reasoning (~9GB VRAM)
  llama3.1:8b         fallback, general (~5GB VRAM)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from backend.cost_tracker import CostTracker
from backend.loop_detect import LOOP_WARNING_MESSAGE, LoopDetector
from backend.models import model_id_from_spec, provider_from_spec
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import CANCELLED, CORRECT_MARKERS, ERROR, FLAG_FOUND, GAVE_UP, SolverResult
from backend.tracing import SolverTracer
from backend.tools.core import (
    do_bash, do_check_findings, do_list_files, do_read_file,
    do_submit_flag, do_web_fetch, do_webhook_create,
    do_webhook_get_requests, do_write_file,
)

logger = logging.getLogger(__name__)

_FLAG_RE = re.compile(r"FLAG\s*:\s*(.+)", re.IGNORECASE)

# OpenAI-compatible tool schemas (same as openrouter_solver.py)
_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "bash",
        "description": "Execute a shell command inside the Docker sandbox.",
        "parameters": {"type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout_seconds": {"type": "integer", "default": 60},
            }, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file",
        "description": "Read a file from the sandbox.",
        "parameters": {"type": "object",
            "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file",
        "description": "Write a file into the sandbox.",
        "parameters": {"type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "list_files",
        "description": "List files in a sandbox directory.",
        "parameters": {"type": "object",
            "properties": {"path": {"type": "string", "default": "/challenge/distfiles"}}}}},
    {"type": "function", "function": {"name": "submit_flag",
        "description": "Submit a discovered flag. Returns CORRECT, ALREADY SOLVED, or INCORRECT.",
        "parameters": {"type": "object",
            "properties": {"flag": {"type": "string"}}, "required": ["flag"]}}},
    {"type": "function", "function": {"name": "web_fetch",
        "description": "Fetch a URL from the host (web challenges).",
        "parameters": {"type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "body": {"type": "string", "default": ""},
            }, "required": ["url"]}}},
    {"type": "function", "function": {"name": "webhook_create",
        "description": "Create a webhook.site token for out-of-band callbacks.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "webhook_get_requests",
        "description": "Fetch requests received by a webhook token.",
        "parameters": {"type": "object",
            "properties": {"uuid": {"type": "string"}}, "required": ["uuid"]}}},
    {"type": "function", "function": {"name": "check_findings",
        "description": "Read unread findings from sibling agents.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "notify_coordinator",
        "description": "Send a strategic message to the coordinator.",
        "parameters": {"type": "object",
            "properties": {"message": {"type": "string"}}, "required": ["message"]}}},
]


class OllamaSolver:
    """Free local solver — one Ollama model, one sandbox, one challenge."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        cost_tracker: CostTracker,
        settings: Any,
        cancel_event: asyncio.Event | None = None,
        no_submit: bool = False,
        submit_fn: Callable | None = None,
        message_bus: Any = None,
        notify_coordinator: Callable | None = None,
        sandbox: DockerSandbox | None = None,
        owns_sandbox: bool | None = None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.no_submit = no_submit
        self.submit_fn = submit_fn
        self.message_bus = message_bus
        self._notify_coordinator = notify_coordinator
        self._owns_sandbox = owns_sandbox if owns_sandbox is not None else (sandbox is None)
        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
        )

        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"

        # Resolved at start()
        self._base_url: str = getattr(settings, "ollama_base_url", "http://localhost:11434/v1")
        self._system_prompt: str = ""
        self._messages: list[dict[str, Any]] = []
        self._step_count: int = 0
        self._flag: str | None = None
        self._confirmed: bool = False
        self._findings: str = ""
        self._bump_count: int = 0

        # Set by reflect_and_reset()
        from backend.reflexion import SolveReflection
        self._reflection: SolveReflection | None = None
        self._sibling_insights_pending: str = ""

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.sandbox._container:
            await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        self._system_prompt = build_prompt(
            self.meta, distfile_names, container_arch=container_arch
        )
        self._messages = []
        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] OllamaSolver started ({self._base_url})")

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._owns_sandbox and self.sandbox:
            await self.sandbox.stop()

    # ─── Bump / Reflect ───────────────────────────────────────────────────────

    def bump(self, insights: str) -> None:
        """Legacy bump — appends to history. Use reflect_and_reset() instead."""
        from pydantic_ai.messages import ModelRequest, UserPromptPart
        self._messages.append({
            "role": "user",
            "content": (
                "Your previous attempt did not find the flag. "
                f"Insights from other agents:\n\n{insights}\n\n"
                "Use these insights to try a DIFFERENT approach. "
                "Do NOT repeat what has already been tried."
            )
        })
        self.loop_detector.reset()
        self.tracer.event("bump_legacy", insights=insights[:300])

    async def reflect_and_reset(self, sibling_insights: str = "") -> None:
        """Distill failed history → clear context → start fresh with structured reflection.

        Implements the Reflexion pattern (Shinn et al. 2023):
          old: append insight to 40k-80k token failure pile
          new: distill → clear → 1.1k token structured reflection
        """
        self._bump_count += 1
        old_len = len(self._messages)

        # Convert our simple dict messages to pseudo pydantic-ai messages for reflect()
        pseudo_messages = _dict_messages_to_pseudo(self._messages)

        from backend.reflexion import reflect
        reflection = await reflect(
            messages=pseudo_messages,
            bump_index=self._bump_count,
            cheap_model=getattr(self.settings, "reflection_model", "openai:gpt-4o-mini"),
        )
        self._reflection = reflection
        self._sibling_insights_pending = sibling_insights

        # THE KEY STEP: clear the polluted history
        self._messages = []
        self.loop_detector.reset()

        self.tracer.event(
            "reflect_and_reset",
            bump_index=self._bump_count,
            messages_cleared=old_len,
            reflection_tokens=reflection.token_estimate(),
            facts=len(reflection.confirmed_facts),
            failed=len(reflection.failed_approaches),
            dead_ends=len(reflection.dead_ends),
        )
        logger.info(
            f"[{self.agent_name}] reflect_and_reset #{self._bump_count}: "
            f"cleared {old_len} msgs → {reflection.token_estimate()} reflection tokens"
        )

    # ─── Main solve loop ──────────────────────────────────────────────────────

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._system_prompt:
            await self.start()

        t0 = time.monotonic()
        total_tool_calls = 0
        max_tool_calls = getattr(self.settings, "ollama_max_tool_calls", 40)

        # Build initial user message
        if not self._messages:
            if self._reflection is not None:
                initial = self._reflection.to_prompt_block(self._sibling_insights_pending)
                self._reflection = None
                self._sibling_insights_pending = ""
            else:
                initial = "Solve this CTF challenge."
            self._messages = [{"role": "user", "content": initial}]

        while not self.cancel_event.is_set():
            # ── Call Ollama ──────────────────────────────────────────────────
            request_body: dict[str, Any] = {
                "model": self.model_id,
                "messages": [{"role": "system", "content": self._system_prompt}]
                             + self._messages[-150:],  # keep last 150 turns
                "tools": _TOOL_SCHEMAS,
                "stream": False,
            }

            data = await self._post_with_retry(request_body, t0)
            if data is None:
                return self._result(ERROR)

            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            assistant_content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            self._messages.append({"role": "assistant", "content": assistant_content,
                                    **({"tool_calls": tool_calls} if tool_calls else {})})
            self.tracer.model_response(assistant_content[:500], self._step_count)

            # ── No tool calls: end of turn ───────────────────────────────────
            if not tool_calls:
                # Try to extract flag from content
                m = _FLAG_RE.search(assistant_content)
                if m:
                    self._flag = m.group(1).strip().splitlines()[0].strip()
                    self._findings = f"Flag found in assistant output: {self._flag}"
                if self._confirmed and self._flag:
                    return self._result(FLAG_FOUND)
                # Try thought_action fallback for models that embed JSON in text
                extracted = _extract_tool_call_from_text(assistant_content)
                if extracted:
                    tool_calls = [extracted]
                else:
                    self._findings = assistant_content[:500] if assistant_content else "No output"
                    return self._result(GAVE_UP)

            # ── Execute tool calls ───────────────────────────────────────────
            for tc in tool_calls:
                if self.cancel_event.is_set():
                    return self._result(CANCELLED)

                self._step_count += 1
                total_tool_calls += 1

                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                tool_name = fn.get("name") or tc.get("name", "")
                tool_call_id = tc.get("id", f"call_{self._step_count}")
                raw_args = fn.get("arguments") or tc.get("arguments") or "{}"

                try:
                    tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    tool_args = {}

                self.tracer.tool_call(tool_name, tool_args, self._step_count)

                # Loop detection
                loop_status = self.loop_detector.check(tool_name, tool_args)
                if loop_status == "break":
                    self.tracer.event("loop_break", tool=tool_name, step=self._step_count)
                    tool_result = LOOP_WARNING_MESSAGE
                else:
                    tool_result = await self._dispatch_tool(tool_name, tool_args)
                    if loop_status == "warn":
                        tool_result = f"{tool_result}\n\n{LOOP_WARNING_MESSAGE}"

                tool_result = str(tool_result)

                # Inject sibling findings every 5 steps
                if total_tool_calls % 5 == 0 and self.message_bus:
                    findings = await do_check_findings(self.message_bus, self.model_spec)
                    if findings and "No new findings" not in findings:
                        tool_result = f"{tool_result}\n\n---\n{findings}"
                        self.tracer.event("findings_injected", step=self._step_count)

                self.tracer.tool_result(tool_name, tool_result, self._step_count)

                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_result,
                })

                if self._confirmed and self._flag:
                    return self._result(FLAG_FOUND)

                if total_tool_calls >= max_tool_calls:
                    self._findings = f"Reached {max_tool_calls} tool call limit"
                    return self._result(GAVE_UP)

        return self._result(CANCELLED)

    # ─── Tool dispatch ────────────────────────────────────────────────────────

    async def _dispatch_tool(self, name: str, args: dict) -> str:
        try:
            if name == "bash":
                return await do_bash(self.sandbox, args.get("command", ""),
                                     timeout_seconds=args.get("timeout_seconds", 60))
            if name == "read_file":
                return await do_read_file(self.sandbox, args.get("path", ""))
            if name == "write_file":
                return await do_write_file(self.sandbox, args.get("path", ""),
                                           args.get("content", ""))
            if name == "list_files":
                return await do_list_files(self.sandbox,
                                           path=args.get("path", "/challenge/distfiles"))
            if name == "submit_flag":
                flag = args.get("flag", "").strip()
                if self.no_submit:
                    return f'DRY RUN — would submit "{flag}"'
                if self.submit_fn:
                    display, confirmed = await self.submit_fn(flag)
                else:
                    display, confirmed = await do_submit_flag(None, self.meta.name, flag)
                if confirmed:
                    self._confirmed = True
                    self._flag = flag
                return display
            if name == "web_fetch":
                return await do_web_fetch(args.get("url", ""),
                                          args.get("method", "GET"),
                                          args.get("body", ""))
            if name == "webhook_create":
                return await do_webhook_create()
            if name == "webhook_get_requests":
                return await do_webhook_get_requests(args.get("uuid", ""))
            if name == "check_findings":
                if not self.message_bus:
                    return "No message bus available."
                return await do_check_findings(self.message_bus, self.model_spec)
            if name == "notify_coordinator":
                if self._notify_coordinator:
                    await self._notify_coordinator(args.get("message", ""))
                    return "Message sent to coordinator."
                return "No coordinator connected."
            return f"Unknown tool: {name}"
        except Exception as e:
            return f"Tool error ({name}): {e}"

    # ─── HTTP ─────────────────────────────────────────────────────────────────

    async def _post_with_retry(
        self, body: dict, t0: float, max_retries: int = 6
    ) -> dict | None:
        backoff = 2.0
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=body,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # Track usage (local = $0 cost but log tokens)
                    usage = data.get("usage") or {}
                    self.cost_tracker.record_tokens(
                        agent_name=self.agent_name,
                        model_name=self.model_id,
                        input_tokens=int(usage.get("prompt_tokens", 0)),
                        output_tokens=int(usage.get("completion_tokens", 0)),
                        cache_read_tokens=0,
                        provider_spec=provider_from_spec(self.model_spec),
                        duration_seconds=max(0.0, time.monotonic() - t0),
                    )
                    return data
            except httpx.ConnectError:
                # Ollama may still be loading the model
                wait = min(backoff * (2 ** attempt), 30.0)
                logger.warning(f"[{self.agent_name}] Ollama not ready, retry in {wait:.0f}s")
                await asyncio.sleep(wait)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response else 0
                body_text = ""
                try:
                    body_text = e.response.json() if e.response else {}
                except Exception:
                    body_text = str(e)[:300]
                logger.error(f"[{self.agent_name}] Ollama HTTP {status}: {body_text}")
                self._findings = f"HTTP error {status}: {body_text}"
                return None
            except Exception as e:
                logger.error(f"[{self.agent_name}] Ollama error: {e}", exc_info=True)
                self._findings = f"Error: {e}"
                return None
        self._findings = "Ollama unreachable after retries — is `ollama serve` running?"
        return None

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _result(self, status: str) -> SolverResult:
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        cost = agent_usage.cost_usd if agent_usage else 0.0
        self.tracer.event("finish", status=status, flag=self._flag,
                          confirmed=self._confirmed, cost_usd=round(cost, 4))
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=self._step_count,
            cost_usd=cost,
            log_path=self.tracer.path,
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _dict_messages_to_pseudo(messages: list[dict]) -> list:
    """Convert plain dict messages to pseudo pydantic-ai message objects for reflect()."""
    try:
        from pydantic_ai.messages import (
            ModelRequest, ModelResponse,
            ToolReturnPart, ToolCallPart, TextPart, UserPromptPart,
        )
    except ImportError:
        return messages  # reflexion will fallback to stringify

    result = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        tool_calls = m.get("tool_calls", [])

        if role == "tool":
            result.append(ModelRequest(parts=[ToolReturnPart(
                tool_name=m.get("name", "tool"),
                content=str(content)[:500],
                tool_call_id=m.get("tool_call_id", ""),
            )]))
        elif role == "assistant" and tool_calls:
            parts = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                parts.append(ToolCallPart(
                    tool_name=fn.get("name", "?"),
                    args=args,
                    tool_call_id=tc.get("id", ""),
                ))
            result.append(ModelResponse(parts=parts))
        elif role == "assistant" and content:
            result.append(ModelResponse(parts=[TextPart(content=content)]))
        elif role == "user" and content:
            result.append(ModelRequest(parts=[UserPromptPart(content=content)]))
    return result


def _extract_tool_call_from_text(text: str) -> dict | None:
    """Thought-action fallback: extract a JSON tool call embedded in model output.

    Some Ollama models write tool calls as JSON in their text response instead of
    using the function_calling format. Handles patterns like:
      ```json\n{"name": "bash", "arguments": {"command": "..."}}```
    """
    if not text:
        return None
    # Look for JSON block in the text
    match = re.search(r"```(?:json)?\s*(\{[^`]+\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r'(\{"name"\s*:\s*"[^"]+"\s*,\s*"(?:arguments|parameters)")', text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        name = data.get("name") or data.get("tool")
        args = data.get("arguments") or data.get("parameters") or {}
        if name:
            return {"function": {"name": name, "arguments": json.dumps(args)}, "id": "ta_0"}
    except Exception:
        pass
    return None
