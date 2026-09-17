"""微信客服（联系客服）回归测试。

覆盖三件事：
1. MP 后台保存消息推送 URL 时的 GET 验签；
2. 明文模式与安全模式（AES-256-CBC）两种入站消息，都能落库、回执、并按同一模式回包；
3. 客服管理接口的口令门槛。

测试里的加解密是**独立实现**（直接用 cryptography），不复用被测代码，
否则自己加密自己解密，签名方向写反了也测不出来。
没有 appid/secret，所以不会发起任何真实的微信接口请求。
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]

KF_TOKEN = 'kf-test-token-please-change'
KF_AES_KEY = base64.b64encode(b'0123456789abcdef0123456789abcdef').decode('utf-8').rstrip('=')
KF_ADMIN_TOKEN = 'kf-admin-test-token'
APPID = 'wxtestappid0000001'
AUTOREPLY_TEXT = (
    '您好，消息已经收到，我们会尽快回复。\n'
    '如果是订单、退款、注销账号或删除资料，麻烦把订单号或知识库名称一起发过来，处理会快很多；\n'
    '常见问题也可以先看「我的 → 使用技巧」。'
)


def sha1_sorted(*parts: str) -> str:
    return hashlib.sha1(''.join(sorted(parts)).encode('utf-8')).hexdigest()


def encrypt(plain: str, appid: str, key: str = KF_AES_KEY) -> str:
    """安全模式加密：random(16) + msg_len(4, 大端) + msg + appid，再 PKCS#7 + AES-CBC。"""
    raw_key = base64.b64decode(key + '=')
    body = plain.encode('utf-8')
    payload = secrets.token_bytes(16) + struct.pack('!I', len(body)) + body + appid.encode('utf-8')
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(payload) + padder.finalize()
    encryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode('utf-8')


def decrypt(cipher_text: str, key: str = KF_AES_KEY) -> str:
    raw_key = base64.b64decode(key + '=')
    raw = base64.b64decode(cipher_text)
    decryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_key[:16])).decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    content = unpadder.update(padded) + unpadder.finalize()
    length = struct.unpack('!I', content[16:20])[0]
    return content[20:20 + length].decode('utf-8')


def inbound_xml(openid: str, content: str, *, msg_type: str = 'text', session_from: str = '') -> str:
    extra = f'<SessionFrom><![CDATA[{session_from}]]></SessionFrom>' if session_from else ''
    return (
        '<xml>'
        f'<ToUserName><![CDATA[{APPID}]]></ToUserName>'
        f'<FromUserName><![CDATA[{openid}]]></FromUserName>'
        '<CreateTime>1789000000</CreateTime>'
        f'<MsgType><![CDATA[{msg_type}]]></MsgType>'
        f'<Content><![CDATA[{content}]]></Content>'
        '<MsgId>1234567890123456</MsgId>'
        f'{extra}'
        '</xml>'
    )


class WeChatKfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='zhi-kf-test-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.database = Path(cls.tmp.name) / 'data.db'
        cls.uploads = Path(cls.tmp.name) / 'uploads'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {
            **os.environ, 'PYTHONPATH': str(ROOT), 'APP_ENV': 'test',
            'DATABASE_URL': 'sqlite+aiosqlite:///' + str(cls.database),
            'UPLOAD_DIR': str(cls.uploads), 'JWT_SECRET': secrets.token_urlsafe(48),
            'WECHAT_APPID': '', 'WECHAT_SECRET': '', 'WECHAT_MCHID': '',
            'WECHAT_KF_TOKEN': KF_TOKEN, 'WECHAT_KF_AES_KEY': KF_AES_KEY,
            'WECHAT_KF_ADMIN_TOKEN': KF_ADMIN_TOKEN, 'WECHAT_KF_ACCOUNT_SUFFIX': '',
            'WECHAT_KF_AUTOREPLY': 'true', 'WECHAT_KF_AUTOREPLY_COOLDOWN_SECONDS': '3600',
            'DEEPSEEK_API_KEY': '', 'MINIMAX_API_KEY': '', 'HARNESS_ENABLED': 'false',
        }
        cls.log = open(Path(cls.tmp.name) / 'server.log', 'w+')
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', str(port)],
            cwd=cls.tmp.name, env=env, stdout=cls.log, stderr=cls.log,
        )
        cls.addClassCleanup(cls.stop_server)
        cls.client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=20, trust_env=False)
        cls.addClassCleanup(cls.client.close)
        for _ in range(80):
            try:
                if cls.client.get('/ready').status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if cls.process.poll() is not None:
                break
            time.sleep(0.1)
        cls.log.seek(0)
        raise RuntimeError(cls.log.read())

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        cls.process.wait(timeout=15)

    def admin(self) -> dict:
        return {'X-Kf-Admin-Token': KF_ADMIN_TOKEN}

    def link_user(self, openid: str) -> str:
        """把客服消息的发送者绑到一个真实用户上，验证会话能关联到账号。"""
        user = uuid.uuid4().hex
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT INTO users(id,openid,nickname,created_at,updated_at) VALUES(?,?,?,?,?)',
                       (user, openid, '客服测试用户', '2026-09-17', '2026-09-17'))
            db.commit()
        return user

    def sessions(self) -> list:
        response = self.client.get('/api/wechat/kf/sessions', headers=self.admin())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ---- 1. GET 验签 --------------------------------------------------------
    def test_url_verification(self):
        timestamp, nonce, echostr = '1789000000', 'nonce-1', 'echo-back-me'
        signature = sha1_sorted(KF_TOKEN, timestamp, nonce)
        ok = self.client.get('/api/wechat/kf/callback', params={
            'signature': signature, 'timestamp': timestamp, 'nonce': nonce, 'echostr': echostr})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.text, echostr)

        bad = self.client.get('/api/wechat/kf/callback', params={
            'signature': 'deadbeef', 'timestamp': timestamp, 'nonce': nonce, 'echostr': echostr})
        self.assertEqual(bad.status_code, 403, bad.text)

    # ---- 2. 明文模式入站 ----------------------------------------------------
    def test_plaintext_inbound_replies_and_persists(self):
        openid = 'oKfPlainUser'
        user_id = self.link_user(openid)
        timestamp, nonce = '1789000001', 'nonce-2'
        signature = sha1_sorted(KF_TOKEN, timestamp, nonce)

        first = self.client.post('/api/wechat/kf/callback', params={
            'signature': signature, 'timestamp': timestamp, 'nonce': nonce},
            content=inbound_xml(openid, '我要注销账号', session_from='mine-data').encode('utf-8'))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertIn(AUTOREPLY_TEXT, first.text)

        # 同一个人紧接着再发一条：在冷却窗口内，只入库、不再自动回执
        second = self.client.post('/api/wechat/kf/callback', params={
            'signature': signature, 'timestamp': timestamp, 'nonce': nonce},
            content=inbound_xml(openid, '订单号 202609170001').encode('utf-8'))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.text, 'success')

        rows = self.sessions()
        self.assertEqual(len(rows), 1, rows)
        session = rows[0]
        self.assertEqual(session['openid'], openid)
        self.assertEqual(session['user_id'], user_id)
        self.assertEqual(session['message_count'], 2)
        self.assertEqual(session['unread_count'], 2)
        self.assertEqual(session['last_message']['content'], '订单号 202609170001')

        detail = self.client.get(f'/api/wechat/kf/sessions/{openid}/messages', headers=self.admin())
        self.assertEqual(detail.status_code, 200, detail.text)
        messages = detail.json()['messages']
        self.assertEqual([m['role'] for m in messages], ['user', 'kf', 'user'])
        self.assertEqual(messages[1]['source'], 'autoreply')
        raw = json.loads(messages[0]['raw_json'])
        self.assertEqual(raw['session_from'], 'mine-data')

        # 看过即已读
        self.assertEqual(self.client.get(
            f'/api/wechat/kf/sessions/{openid}/messages', headers=self.admin()).json()['session']['unread_count'], 0)

        unread = self.client.get('/api/wechat/kf/unread', headers=self.admin())
        self.assertEqual(unread.status_code, 200, unread.text)
        self.assertEqual(unread.json(), {'sessions': 0, 'messages': 0})

    # ---- 3. 安全模式入站 ----------------------------------------------------
    def test_safe_mode_inbound_is_decrypted_and_reply_is_encrypted(self):
        openid = 'oKfSafeUser'
        timestamp, nonce = '1789000002', 'nonce-3'
        cipher = encrypt(inbound_xml(openid, '怎么退款', session_from='legal'), APPID)
        body = f'<xml><ToUserName><![CDATA[{APPID}]]></ToUserName><Encrypt><![CDATA[{cipher}]]></Encrypt></xml>'
        msg_signature = sha1_sorted(KF_TOKEN, timestamp, nonce, cipher)

        response = self.client.post('/api/wechat/kf/callback', params={
            'signature': sha1_sorted(KF_TOKEN, timestamp, nonce),
            'timestamp': timestamp, 'nonce': nonce,
            'encrypt_type': 'aes', 'msg_signature': msg_signature,
        }, content=body.encode('utf-8'))
        self.assertEqual(response.status_code, 200, response.text)
        # 安全模式回包必须是密文，且能被自己的 Key 解开
        self.assertIn('<Encrypt>', response.text)
        reply_cipher = response.text.split('<Encrypt><![CDATA[')[1].split(']]>')[0]
        self.assertEqual(sha1_sorted(KF_TOKEN, timestamp, nonce, reply_cipher),
                         response.text.split('<MsgSignature><![CDATA[')[1].split(']]>')[0])
        self.assertIn(AUTOREPLY_TEXT, decrypt(reply_cipher))

        # 签名不对必须拒绝，且不能落库
        tampered = self.client.post('/api/wechat/kf/callback', params={
            'signature': sha1_sorted(KF_TOKEN, timestamp, nonce),
            'timestamp': timestamp, 'nonce': nonce,
            'encrypt_type': 'aes', 'msg_signature': 'bad-signature',
        }, content=body.encode('utf-8'))
        self.assertEqual(tampered.status_code, 403, tampered.text)
        self.assertEqual([row['openid'] for row in self.sessions()].count(openid), 1)

    # ---- 4. 管理接口口令 ----------------------------------------------------
    def test_admin_token_is_required(self):
        for path in ('/api/wechat/kf/status', '/api/wechat/kf/sessions', '/api/wechat/kf/accounts',
                     '/api/wechat/kf/unread', '/api/wechat/kf/accounts/online'):
            self.assertEqual(self.client.get(path).status_code, 403, path)
            self.assertEqual(self.client.get(path, headers={'X-Kf-Admin-Token': 'wrong'}).status_code, 403, path)
        self.assertEqual(self.client.post('/api/wechat/kf/send', json={'openid': 'x', 'msg_type': 'text'}).status_code, 403)

    def test_admin_surface_reports_missing_credentials_without_network(self):
        """测试环境没有 appid/secret：账号类接口要直接报「未配置」，不能真的去请求微信。"""
        response = self.client.get('/api/wechat/kf/accounts', headers=self.admin())
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn('WECHAT_APPID', response.text)

        media = self.client.post('/api/wechat/kf/media', headers=self.admin(),
                                 data={'media_type': 'image'},
                                 files={'file': ('a.png', b'\x89PNG\r\n\x1a\n', 'image/png')})
        self.assertEqual(media.status_code, 502, media.text)

    def test_status_never_echoes_secrets(self):
        payload = self.client.get('/api/wechat/kf/status', headers=self.admin()).json()
        self.assertTrue(payload['push_configured'])
        self.assertTrue(payload['safe_mode_configured'])
        self.assertTrue(payload['admin_token_configured'])
        self.assertTrue(payload['autoreply'])
        self.assertEqual(payload['callback_url'], '/api/wechat/kf/callback')
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(KF_TOKEN, dumped)
        self.assertNotIn(KF_AES_KEY, dumped)
        self.assertNotIn(KF_ADMIN_TOKEN, dumped)


