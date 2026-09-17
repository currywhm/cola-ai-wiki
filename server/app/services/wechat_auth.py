"""WeChat Mini Program credential validation.

The Mini Program AppID is the developer ID shown in the WeChat admin console.
Production startup calls the official stable-token API so a typo in AppID or
AppSecret fails fast instead of surfacing later during user login.
"""

from __future__ import annotations

import httpx

from ..config import settings

WECHAT_API_BASE = "https://api.weixin.qq.com"


async def validate_wechat_credentials() -> None:
    if settings.app_env != "production":
        return
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5)) as client:
            response = await client.post(
                f"{WECHAT_API_BASE}/cgi-bin/stable_token",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"微信开发者凭证校验请求失败：{exc}") from exc
    if data.get("errcode") or not data.get("access_token"):
        code = data.get("errcode", "unknown")
        message = data.get("errmsg", "unknown error")
        raise RuntimeError(f"微信开发者凭证校验失败：errcode={code}, errmsg={message}")
