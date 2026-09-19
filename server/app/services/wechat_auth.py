"""WeChat Mini Program credential validation.

The Mini Program AppID is the developer ID shown in the WeChat admin console.
Production startup calls the official stable-token API so a typo in AppID or
AppSecret fails fast instead of surfacing later during user login.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import settings
from .wechat_http import wechat_request

class WeChatAuthError(RuntimeError):
    """WeChat OpenAPI error with the original official errcode when present."""

    def __init__(self, code: int | str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# 微信明确表示「凭证本身无效」的错误码：只有这些才在启动时判定为配置错误。
# 其它错误码（-1 系统繁忙、45009 调用频率超限、40164 IP 不在白名单等）以及形状异常的返回体
# 都属于网络/平台侧问题，只记日志、不阻塞启动——服务起不来比凭证暂时不可用严重得多。
WECHAT_FATAL_CREDENTIAL_CODES = {40013, 40125}


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
        response = await wechat_request(
            "POST", "/cgi-bin/stable_token", timeout=httpx.Timeout(12, connect=5), json=payload
        )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[wechat] 凭证在线校验跳过（网络或 TLS 暂不可用）：{exc}", flush=True)
        return False
    if not isinstance(data, dict):
        raise RuntimeError("微信开发者凭证校验失败：返回内容不是 JSON 对象")
    errcode = data.get("errcode")
    if isinstance(errcode, int) and errcode in WECHAT_FATAL_CREDENTIAL_CODES:
        raise RuntimeError(f"微信开发者凭证校验失败：errcode={errcode}, errmsg={data.get('errmsg', '')}")
    if errcode or not data.get("access_token"):
        # 没有 access_token 又不属于上面那些明确的凭证错误，说明这次拿到的不是微信的标准响应
        # （云托管出口代理拦截 HTTPS 后回退 HTTP 时出现过，返回体是代理自己的 JSON）。
        # 据这种响应判定「凭证错误」会让容器卡在启动阶段反复重启，所以按暂不可用处理，
        # 并把状态码与原始响应体打出来，便于下次直接定位到底是平台出口还是微信侧的问题。
        print(
            f"[wechat] 凭证在线校验未通过（按暂不可用处理，服务继续启动）："
            f"http={response.status_code} errcode={errcode} body={str(data)[:300]}",
            flush=True,
        )
        return False
    return True


async def exchange_code_for_session(code: str) -> dict[str, Any]:
    """Exchange a Mini Program login code for the official session payload."""
    if not settings.wechat_appid or not settings.wechat_secret:
        raise WeChatAuthError(-1, "服务端未配置微信小程序登录参数")
    try:
        response = await wechat_request(
            "GET",
            "/sns/jscode2session",
            timeout=10,
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
