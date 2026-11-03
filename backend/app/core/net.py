"""Client address resolution.

Never read X-Forwarded-For directly: its left-most entry is whatever the client sent. uvicorn's
ProxyHeadersMiddleware (on by default) rewrites `client` from X-Forwarded-For only when the
direct peer is a trusted proxy (FORWARDED_ALLOW_IPS, CIDRs allowed), walking from the right —
so `conn.client.host` is the real client IP behind nginx/Caddy and cannot be spoofed.
"""

from __future__ import annotations

from starlette.requests import HTTPConnection


def client_ip(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else ""
