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
# tenant is the tenant_id UUID — one series per tenant per outcome, bounded by the
# (small) tenant count, so cardinality stays safe.
retrieval_calls = Counter(
    "acs_retrieval_total", "RAG retrievals", ["outcome", "tenant"]
)
# kind=vector_sim is the raw (pre-floor) ANN top-1 cosine similarity of every vector
# search — watch it against rag_vector_min_sim to see whether the distribution hugs
# the gate (mis-calibration shows up here, not in outcome counts). kind=fusion/rerank
# is the top result score on hits: weighted-RRF scores live in ~[0.005, 0.017] (rank
# reciprocals), rerank scores in [0, 1] — hence the dense low-end buckets.
retrieval_top_score = Histogram(
    "acs_retrieval_top_score",
    "Top retrieval score",
    ["kind"],
    buckets=(
        0.002, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.05,
        0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
    ),
)
