"""出站地址校验（SSRF 防护）。

规则：只允许 http/https 的绝对 URL；默认拒绝回环、内网、链路本地地址。
确需抓取内网页面时设置 ALLOW_PRIVATE_NETWORKS=1。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .errors import CiError

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def _is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
        )
    )


def ensure_allowed(
    url: str,
    *,
    allow_private: bool = False,
    resolver=socket.getaddrinfo,
) -> None:
    """校验 URL；不合规直接抛 CiError（BAD_ARGUMENT / BLOCKED）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CiError(
            "BAD_ARGUMENT",
            f"只支持 http/https 的完整 URL: {url}",
            hint="形如 https://example.com/pricing",
        )
    if allow_private:
        return

    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".local"):
        raise CiError(
            "BLOCKED",
            f"拒绝抓取本机/局域网地址: {host}",
            hint="确需抓取请设置 ALLOW_PRIVATE_NETWORKS=1",
        )

    try:
        infos = resolver(host, None)
    except socket.gaierror as exc:
        raise CiError(
            "BAD_ARGUMENT",
            f"域名无法解析: {host}",
            hint="检查 URL 拼写或网络连通性",
            retryable=True,
        ) from exc

    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:  # pragma: no cover - getaddrinfo 只返回合法地址
            continue
        if _is_private(ip):
            raise CiError(
                "BLOCKED",
                f"拒绝抓取内网地址 {raw}（{host}）",
                hint="确需抓取请设置 ALLOW_PRIVATE_NETWORKS=1",
            )
