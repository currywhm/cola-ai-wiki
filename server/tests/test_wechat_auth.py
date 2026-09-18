import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat_auth


def _client_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    client = mock.MagicMock()
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock(return_value=False)
    client.post = mock.AsyncMock(return_value=response)
    client.get = mock.AsyncMock(return_value=response)
    return client


class WeChatCredentialValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_credentials_are_checked_with_stable_token(self):
        client = _client_response({'access_token': 'token', 'expires_in': 7200})
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'create_wechat_client', return_value=client),
        ):
            checked = await wechat_auth.validate_wechat_credentials()
        self.assertTrue(checked)
        client.post.assert_awaited_once()
        self.assertEqual(client.post.await_args.kwargs['json']['appid'], 'wx1234567890abcdef')

    async def test_invalid_credentials_raise_startup_error(self):
        client = _client_response({'errcode': 40013, 'errmsg': 'invalid appid'})
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'create_wechat_client', return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, '40013'):
                await wechat_auth.validate_wechat_credentials()

    async def test_tls_failure_does_not_block_startup(self):
        client = mock.MagicMock()
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        client.post = mock.AsyncMock(
            side_effect=httpx.ConnectError('certificate verify failed: self-signed certificate')
        )
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'create_wechat_client', return_value=client),
        ):
            checked = await wechat_auth.validate_wechat_credentials()
        self.assertFalse(checked)


class WeChatLoginExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_code_returns_session(self):
        client = _client_response({'openid': 'openid-1', 'session_key': 'session-key'})
        with (
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'create_wechat_client', return_value=client),
        ):
            data = await wechat_auth.exchange_code_for_session('login-code')
        self.assertEqual(data['openid'], 'openid-1')
        self.assertEqual(client.get.await_args.kwargs['params']['js_code'], 'login-code')

    async def test_login_transport_error_is_mapped(self):
        client = mock.MagicMock()
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        client.get = mock.AsyncMock(side_effect=httpx.ConnectError('tls failed'))
        with (
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth, 'create_wechat_client', return_value=client),
        ):
            with self.assertRaises(wechat_auth.WeChatAuthError) as ctx:
                await wechat_auth.exchange_code_for_session('login-code')
        self.assertEqual(ctx.exception.code, -1)


if __name__ == '__main__':
    unittest.main()
