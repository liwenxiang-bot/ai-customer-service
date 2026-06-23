"""Prometheus metrics. Exposed at /metrics for Grafana scraping (requirements §12)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

http_requests = Counter(
    "acs_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
http_latency = Histogram(
    "acs_http_request_seconds", "HTTP request latency", ["method", "path"]
)
llm_calls = Counter("acs_llm_calls_total", "LLM calls", ["model", "outcome"])
llm_tokens = Counter("acs_llm_tokens_total", "LLM tokens", ["model", "kind"])
tool_calls = Counter("acs_tool_calls_total", "Tool invocations", ["tool", "status"])
escalations = Counter("acs_escalations_total", "Human handoffs", ["reason"])
retrieval_calls = Counter("acs_retrieval_total", "RAG retrievals", ["outcome"])
