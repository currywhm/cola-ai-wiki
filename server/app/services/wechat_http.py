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


def wechat_api_urls(path: str) -> list[str]:
    """候选地址，按优先级排列：先 HTTPS，失败后回退到平台内网 HTTP 端点。"""
    urls = [wechat_api_url(path)]
    if not settings.wechat_insecure_fallback:
        return urls
    fallback_base = settings.wechat_insecure_fallback_base.strip().rstrip("/")
    if not fallback_base:
        return urls
    candidate = f"{fallback_base}/{path.lstrip('/')}"
    if candidate not in urls:
        urls.append(candidate)
    return urls


async def wechat_request(
    method: str, path: str, *, timeout: httpx.Timeout | float | int = 15, **kwargs
) -> httpx.Response:
    """请求微信开放接口，仅在传输层（含 TLS 握手）失败时切换备用地址。

    微信返回的 4xx/5xx 属于业务响应，直接交回调用方解析，不做地址回退。
    """
    urls = wechat_api_urls(path)
    last_error: httpx.TransportError | None = None
    for index, url in enumerate(urls):
        try:
            async with create_wechat_client(timeout=timeout) as client:
                response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_error = exc
            if index + 1 < len(urls):
                print(f"[wechat] {url} 连接失败（{exc}），改用备用地址 {urls[index + 1]}", flush=True)
            continue
        if index:
            print(f"[wechat] 已改用备用地址 {url}", flush=True)
        return response
    assert last_error is not None
    raise last_error
