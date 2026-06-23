"""Structured logging with trace_id correlation.

Every log line is JSON in production (greppable, ingestible) and pretty in dev.
A contextvar carries `trace_id` so a single conversation turn / request can be
stitched together across the async call stack — the backbone of "why did it
answer this way" debugging.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

from app.config import settings

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str | None) -> None:
    _trace_id.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id.get()


def _add_trace_id(logger, method_name, event_dict):  # noqa: ANN001
    tid = _trace_id.get()
    if tid:
        event_dict["trace_id"] = tid
    return event_dict


def configure_logging() -> None:
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _add_trace_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.app_debug else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Tame noisy third-party loggers, route through stdlib -> stderr.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    for noisy in ("uvicorn.access", "httpx", "boto3", "botocore", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(name)
