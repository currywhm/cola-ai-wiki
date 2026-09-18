"""WeChat Mini Program credential validation.

The Mini Program AppID is the developer ID shown in the WeChat admin console.
Production startup calls the official stable-token API so a typo in AppID or
AppSecret fails fast instead of surfacing later during user login.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import settings
from .wechat_http import create_wechat_client, wechat_api_url
class WeChatAuthError(RuntimeError):
    """WeChat OpenAPI error with the original official errcode when present."""

    def __init__(self, code: int | str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def validate_wechat_credentials() -> bool:
    """Validate production credentials when the network is reachable.

    Invalid credentials returned by WeChat remain a fatal configuration error.
    Transport and TLS failures are transient infrastructure errors, so startup
    continues and the request path will retry.
    """
    if settings.app_env != "production":
        return True
    appid = settings.wechat_appid.strip()
    secret = settings.wechat_secret.strip()
    if not appid or not secret:
        raise RuntimeError("生产环境未配置 WECHAT_APPID / WECHAT_SECRET")
    payload = {
        "grant_type": "client_credential",
        "appid": appid,
        "secret": secret,
        "force_refresh": False,
    }
    try:
        async with create_wechat_client(
            timeout=httpx.Timeout(12, connect=5)
        ) as client:
            response = await client.post(
                wechat_api_url("/cgi-bin/stable_token"),
                json=payload,
            )
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[wechat] 凭证在线校验跳过（网络或 TLS 暂不可用）：{exc}", flush=True)
        return False
    if not isinstance(data, dict):
        raise RuntimeError("微信开发者凭证校验失败：返回内容不是 JSON 对象")
    if data.get("errcode") or not data.get("access_token"):
        code = data.get("errcode", "unknown")
        message = data.get("errmsg", "unknown error")
        raise RuntimeError(f"微信开发者凭证校验失败：errcode={code}, errmsg={message}")
    return True


async def exchange_code_for_session(code: str) -> dict[str, Any]:
    """Exchange a Mini Program login code for the official session payload."""
    if not settings.wechat_appid or not settings.wechat_secret:
        raise WeChatAuthError(-1, "服务端未配置微信小程序登录参数")
    try:
        async with create_wechat_client(timeout=10) as client:
            response = await client.get(
                wechat_api_url("/sns/jscode2session"),
                params={
                    "appid": settings.wechat_appid,
                    "secret": settings.wechat_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeChatAuthError(-1, f"微信登录校验请求失败：{exc}") from exc
    if not isinstance(data, dict):
        raise WeChatAuthError(-1, "微信登录校验返回内容不是 JSON 对象")
    if data.get("errcode") or not data.get("openid"):
        code_value = data.get("errcode") or -1
        message = str(data.get("errmsg") or "微信登录校验失败")
        raise WeChatAuthError(code_value, message)
    return data
