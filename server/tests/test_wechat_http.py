import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat_http


def _stub_client(*side_effect):
    """httpx.AsyncClient 替身：每次 request 依次抛出或返回。"""
    client = mock.MagicMock()
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock(return_value=False)
    client.request = mock.AsyncMock(side_effect=side_effect)
    return client


class WeChatHttpClientTests(unittest.TestCase):
    def test_client_does_not_inherit_environment_proxy_by_default(self):
        with (
            mock.patch.object(wechat_http.settings, 'wechat_https_proxy', ''),
            mock.patch.object(wechat_http.settings, 'wechat_ca_file', ''),
            mock.patch.object(wechat_http.httpx, 'AsyncClient') as factory,
        ):
            wechat_http.create_wechat_client(timeout=5)
        kwargs = factory.call_args.kwargs
        self.assertFalse(kwargs['trust_env'])
        self.assertTrue(kwargs['follow_redirects'])
        self.assertNotIn('proxy', kwargs)

    def test_explicit_proxy_is_used_only_when_configured(self):
        with (
            mock.patch.object(wechat_http.settings, 'wechat_https_proxy', 'http://127.0.0.1:7890'),
            mock.patch.object(wechat_http.settings, 'wechat_ca_file', ''),
            mock.patch.object(wechat_http.httpx, 'AsyncClient') as factory,
        ):
            wechat_http.create_wechat_client(timeout=5)
        self.assertEqual(factory.call_args.kwargs['proxy'], 'http://127.0.0.1:7890')

    def test_api_url_uses_https_base(self):
        with mock.patch.object(wechat_http.settings, 'wechat_api_base', 'https://api.weixin.qq.com/'):
            self.assertEqual(
                wechat_http.wechat_api_url('/cgi-bin/stable_token'),
                'https://api.weixin.qq.com/cgi-bin/stable_token',
            )

    def test_api_urls_keep_https_first_and_add_http_fallback(self):
        with (
            mock.patch.object(wechat_http.settings, 'wechat_api_base', 'https://api.weixin.qq.com'),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback', True),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback_base', 'http://api.weixin.qq.com'),
        ):
            self.assertEqual(
                wechat_http.wechat_api_urls('/sns/jscode2session'),
                [
                    'https://api.weixin.qq.com/sns/jscode2session',
                    'http://api.weixin.qq.com/sns/jscode2session',
                ],
            )

    def test_api_urls_can_disable_the_fallback(self):
        with (
            mock.patch.object(wechat_http.settings, 'wechat_api_base', 'https://api.weixin.qq.com'),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback', False),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback_base', 'http://api.weixin.qq.com'),
        ):
            self.assertEqual(
                wechat_http.wechat_api_urls('/sns/jscode2session'),
                ['https://api.weixin.qq.com/sns/jscode2session'],
            )


class WeChatRequestFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_tls_failure_retries_over_the_http_endpoint(self):
        good = mock.Mock(status_code=200)
        client = _stub_client(httpx.ConnectError('self-signed certificate'), good)
        with (
            mock.patch.object(wechat_http.settings, 'wechat_api_base', 'https://api.weixin.qq.com'),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback', True),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback_base', 'http://api.weixin.qq.com'),
            mock.patch.object(wechat_http, 'create_wechat_client', return_value=client),
        ):
            response = await wechat_http.wechat_request('GET', '/sns/jscode2session')
        self.assertIs(response, good)
        self.assertEqual(
            [call.args[1] for call in client.request.await_args_list],
            [
                'https://api.weixin.qq.com/sns/jscode2session',
                'http://api.weixin.qq.com/sns/jscode2session',
            ],
        )

    async def test_business_error_response_is_not_retried(self):
        response = mock.Mock(status_code=500)
        client = _stub_client(response)
        with (
            mock.patch.object(wechat_http.settings, 'wechat_api_base', 'https://api.weixin.qq.com'),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback', True),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback_base', 'http://api.weixin.qq.com'),
            mock.patch.object(wechat_http, 'create_wechat_client', return_value=client),
        ):
            await wechat_http.wechat_request('POST', '/cgi-bin/stable_token')
        self.assertEqual(client.request.await_count, 1)

    async def test_all_candidates_failing_raises_the_last_transport_error(self):
        client = _stub_client(
            httpx.ConnectError('tls failed'),
            httpx.ConnectError('http also failed'),
        )
        with (
            mock.patch.object(wechat_http.settings, 'wechat_api_base', 'https://api.weixin.qq.com'),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback', True),
            mock.patch.object(wechat_http.settings, 'wechat_insecure_fallback_base', 'http://api.weixin.qq.com'),
            mock.patch.object(wechat_http, 'create_wechat_client', return_value=client),
        ):
            with self.assertRaisesRegex(httpx.ConnectError, 'http also failed'):
                await wechat_http.wechat_request('GET', '/sns/jscode2session')
        self.assertEqual(client.request.await_count, 2)


if __name__ == '__main__':
    unittest.main()
