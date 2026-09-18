import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat_http


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


if __name__ == '__main__':
    unittest.main()
