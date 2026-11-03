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


# ---- Outbound URLs configured by admins (LLM / embedding / rerank / webhook) ----
# Self-hosters legitimately point these at localhost or LAN services (local models, the bundled
# embedding server), so private ranges stay allowed. What is never legitimate: non-HTTP schemes
# and cloud instance-metadata endpoints, which hand out machine credentials (classic SSRF prize).

_METADATA_HOSTS = {"metadata.google.internal", "metadata", "metadata.tencentyun.com"}
_METADATA_IPS = {"169.254.169.254", "100.100.100.200", "fd00:ec2::254", "169.254.170.2"}


class UnsafeURL(ValueError):
    pass


def check_outbound_url(url: str) -> None:
    """Raise UnsafeURL for a scheme/host an admin-configured endpoint must never use."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("只支持 http / https 地址")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("地址缺少主机名")
    if host in _METADATA_HOSTS:
        raise UnsafeURL("不允许访问云服务器元数据地址")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return  # unresolvable now — the call itself will fail; nothing to leak
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if str(ip) in _METADATA_IPS or (ip.version == 4 and ip.is_link_local):
            raise UnsafeURL("不允许访问云服务器元数据 / 链路本地地址")
