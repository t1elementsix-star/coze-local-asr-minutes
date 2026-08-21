from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse


def parse_hosts(value: str) -> set[str]:
    return {item.strip().lower().rstrip(".") for item in value.split(",") if item.strip()}


def validate_source_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    allow_private: bool = False,
) -> str:
    """Validate an HTTP(S) audio URL before the service downloads it.

    The hostname allowlist is optional. Private, loopback, link-local, reserved,
    multicast, and unspecified IP addresses are rejected unless explicitly
    enabled for a trusted local deployment.
    """

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("audio_url 仅支持 http 或 https")
    if not parsed.hostname:
        raise ValueError("audio_url 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("audio_url 不允许包含用户名或密码")

    hostname = parsed.hostname.lower().rstrip(".")
    normalized_hosts = {item.lower().rstrip(".") for item in allowed_hosts}
    if normalized_hosts and hostname not in normalized_hosts:
        raise ValueError("audio_url 主机不在 AUDIO_ALLOWED_HOSTS 中")

    if allow_private:
        return url

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError("audio_url 主机名无法解析") from exc

    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError("audio_url 解析到非公网地址")

    return url


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
