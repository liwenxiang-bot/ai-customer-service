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
from app.core.metrics import guard_blocks, llm_calls, llm_tokens
from app.core.metrics import tool_calls as tool_calls_metric
from app.llm.base import LLMProvider
from app.llm.pricing import estimate_cost
from app.llm.types import Message, ToolCall, Usage
from app.models.config import AIConfig
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole
from app.services.abuse_guard import count_block, guard_on, off_topic_reply
from app.services.answer_kind import (
    classify,
    clean_citations,
    is_chitchat,
    last_user_text,
    searched_knowledge,
    wants_human,
)
from app.services.usage import record_turn_cost

log = get_logger("agent.runner")

async def _release_connection(db) -> None:
    """Commit before each LLM call so the pooled connection isn't held while the model streams.

    An LLM pass can take tens of seconds; keeping the transaction open across it pins one
    pooled connection per in-flight conversation (pool 10+20 → ~30 concurrent chats per
    process). Work done so far in the turn (reads, a handoff ticket from a tool) is complete
    and consistent at this point, so committing it early is safe — and lets post-commit side
    effects (operator notifications) go out sooner.
    """
    in_tx = getattr(db, "in_transaction", None)
    if callable(in_tx) and in_tx():
        await db.commit()


DEGRADED_REPLY = (
    "抱歉，我这边暂时遇到了一点问题，没能正常处理你的请求。"
    "你可以稍后再试一次，或回复「人工」由同事为你跟进。"
)
INTERRUPTED_NOTE = "\n\n（回复中断了，内容可能不完整。你可以重新提问，或回复「人工」由同事跟进。）"
TRUNCATED_NOTE = "\n\n（回答较长，已截断。可以告诉我想继续了解哪一部分。）"

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
        tool_schemas = [
            t for t in self.registry.schemas()
            if t["function"]["name"] != "decline_off_topic" or guard_on(self.ai_config)
        ]
        ctx = ToolContext(db=db, session=session, ai_config=self.ai_config, trace_id=trace_id)
        user_text = last_user_text(messages)
        ctx.user_text = user_text
        # Every business question starts with a knowledge search. Left to "auto", the
        # model answered ~1/3 of real questions ("现在用不了 gpt") from memory and skipped
        # the KB entirely; greetings and handoff requests are the only exemptions.
        force_search = (
            any(t["function"]["name"] == "search_knowledge" for t in tool_schemas)
            and not is_chitchat(user_text)
            and not wants_human(user_text)
        )

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
            # First pass: force a knowledge search (business questions and image turns —
            # with an image the model tends to answer the picture directly). Subsequent
            # passes go back to "auto" so it can answer, retry a rewrite, decline or
            # escalate.
            tool_choice: str | dict = "auto"
            if loop_i == 0 and tools_for_call and (has_image_input or force_search):
                tool_choice = {"type": "function", "function": {"name": "search_knowledge"}}
            assistant_text = ""
            pending: list[ToolCall] = []
            errored = False
            finish_reason: str | None = None

            await _release_connection(db)
            async for ev in self.provider.stream_chat(
                messages, tools=tools_for_call, tool_choice=tool_choice
            ):
                if ev.kind == "text":
                    assistant_text += ev.text
                    yield AgentEvent(kind="text", text=ev.text)
                elif ev.kind == "tool_calls":
                    pending = ev.tool_calls
                elif ev.kind == "done":
                    finish_reason = ev.finish_reason
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
                final_text = DEGRADED_REPLY
                yield AgentEvent(kind="text", text=final_text)
                break

            if errored:
                # A later pass failed (after tools ran) or the stream broke mid-answer. Never
                # present that as a normal answer: it would be counted as "ok", hide the outage
                # from dashboards and be eligible for the semantic cache. Half-received tool
                # calls from a broken stream are not executed.
                degraded = True
                log.warning(
                    "llm_turn_degraded", loop=loop_i, partial_chars=len(assistant_text),
                    tools_ran=len(tool_trace),
                )
                if assistant_text.strip():
                    final_text = assistant_text + INTERRUPTED_NOTE
                    yield AgentEvent(kind="text", text=INTERRUPTED_NOTE)
                else:
                    final_text = DEGRADED_REPLY
                    yield AgentEvent(kind="text", text=final_text)
                break

            if not pending:
                final_text = assistant_text
                if finish_reason == "length":
                    # Cut off by max_tokens: keep what was said, but flag it as incomplete.
                    degraded = True
                    log.warning("llm_answer_truncated", loop=loop_i, chars=len(assistant_text))
                    final_text = assistant_text + TRUNCATED_NOTE
                    yield AgentEvent(kind="text", text=TRUNCATED_NOTE)
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

            if ctx.off_topic:
                # Out of scope: canned refusal, no further LLM call for this turn.
                guard_blocks.labels(f"off_topic:{ctx.off_topic}").inc()
                await count_block("off_topic")
                final_text = off_topic_reply(self.ai_config)
                yield AgentEvent(kind="text", text=final_text)
                break
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
                fallback_errored = False
                await _release_connection(db)
                async for ev in self.provider.stream_chat(messages, tools=None):
                    if ev.kind == "text" and ev.text:
                        final_text += ev.text
                        yield AgentEvent(kind="text", text=ev.text)
                    elif ev.kind == "done" and ev.usage:
                        usage.prompt_tokens += ev.usage.prompt_tokens
                        usage.completion_tokens += ev.usage.completion_tokens
                    elif ev.kind == "error":
                        fallback_errored = True
                        log.warning("llm_error_in_fallback", error=ev.error)
                final_text = final_text.strip()
                if fallback_errored:
                    degraded = True
                    if final_text:
                        final_text += INTERRUPTED_NOTE
                        yield AgentEvent(kind="text", text=INTERRUPTED_NOTE)
                if not final_text:
                    # Two passes and still nothing: that is a failed turn, not an answer.
                    # Flag it so dashboards and the semantic cache treat it as such.
                    degraded = True
                    log.warning("llm_empty_answer_fallback", tools_ran=len(tool_trace))
                    final_text = "抱歉，我这边没能正常生成回复，可以再描述一下你的问题吗？"
                    yield AgentEvent(kind="text", text=final_text)

        # ---- Citations the answer actually stands on + what kind of turn this was ----
        # ctx.citations is everything retrieval returned; the client-facing and persisted
        # list keeps only the [来源N] the answer mentions (none when it says it found
        # nothing). The streamed "citations" event already showed the full list, so send
        # the corrected one whenever it differs.
        citations = clean_citations(final_text, ctx.citations)
        if len(citations) != len(ctx.citations):
            yield AgentEvent(kind="citations", citations=list(citations))
        answer_kind = classify(
            user_text=user_text,
            final_text=final_text,
            citations=citations,
            searched=searched_knowledge(tool_trace),
            escalated=ctx.escalation is not None,
            off_topic=ctx.off_topic is not None,
            degraded=degraded,
        )

        # ---- Persist the assistant message with the full trace ----
        latency_ms = int((time.monotonic() - started) * 1000)
        cost = estimate_cost(self.ai_config.llm_model, usage.prompt_tokens, usage.completion_tokens)
        llm_calls.labels(self.ai_config.llm_model, "degraded" if degraded else "ok").inc()
        llm_tokens.labels(self.ai_config.llm_model, "prompt").inc(usage.prompt_tokens)
        llm_tokens.labels(self.ai_config.llm_model, "completion").inc(usage.completion_tokens)
        assistant_msg = await self._persist_assistant(
            db, session, final_text, tool_trace, citations, trace_id, usage, cost,
            latency_ms, degraded, answer_kind=answer_kind,
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
                "citations": list(citations),
                "answer_kind": answer_kind,
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
        latency_ms, degraded, answer_kind: str = "",
    ) -> DBMessage:
        # Imported lazily: app.services.conversation imports this module, so a top-level
        # import would be circular.
        from app.services.conversation import next_seq

        seq = await next_seq(db, session)
        msg = DBMessage(
            tenant_id=session.tenant_id,
            session_id=session.id,
            seq=seq,
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
            answer_kind=answer_kind,
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