class PayloadShapeTests(unittest.TestCase):
    """下发消息的 payload 形状：官方对 miniprogrampage.appid 标的是必填，
    AI 生成的内容要在消息下方带「内容由第三方AI生成」标注。"""

    def test_decorate_fills_required_and_optional_fields(self):
        from app.services import wechat_kf

        card = wechat_kf._decorate(
            {'touser': 'o', 'msgtype': 'miniprogrampage', 'miniprogrampage': {'title': 't'}},
            'kf@wx', appid='wxappid', ai_msg=True)
        self.assertEqual(card['miniprogrampage']['appid'], 'wxappid')
        self.assertEqual(card['customservice'], {'kf_account': 'kf@wx'})
        self.assertEqual(card['aimsgcontext'], {'is_ai_msg': 1})

        bare = wechat_kf._decorate({'touser': 'o', 'msgtype': 'text'}, '')
        self.assertNotIn('customservice', bare)
        self.assertNotIn('aimsgcontext', bare)
        self.assertIsNone(bare.get('miniprogrampage'))

    def test_miniprogrampage_appid_defaults_to_own_appid(self):
        """appid 传空时要回退到自己的 appid，不能发出一个打不开的卡片。"""
        import inspect
        from app.services import wechat_kf

        source = inspect.getsource(wechat_kf.send_miniprogrampage)
        self.assertIn("appid=appid or settings.wechat_appid", source)


if __name__ == '__main__':
    unittest.main()
