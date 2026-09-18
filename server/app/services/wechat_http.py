"""Shared HTTP client for WeChat open APIs.

WeChat Cloud Run can expose an HTTP proxy through environment variables.  A
proxy may terminate TLS with a private certificate, which must not be trusted
for official WeChat domains unless the operator explicitly provides that CA.
Keeping these calls on a dedicated client makes the trust boundary explicit.
"""

from __future__ import annotations

import httpx

from ..config import settings


def wechat_api_url(path: str) -> str:
    base = settings.wechat_api_base.strip().rstrip("/")
    if not base:
        raise RuntimeError("WECHAT_API_BASE 不能为空")
    return f"{base}/{path.lstrip('/')}"


def create_wechat_client(
    *, timeout: httpx.Timeout | float | int = 15
) -> httpx.AsyncClient:
    """Create a client that does not silently inherit a cloud TLS proxy.

    Set ``WECHAT_HTTPS_PROXY`` only when the deployment intentionally requires
    an egress proxy.  ``WECHAT_CA_FILE`` may point to the proxy's CA PEM, but
    never to a WeChat Pay/Open Platform API certificate or private key.
    """
    kwargs: dict = {
        "timeout": timeout,
        "trust_env": False,
        "follow_redirects": True,
    }
    proxy = settings.wechat_https_proxy.strip()
    if proxy:
        kwargs["proxy"] = proxy
    ca_file = settings.wechat_ca_file.strip()
    if ca_file:
        path = settings.resolve_path(ca_file)
        if not path.is_file():
            raise RuntimeError(f"WECHAT_CA_FILE 文件不存在：{path}")
        kwargs["verify"] = str(path)
    return httpx.AsyncClient(**kwargs)
