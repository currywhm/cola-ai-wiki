import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat_auth


def _response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    return response


class WeChatCredentialValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_credentials_are_checked_with_stable_token(self):
        request = mock.AsyncMock(return_value=_response({'access_token': 'token', 'expires_in': 7200}))
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            checked = await wechat_auth.validate_wechat_credentials()
        self.assertTrue(checked)
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[:2], ('POST', '/cgi-bin/stable_token'))
        self.assertEqual(request.await_args.kwargs['json']['appid'], 'wx1234567890abcdef')

    async def test_invalid_credentials_raise_startup_error(self):
        request = mock.AsyncMock(return_value=_response({'errcode': 40013, 'errmsg': 'invalid appid'}))
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            with self.assertRaisesRegex(RuntimeError, '40013'):
                await wechat_auth.validate_wechat_credentials()

    async def test_tls_failure_does_not_block_startup(self):
        request = mock.AsyncMock(
            side_effect=httpx.ConnectError('certificate verify failed: self-signed certificate')
        )
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            checked = await wechat_auth.validate_wechat_credentials()
        self.assertFalse(checked)

    async def test_unexpected_payload_does_not_block_startup(self):
        """形状异常的返回体（如出口代理自己的 JSON）不能判定为凭证错误。

        线上踩过：HTTPS 被拦截后回退 HTTP，拿到 {'code': 1, 'message': 'proxy'} 这类响应，
        旧逻辑把它当成「凭证无效」直接 raise，容器卡在启动阶段反复重启。
        """
        request = mock.AsyncMock(return_value=_response({'code': 1, 'message': 'proxy rejected'}))
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            checked = await wechat_auth.validate_wechat_credentials()
        self.assertFalse(checked)

    async def test_transient_wechat_error_does_not_block_startup(self):
        """微信侧的非凭证错误（系统繁忙、频率超限、IP 白名单）同样不该阻塞启动。"""
        request = mock.AsyncMock(return_value=_response({'errcode': -1, 'errmsg': 'system error'}))
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            checked = await wechat_auth.validate_wechat_credentials()
        self.assertFalse(checked)


class WeChatLoginExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_code_returns_session(self):
        request = mock.AsyncMock(return_value=_response({'openid': 'openid-1', 'session_key': 'session-key'}))
        with (
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            data = await wechat_auth.exchange_code_for_session('login-code')
        self.assertEqual(data['openid'], 'openid-1')
        self.assertEqual(request.await_args.args[:2], ('GET', '/sns/jscode2session'))
        self.assertEqual(request.await_args.kwargs['params']['js_code'], 'login-code')

    async def test_login_transport_error_is_mapped(self):
        request = mock.AsyncMock(side_effect=httpx.ConnectError('tls failed'))
        with (
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'wechat_request', request),
        ):
            with self.assertRaises(wechat_auth.WeChatAuthError) as ctx:
                await wechat_auth.exchange_code_for_session('login-code')
        self.assertEqual(ctx.exception.code, -1)


if __name__ == '__main__':
    unittest.main()
