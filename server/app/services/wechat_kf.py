"""微信客服消息（kf-mgnt/kf-message）服务端封装。

对应官方「客服消息」全量接口，一个都不少：

账号管理
  POST /customservice/kfaccount/add         添加客服账号
  POST /customservice/kfaccount/del         删除客服账号
  GET  /cgi-bin/customservice/getkflist     获取所有客服账号
  GET  /cgi-bin/customservice/getonlinekflist  获取在线客服列表
  GET  /customservice/kfaccount/setadmin    设置客服管理员
  GET  /customservice/kfaccount/canceladmin 取消客服管理员

消息与素材
  POST /cgi-bin/message/custom/send               发送客服消息
  POST /cgi-bin/message/custom/business/typing    客服输入状态
  POST /cgi-bin/media/upload                      上传临时素材
  GET  /cgi-bin/media/get                         获取临时素材

官方接口清单的完整性以文档目录为准：小程序「服务端 → kf-mgnt」下只有 kf-message 与
kf-management 两组，上面 10 个覆盖了 kf-message 的全部；kf-management（registerbusiness /
getbusiness / listbusiness / updatebusiness）是服务商给「客服子商户」用的，普通小程序
主体没有子商户，不属本模块范围。

另外，消息推送没有配置到 MP 后台之前，用户发出的消息微信不会推给我们，后台会话列表
就是空的——这不是代码问题，是还差一步配置（见 .env.example 的 WECHAT_KF_*）。

接口只能在服务器端调用：access_token 是账号级凭证，落到前端等于把整个小程序交出去。
"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Any
import xml.etree.ElementTree as ET

import httpx

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..config import settings
from ..db import connect, fetchone

WECHAT_API_BASE = 'https://api.weixin.qq.com'
TOKEN_CACHE_KEY = 'kf_access_token'
# 官方给的 token 有效期 7200 秒，提前 5 分钟换掉，避免边界上刚好过期
TOKEN_SAFETY_SECONDS = 300


class WeChatKfError(RuntimeError):
    """客服接口调用失败。带 errcode，方便页面把「未配置客服」和「签名不对」区分开。"""

    def __init__(self, code: int, message: str, path: str = '') -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def configured() -> bool:
    return bool(settings.wechat_appid and settings.wechat_secret)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# access_token：进程内 + 数据库双缓存
# 单机进程缓存足够快，落库是为了重启后不用立刻再换一次（换 token 太频繁会顶掉旧的，
# 正在跑的其他实例会突然失效）。
# ---------------------------------------------------------------------------
async def _cached_token() -> str:
    db = await connect()
    try:
        row = await fetchone(db, 'SELECT value, expires_at FROM wx_tokens WHERE name=?', (TOKEN_CACHE_KEY,))
    finally:
        await db.close()
    if not row or not row['value']:
        return ''
    try:
        expires = datetime.fromisoformat(str(row['expires_at']))
    except ValueError:
        return ''
    if expires - timedelta(seconds=TOKEN_SAFETY_SECONDS) <= _now():
        return ''
    return str(row['value'])


async def _store_token(value: str, expires_in: int) -> None:
    expires_at = (_now() + timedelta(seconds=max(60, expires_in))).isoformat()
    db = await connect()
    try:
        await db.execute(
            "INSERT INTO wx_tokens(name,value,expires_at,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (TOKEN_CACHE_KEY, value, expires_at, _now().isoformat()),
        )
        await db.commit()
    finally:
        await db.close()


async def access_token(force: bool = False) -> str:
    if not configured():
        raise WeChatKfError(-1, '服务端未配置 WECHAT_APPID / WECHAT_SECRET，客服接口不可用')
    if not force:
        cached = await _cached_token()
        if cached:
            return cached
    async with httpx.AsyncClient(timeout=15) as client:
        # stable_token 是官方推荐的稳定版：不会被其它业务刷新顶掉，也更容易做多实例
        response = await client.post(
            f'{WECHAT_API_BASE}/cgi-bin/stable_token',
            json={'grant_type': 'client_credential', 'appid': settings.wechat_appid, 'secret': settings.wechat_secret, 'force_refresh': bool(force)},
        )
        data = response.json() if response.content else {}
        if not data.get('access_token'):
            # 老账号/灰度账号可能还没有 stable_token，退回普通接口
            response = await client.get(
                f'{WECHAT_API_BASE}/cgi-bin/token',
                params={'grant_type': 'client_credential', 'appid': settings.wechat_appid, 'secret': settings.wechat_secret},
            )
            data = response.json() if response.content else {}
    token = str(data.get('access_token') or '')
    if not token:
        raise WeChatKfError(int(data.get('errcode') or -1), str(data.get('errmsg') or '获取 access_token 失败'))
    await _store_token(token, int(data.get('expires_in') or 7200))
    return token


# ---------------------------------------------------------------------------
# 通用请求
# ---------------------------------------------------------------------------
async def _call(path: str, *, method: str = 'POST', payload: dict | None = None, params: dict | None = None, raw: bytes | None = None, content_type: str = 'application/json') -> dict:
    token = await access_token()
    query = dict(params or {})
    query['access_token'] = token
    async with httpx.AsyncClient(timeout=30) as client:
        if raw is not None:
            response = await client.post(f'{WECHAT_API_BASE}{path}', params=query, content=raw, headers={'Content-Type': content_type})
        elif method == 'GET':
            response = await client.get(f'{WECHAT_API_BASE}{path}', params=query)
        else:
            response = await client.post(f'{WECHAT_API_BASE}{path}', params=query, json=payload or {})
    if response.headers.get('content-type', '').startswith(('image/', 'audio/', 'video/', 'application/octet')):
        # /cgi-bin/media/get 成功时直接回文件字节
        return {'_binary': response.content, '_content_type': response.headers.get('content-type', '')}
    data = response.json() if response.content else {}
    code = int(data.get('errcode') or 0)
    if code:
        # 40001/42001 是 token 失效，强刷一次再给上层一次机会
        if code in (40001, 40014, 42001):
            await access_token(force=True)
            raise WeChatKfError(code, f'{data.get("errmsg") or "access_token 已失效"}（已自动刷新，请重试）', path)
        raise WeChatKfError(code, str(data.get('errmsg') or '微信接口返回错误'), path)
    return data


# ---------------------------------------------------------------------------
# 客服账号管理
# ---------------------------------------------------------------------------
def normalize_account(prefix: str) -> str:
    """按官方格式补全客服账号：`前缀@小程序微信号`。

    官方要求前缀最多 10 字符、只能是英文/数字/下划线；这里顺手把非法字符剔掉，
    避免运营在后台填了中文以后收到一个看不懂的 errcode 40003。
    """
    raw = (prefix or '').strip()
    if '@' in raw:
        return raw
    cleaned = ''.join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch == '_'))[:10]
    return cleaned


async def add_account(kf_account: str, nickname: str) -> dict:
    return await _call('/customservice/kfaccount/add', payload={'kf_account': kf_account, 'nickname': nickname[:16]})


async def del_account(kf_account: str) -> dict:
    return await _call('/customservice/kfaccount/del', payload={'kf_account': kf_account})


async def list_accounts() -> list[dict]:
    data = await _call('/cgi-bin/customservice/getkflist')
    return [_account_payload(item) for item in (data.get('kf_list') or [])]


async def list_online_accounts() -> list[dict]:
    data = await _call('/cgi-bin/customservice/getonlinekflist')
    return [_account_payload(item) for item in (data.get('kf_online_list') or [])]


def _account_payload(item: dict) -> dict:
    return {
        'kf_account': str(item.get('kf_account') or ''),
        'kf_id': str(item.get('kf_id') or ''),
        'kf_nick': str(item.get('kf_nick') or ''),
        'kf_headimgurl': str(item.get('kf_headimgurl') or ''),
        'kf_openid': str(item.get('kf_openid') or ''),
        'online': bool(item.get('status') in (1, '1', True)) if item.get('status') is not None else False,
        'accepted_case': int(item.get('accepted_case') or 0),
    }


async def set_admin(kf_openid: str) -> dict:
    return await _call('/customservice/kfaccount/setadmin', method='GET', params={'kf_openid': kf_openid})


async def cancel_admin(kf_openid: str) -> dict:
    return await _call('/customservice/kfaccount/canceladmin', method='GET', params={'kf_openid': kf_openid})


# ---------------------------------------------------------------------------
# 客服消息
# ---------------------------------------------------------------------------
def _decorate(payload: dict, kf_account: str = '', *, appid: str = '', ai_msg: bool = False) -> dict:
    """补上可选字段：指定客服账号、小程序卡片必需的 appid、AI 内容标注。

    官方对 miniprogrampage 的 appid 是「必填」；对 AI 生成的内容，在消息下方加一句
    「内容由第三方AI生成」，是我们这类产品该主动做的事，不是可选项。
    """
    if kf_account:
        payload['customservice'] = {'kf_account': kf_account}
    if appid and payload.get('miniprogrampage') is not None:
        payload['miniprogrampage']['appid'] = appid
    if ai_msg:
        payload['aimsgcontext'] = {'is_ai_msg': 1}
    return payload


async def send_text(openid: str, content: str, kf_account: str = '', *, ai_msg: bool = False) -> dict:
    payload: dict[str, Any] = {'touser': openid, 'msgtype': 'text', 'text': {'content': content}}
    return await _call('/cgi-bin/message/custom/send', payload=_decorate(payload, kf_account, ai_msg=ai_msg))


async def send_image(openid: str, media_id: str, kf_account: str = '', *, ai_msg: bool = False) -> dict:
    payload: dict[str, Any] = {'touser': openid, 'msgtype': 'image', 'image': {'media_id': media_id}}
    return await _call('/cgi-bin/message/custom/send', payload=_decorate(payload, kf_account, ai_msg=ai_msg))


async def send_miniprogrampage(openid: str, title: str, pagepath: str, thumb_media_id: str, appid: str = '', kf_account: str = '') -> dict:
    """小程序卡片：用户在客服会话里点一下就能回到小程序的具体页面。

    这是把「客服会话」和「小程序」连起来的关键能力，所以单独包一层。
    """
    payload: dict[str, Any] = {
        'touser': openid,
        'msgtype': 'miniprogrampage',
        'miniprogrampage': {'title': title[:64], 'pagepath': pagepath, 'thumb_media_id': thumb_media_id},
    }
    # appid 官方标的是必填：不传卡片就点不开，所以这里默认回退到自己的 appid
    return await _call('/cgi-bin/message/custom/send', payload=_decorate(payload, kf_account, appid=appid or settings.wechat_appid))


async def send_news(openid: str, articles: list[dict], kf_account: str = '') -> dict:
    # 小程序客服的图文消息官方限 1 条，多传会直接报 45008
    payload: dict[str, Any] = {'touser': openid, 'msgtype': 'news', 'news': {'articles': articles[:1]}}
    return await _call('/cgi-bin/message/custom/send', payload=_decorate(payload, kf_account))


async def send_raw(payload: dict) -> dict:
    return await _call('/cgi-bin/message/custom/send', payload=payload)


async def typing(openid: str, command: str = 'Typing') -> dict:
    """客服输入状态：command 取 Typing（正在输入）/ CancelTyping（取消）。"""
    normalized = 'Typing' if command == 'Typing' else 'CancelTyping'
    return await _call('/cgi-bin/message/custom/business/typing', payload={'touser': openid, 'command': normalized})


# ---------------------------------------------------------------------------
# 临时素材
# ---------------------------------------------------------------------------
async def upload_temp_media(content: bytes, filename: str, media_type: str = 'image') -> dict:
    token = await access_token()
    files = {'media': (filename or 'upload.bin', content, 'application/octet-stream')}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f'{WECHAT_API_BASE}/cgi-bin/media/upload',
            params={'access_token': token, 'type': media_type},
            files=files,
        )
    data = response.json() if response.content else {}
    if int(data.get('errcode') or 0):
        raise WeChatKfError(int(data['errcode']), str(data.get('errmsg') or '上传临时素材失败'), '/cgi-bin/media/upload')
    return {'media_id': str(data.get('media_id') or ''), 'type': str(data.get('type') or media_type), 'created_at': int(data.get('created_at') or 0)}


async def get_temp_media(media_id: str) -> dict:
    return await _call('/cgi-bin/media/get', method='GET', params={'media_id': media_id})


# ---------------------------------------------------------------------------
# 消息推送：验签 / 安全模式加解密 / 入站消息解析
#
# MP 后台「开发管理 → 消息推送」把用户与客服的往来消息推到我们的回调 URL：
#   GET  保存前验签：sha1(sort(token,timestamp,nonce)) == signature，原样回 echostr
#   POST 收消息：明文模式直接是 XML；安全模式是 <Encrypt>，用 AES-256-CBC 解
#   安全模式的签名多一个参数：sha1(sort(token,timestamp,nonce,encrypt)) == msg_signature
# 这两段是纯计算，放在这里而不是 main.py，测试才能不依赖 HTTP 层直接跑。
# ---------------------------------------------------------------------------
def _token() -> str:
    return (settings.wechat_kf_token or '').strip()


def _aes_key() -> bytes:
    raw = (settings.wechat_kf_aes_key or '').strip()
    if len(raw) != 43:
        raise WeChatKfError(-2, 'WECHAT_KF_AES_KEY 应为 43 位 EncodingAESKey（安全模式必填）')
    try:
        return base64.b64decode(raw + '=')
    except Exception as exc:  # 配置写错时要给运营看得懂的提示
        raise WeChatKfError(-2, f'WECHAT_KF_AES_KEY 不是合法的 EncodingAESKey：{exc}') from exc


def _sha1(*parts: str) -> str:
    return hashlib.sha1(''.join(sorted(parts)).encode('utf-8')).hexdigest()


def push_ready() -> bool:
    """回调能不能用：至少要 Token；开安全模式还要 AESKey。"""
    return bool(_token())


def verify_url_signature(signature: str, timestamp: str, nonce: str) -> bool:
    if not _token() or not signature:
        return False
    return _sha1(_token(), timestamp or '', nonce or '') == signature


def verify_encrypt_signature(msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
    if not _token() or not msg_signature or not encrypt:
        return False
    return _sha1(_token(), timestamp or '', nonce or '', encrypt) == msg_signature


def decrypt_message(encrypt: str) -> dict:
    """解开安全模式密文，回 {'xml': 明文, 'appid': 密文里的 appid}。

    明文结构（官方定义）：random(16B) + msg_len(4B 网络字节序) + msg + appid。
    末段 appid 是自己的小程序 appid，校验一遍就能挡掉拿别人密文来试探的请求。
    """
    key = _aes_key()
    try:
        raw = base64.b64decode(encrypt)
    except Exception as exc:
        raise WeChatKfError(-3, f'密文不是合法 base64：{exc}') from exc
    if len(raw) % 16:
        raise WeChatKfError(-3, '密文长度不是 16 的整数倍')
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    try:
        content = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise WeChatKfError(-3, 'EncodingAESKey 与后台不一致，解密失败') from exc
    if len(content) < 20:
        raise WeChatKfError(-3, '解密结果长度异常')
    msg_len = struct.unpack('!I', content[16:20])[0]
    if 20 + msg_len > len(content):
        raise WeChatKfError(-3, '解密结果里的消息长度异常')
    xml = content[20:20 + msg_len].decode('utf-8', errors='replace')
    return {'xml': xml, 'appid': content[20 + msg_len:].decode('utf-8', errors='replace')}


def encrypt_message(plain: str, *, appid: str = '', random_bytes: bytes | None = None) -> str:
    """把回复 XML 按安全模式加密，填入回包 <Encrypt>。"""
    key = _aes_key()
    body = (plain or '').encode('utf-8')
    payload = (random_bytes or os.urandom(16)) + struct.pack('!I', len(body)) + body + (appid or settings.wechat_appid or '').encode('utf-8')
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(payload) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode('utf-8')


def wrap_encrypted_reply(encrypt: str, signature: str, timestamp: str, nonce: str) -> str:
    return (
        '<xml>'
        f'<Encrypt><![CDATA[{encrypt}]]></Encrypt>'
        f'<MsgSignature><![CDATA[{signature}]]></MsgSignature>'
        f'<TimeStamp>{timestamp}</TimeStamp>'
        f'<Nonce><![CDATA[{nonce}]]></Nonce>'
        '</xml>'
    )


def reply_encrypt(plain: str, timestamp: str, nonce: str) -> str:
    """加密 + 用自己的 Token 重算 msg_signature，返回可直接吐给微信的回包。"""
    encrypt = encrypt_message(plain)
    return wrap_encrypted_reply(encrypt, _sha1(_token(), timestamp, nonce, encrypt), timestamp, nonce)


def _plain_summary(msg: dict) -> str:
    """给非文本消息写一句人话摘要，运营在后台列表里一眼能看懂。"""
    msg_type = msg.get('msg_type') or ''
    if msg_type == 'text':
        return str(msg.get('content') or '')
    if msg_type == 'image':
        return '[图片]'
    if msg_type == 'voice':
        extra = f" {msg['recognition']}" if msg.get('recognition') else ''
        return f'[语音]{extra}'
    if msg_type == 'video':
        return '[视频]'
    if msg_type == 'shortvideo':
        return '[小视频]'
    if msg_type == 'location':
        return f"[位置] {msg.get('label') or ''}"
    if msg_type == 'link':
        return f"[链接] {msg.get('title') or ''} {msg.get('url') or ''}".strip()
    if msg_type == 'event':
        return f"[事件] {msg.get('event') or ''}"
    return f'[{msg_type or "未知消息"}]'


def parse_message(xml_text: str) -> dict:
    """把入站消息 XML 解析成扁平字典，字段统一成小写下划线。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise WeChatKfError(-4, f'入站消息不是合法 XML：{exc}') from exc

    def text(tag: str) -> str:
        return (root.findtext(tag) or '').strip()

    message = {
        'to_user': text('ToUserName'),
        'from_user': text('FromUserName'),
        'create_time': text('CreateTime'),
        'msg_type': text('MsgType'),
        'msg_id': text('MsgId'),
        'event': text('Event'),
        'event_key': text('EventKey'),
        'session_from': text('SessionFrom'),
        'kf_account': text('KfAccount'),
        'pic_url': text('PicUrl'),
        'media_id': text('MediaId') or text('MediaID'),
        'format': text('Format'),
        'recognition': text('Recognition'),
        'thumb_media_id': text('ThumbMediaId'),
        'title': text('Title'),
        'description': text('Description'),
        'url': text('Url'),
        'latitude': text('Latitude'),
        'longitude': text('Longitude'),
        'label': text('Label'),
        'status': text('Status'),
        'content': text('Content'),
    }
    message['plain'] = _plain_summary(message)
    return message


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    """被动回复 XML（5 秒内直接回包）。超过 5 秒应改用客服消息接口主动下发。"""
    safe = (content or '').replace(']]>', ']]&gt;')
    return (
        '<xml>'
        f'<ToUserName><![CDATA[{to_user}]]></ToUserName>'
        f'<FromUserName><![CDATA[{from_user}]]></FromUserName>'
        f'<CreateTime>{int(time.time())}</CreateTime>'
        '<MsgType><![CDATA[text]]></MsgType>'
        f'<Content><![CDATA[{safe}]]></Content>'
        '</xml>'
    )
