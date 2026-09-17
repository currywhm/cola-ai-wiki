import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat_auth


def _client_response(payload):
    response = mock.Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    client = mock.MagicMock()
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock(return_value=False)
    client.post = mock.AsyncMock(return_value=response)
    return client


class WeChatCredentialValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_credentials_are_checked_with_stable_token(self):
        client = _client_response({'access_token': 'token', 'expires_in': 7200})
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth.httpx, 'AsyncClient', return_value=client),
        ):
            await wechat_auth.validate_wechat_credentials()
        client.post.assert_awaited_once()
        self.assertEqual(client.post.await_args.kwargs['json']['appid'], 'wx1234567890abcdef')

    async def test_invalid_credentials_raise_startup_error(self):
        client = _client_response({'errcode': 40013, 'errmsg': 'invalid appid'})
        with (
            mock.patch.object(wechat_auth.settings, 'app_env', 'production'),
            mock.patch.object(wechat_auth.settings, 'wechat_appid', 'wx1234567890abcdef'),
            mock.patch.object(wechat_auth.settings, 'wechat_secret', 'a' * 32),
            mock.patch.object(wechat_auth.httpx, 'AsyncClient', return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, '40013'):
                await wechat_auth.validate_wechat_credentials()


if __name__ == '__main__':
    unittest.main()
