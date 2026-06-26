"""AgentRunner — orchestrates one assistant turn.

Flow (requirements §4, §6): build context → stream from LLM with tools → bounded tool
loop (RAG / business / escalate) → stream the final answer → persist the assistant
message with full trace (tool calls, citations, tokens, cost, latency).

Resilience: tool failures are handed back to the model as text (never surfaced raw);
LLM/provider failure degrades to a graceful apology; the turn is always persisted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import build_messages, messages_have_image_parts, needs_summarization
from app.agent.events import AgentEvent
from app.agent.tools.base import ToolContext, ToolRegistry
from app.config import settings
from app.core.logging import get_logger
from app.core.metrics import llm_calls, llm_tokens
from app.core.metrics import tool_calls as tool_calls_metric
from app.llm.base import LLMProvider
from app.llm.pricing import estimate_cost
from app.llm.types import Message, ToolCall, Usage
from app.models.config import AIConfig
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole
from app.services.usage import record_turn_cost

log = get_logger("agent.runner")

_TOOL_TIMEOUT = 20.0


class AgentRunner:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        ai_config: AIConfig,
        channel_system_prompt: str | None = None,
        image_understanding: bool = False,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.ai_config = ai_config
        self.channel_system_prompt = channel_system_prompt
        self.image_understanding = image_understanding

    async def run_turn(
        self, db: AsyncSession, session: Session, trace_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        started = time.monotonic()
        messages = await build_messages(
            db, session, self.ai_config, self.channel_system_prompt, self.image_understanding
        )
        has_image_input = messages_have_image_parts(messages)
        image_tools_disabled = False
        vision_fallback_used = False
        tool_schemas = self.registry.schemas()
        ctx = ToolContext(db=db, session=session, ai_config=self.ai_config, trace_id=trace_id)

        usage = Usage()
        tool_trace: list[dict] = []
        final_text = ""
        degraded = False
        max_loops = settings.max_tool_calls_per_turn

        loop_i = 0
        while loop_i <= max_loops:
            allow_tools = loop_i < max_loops  # last pass: force a tool-free answer
            tools_for_call = (
                tool_schemas
                if allow_tools and not (has_image_input and image_tools_disabled)
                else None
            )
            # With an image, the model tends to answer the picture directly and skip retrieval.
            # Force a knowledge search on the first pass so the reply is still grounded in the KB
            # (subsequent passes go back to "auto" so it can answer or call other tools).
            tool_choice: str | dict = "auto"
            if loop_i == 0 and has_image_input and tools_for_call:
                tool_choice = {"type": "function", "function": {"name": "search_knowledge"}}
            assistant_text = ""
            pending: list[ToolCall] = []
            errored = False

            async for ev in self.provider.stream_chat(
                messages, tools=tools_for_call, tool_choice=tool_choice
            ):
                if ev.kind == "text":
                    assistant_text += ev.text
                    yield AgentEvent(kind="text", text=ev.text)
                elif ev.kind == "tool_calls":
                    pending = ev.tool_calls
                elif ev.kind == "done":
                    if ev.usage:
                        usage.prompt_tokens += ev.usage.prompt_tokens
                        usage.completion_tokens += ev.usage.completion_tokens
                elif ev.kind == "error":
                    errored = True
                    log.warning("llm_error_in_turn", error=ev.error, loop=loop_i)

            if errored and not assistant_text and not tool_trace:
                if has_image_input and tools_for_call and not image_tools_disabled:
                    # Some OpenAI-compatible gateways reject multimodal messages when tool
                    # schemas are present. Retry once without tools before giving up on vision.
                    image_tools_disabled = True
                    log.warning(
                        "llm_image_retry_without_tools",
                        model=self.ai_config.llm_model,
                        loop=loop_i,
                    )
                    continue
                if has_image_input and not vision_fallback_used:
                    # If the selected model or relay does not accept image inputs at all,
                    # rebuild the prompt with attachment names only so the turn can still
                    # complete gracefully instead of surfacing a generic failure.
                    messages = await build_messages(
                        db,
                        session,
                        self.ai_config,
                        self.channel_system_prompt,
                        image_understanding=False,
                    )
                    has_image_input = False
                    image_tools_disabled = False
                    vision_fallback_used = True
                    degraded = True
                    log.warning("llm_image_retry_as_text", model=self.ai_config.llm_model)
                    continue
                # Hard provider failure on the first pass → graceful degrade.
                degraded = True
                final_text = (
                    "抱歉，我这边暂时遇到了一点问题，没能正常处理你的请求。"
                    "你可以稍后再试一次，或回复「人工」由同事为你跟进。"
                )
                yield AgentEvent(kind="text", text=final_text)
                break

            if not pending:
                final_text = assistant_text
                break

            # ---- Execute the requested tools, then loop so the model can use results ----
            messages.append(_assistant_with_tool_calls(assistant_text, pending))
            for tc in pending:
                yield AgentEvent(kind="tool_status", tool=tc.name, status="running")
                result, ok, dur_ms = await self._run_tool(tc, ctx)
                tool_trace.append(
                    {
                        "name": tc.name,
                        "arguments": tc.parsed_arguments(),
                        "result": result[:2000],
                        "status": "ok" if ok else "failed",
                        "duration_ms": dur_ms,
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result}
                )
                yield AgentEvent(
                    kind="tool_status", tool=tc.name, status="done" if ok else "failed"
                )

            if ctx.citations:
                yield AgentEvent(kind="citations", citations=list(ctx.citations))
            if ctx.escalation:
                yield AgentEvent(kind="escalation", data=ctx.escalation)
            loop_i += 1

        # No visible answer produced (e.g. the model only called tools then returned empty on
        # its final pass — common when the knowledge base has no match). Don't fob the user off
        # with a canned greeting: force one tool-free pass so the model actually answers, using
        # whatever context (incl. tool results) it already has. Only if that is *also* empty do
        # we fall back gracefully.
        if not (final_text or "").strip() and not degraded:
            if ctx.escalation:
                final_text = "我已为你转接人工，请稍候有同事跟进。"
                yield AgentEvent(kind="text", text=final_text)
            else:
                async for ev in self.provider.stream_chat(messages, tools=None):
                    if ev.kind == "text" and ev.text:
                        final_text += ev.text
                        yield AgentEvent(kind="text", text=ev.text)
                    elif ev.kind == "done" and ev.usage:
                        usage.prompt_tokens += ev.usage.prompt_tokens
                        usage.completion_tokens += ev.usage.completion_tokens
                final_text = final_text.strip()
                if not final_text:
                    final_text = "抱歉，我这边没能正常生成回复，可以再描述一下你的问题吗？"
                    yield AgentEvent(kind="text", text=final_text)

        # ---- Persist the assistant message with the full trace ----
        latency_ms = int((time.monotonic() - started) * 1000)
        cost = estimate_cost(self.ai_config.llm_model, usage.prompt_tokens, usage.completion_tokens)
        llm_calls.labels(self.ai_config.llm_model, "degraded" if degraded else "ok").inc()
        llm_tokens.labels(self.ai_config.llm_model, "prompt").inc(usage.prompt_tokens)
        llm_tokens.labels(self.ai_config.llm_model, "completion").inc(usage.completion_tokens)
        assistant_msg = await self._persist_assistant(
            db, session, final_text, tool_trace, ctx.citations, trace_id, usage, cost,
            latency_ms, degraded,
        )

        await record_turn_cost(db, session, usage, cost)

        if needs_summarization(session):
            await _maybe_schedule_summary(session)

        yield AgentEvent(
            kind="done",
            text=final_text,
            data={
                "message_id": str(assistant_msg.id),
                "session_id": str(session.id),
                "citations": list(ctx.citations),
                "escalation": ctx.escalation,
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                },
                "cost_usd": round(cost, 6),
                "latency_ms": latency_ms,
                "degraded": degraded,
            },
        )

    # ------------------------------------------------------------------ helpers
    async def _run_tool(self, tc: ToolCall, ctx: ToolContext) -> tuple[str, bool, int]:
        tool = self.registry.get(tc.name)
        if tool is None:
            return f"未知工具：{tc.name}", False, 0
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(tool.run(tc.parsed_arguments(), ctx), _TOOL_TIMEOUT)
            tool_calls_metric.labels(tc.name, "ok").inc()
            return result, True, int((time.monotonic() - t0) * 1000)
        except TimeoutError:
            tool_calls_metric.labels(tc.name, "timeout").inc()
            return f"工具 {tc.name} 调用超时，请基于已有信息回应用户或转人工。", False, int(
                (time.monotonic() - t0) * 1000
            )
        except Exception as exc:  # noqa: BLE001 — hand the error back to the model
            tool_calls_metric.labels(tc.name, "failed").inc()
            log.warning("tool_exec_error", tool=tc.name, error=str(exc))
            return f"工具 {tc.name} 执行出错：{exc}。请据此决定如何回应用户。", False, int(
                (time.monotonic() - t0) * 1000
            )

    async def _persist_assistant(
        self, db, session, content, tool_trace, citations, trace_id, usage, cost,
        latency_ms, degraded,
    ) -> DBMessage:
        session.message_count += 1
        msg = DBMessage(
            tenant_id=session.tenant_id,
            session_id=session.id,
            seq=session.message_count,
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_trace,
            citations=citations,
            trace_id=trace_id,
            model=self.ai_config.llm_model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            degraded=degraded,
        )
        db.add(msg)
        await db.flush()
        return msg


def _assistant_with_tool_calls(text: str, calls: list[ToolCall]) -> Message:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments or "{}"},
            }
            for c in calls
        ],
    }


async def _maybe_schedule_summary(session: Session) -> None:
    """Enqueue a long-context summarization task (best-effort; no-op if queue absent)."""
    try:
        from app.tasks.queue import enqueue

        await enqueue("summarize_session", str(session.id))
    except Exception as exc:  # noqa: BLE001
        log.debug("summary_enqueue_skipped", error=str(exc))
