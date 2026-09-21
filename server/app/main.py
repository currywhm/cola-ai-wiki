import asyncio
import base64
import hashlib
import json
import math
import logging
import mimetypes
import re
import secrets
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from .config import settings
from .db import (
    close_connections,
    close_db_pool,
    close_request_connections,
    connect,
    decode_sources,
    fetchall,
    fetchone,
    init_db,
    row_dict,
    take_request_connections,
)
from .schemas import ArticleImportRequest, AssetResolveRequest, ChatRequest, ConversationPinUpdate, DocumentMove, DocumentTagUpdate, FolderCreate, KnowledgeCreate, LoginRequest, PayCreateRequest, PlanReviewRequest, PreferenceUpdate, ProfileUpdate, ShareCreate, ShareToKnowledge, SkillBuildRequest, SkillEnhanceRequest, SkillFlagUpdate, SkillForm, SkillPublishUpdate, UploadCompleteRequest, UploadDirectRequest
from .schemas import KnowledgePublishUpdate, KnowledgeSubscriptionCreate

from .security import create_token, current_user, current_user_optional, rate_limit
from .services.documents import ALLOWED_SUFFIXES, extract_text, split_chunks
from .services import content as content_service
from .services import knowledge_policy
from .services import skill_build
from .services import jobs as jobs_service
from .services import artifacts as artifact_service
from .services import storage
from .services.wechat_article import ArticleFetchError, build_document_html, fetch_wechat_article
from .services import wechat_kf as kf_service
from .services.wechat_auth import (
    WeChatAuthError,
    exchange_code_for_session,
    validate_wechat_credentials,
)
from .services.llm import stream_answer
from .services.harness import (
    SKILL_LABELS,
    configured as harness_configured,
    stream_raw_prompt,
    cancel_user_runtime,
    select_turn_skill_ids,
    warm as harness_warm,
)
from .services.organizer import organize_document
from .services.credits import estimate_usage, quote_turn, total_tokens
from .services.virtual_pay import calc_pay_sig, calc_user_signature, query_order, sign_data, virtual_configured, virtual_product

# 免费试用：注册当天起 30 天倒计时，期间 300MB，另可新建 2 个个人知识库。
# 会员按租期开通（月/季/年）：Plus 10GB / Pro 30GB，另外区别在资料库数量、单文件大小与每月问答额度。
TRIAL_DAYS = 30
FREE_STORAGE_BYTES = 300 * 1024 * 1024
PLUS_STORAGE_BYTES = 10 * 1024 * 1024 * 1024
PRO_STORAGE_BYTES = 30 * 1024 * 1024 * 1024
# 额度单位是「积分」，不是问答次数：一轮扣多少分取决于这一轮真实烧了多少 token
# （deepseek-flash 官方价 × 1.5 倍加价，换算规则见 services/credits.py）。
# 三档额度按「一轮典型问答约 3 积分」折算，与原「每月 200 / 1000 / 5000 次」等价。
FREE_CREDITS_AFTER_TRIAL = 150

MEMBERSHIP_LIMITS = {
    # 知识库配额只算「自己新建的」：系统默认的「微信用户的知识库」、共享收件箱与订阅镜像都不占名额，
    # 所以免费试用说的是「除默认库外还能再建 1 个」。
    'free': {'label': '免费试用', 'knowledge_bases': 1, 'storage_bytes': FREE_STORAGE_BYTES, 'monthly_credits': 600, 'max_file_bytes': 50 * 1024 * 1024, 'skills': 1},
    'plus': {'label': 'Plus 会员', 'knowledge_bases': 10, 'storage_bytes': PLUS_STORAGE_BYTES, 'monthly_credits': 3000, 'max_file_bytes': 100 * 1024 * 1024, 'skills': 5},
    'pro': {'label': 'Pro 会员', 'knowledge_bases': 50, 'storage_bytes': PRO_STORAGE_BYTES, 'monthly_credits': 15000, 'max_file_bytes': 300 * 1024 * 1024, 'skills': 10},
}

PLAN_CATALOG = {
    'plus_monthly': (1200, 'Plus 会员月度', 'plus', 31), 'plus_quarterly': (3000, 'Plus 会员季度', 'plus', 92), 'plus_yearly': (10800, 'Plus 会员年度', 'plus', 365),
    'pro_monthly': (2900, 'Pro 会员月度', 'pro', 31), 'pro_quarterly': (7500, 'Pro 会员季度', 'pro', 92), 'pro_yearly': (25800, 'Pro 会员年度', 'pro', 365),
}

DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'
DEFAULT_KNOWLEDGE_DESCRIPTION = '你的默认资料空间'
DEFAULT_KNOWLEDGE_ICON = 'library_books'

def membership_for_user(row: Any) -> str:
    value = str(row['membership'] or 'free') if row else 'free'
    expires = str(row['membership_expires_at'] or '') if row else ''
    if value not in MEMBERSHIP_LIMITS or (expires and expires <= now()): return 'free'
    return value

def limits_for_user(row: Any) -> dict:
    tier = membership_for_user(row)
    return {'tier': tier, **MEMBERSHIP_LIMITS[tier]}


def parse_moment(value: Any) -> datetime | None:
    """把库里的 ISO 时间串解析成带时区的 datetime；坏数据返回 None。"""
    try:
        parsed = datetime.fromisoformat(str(value or ''))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def openid_fingerprint(openid: str) -> str:
    """注销标记用的加盐指纹：不可逆、不含可识别信息，只用于防止反复注销刷试用与额度。"""
    return hashlib.sha256(f'{settings.jwt_secret}:{openid}'.encode('utf-8')).hexdigest()


def trial_state(row: Any) -> dict:
    """新用户免费试用倒计时：注册起 TRIAL_DAYS 天，会员不参与倒计时。"""
    if row is not None and 'trial_used' in row.keys() and int(row['trial_used'] or 0):
        # 注销过又回来的账号：不再发一次试用（否则反复注销就能无限续试用与额度）
        return {'active': False, 'days_left': 0, 'ends_at': ''}
    created = parse_moment(row['created_at']) if row is not None and 'created_at' in row.keys() else None
    if not created:
        return {'active': True, 'days_left': TRIAL_DAYS, 'ends_at': ''}
    ends_at = created + timedelta(days=TRIAL_DAYS)
    seconds_left = (ends_at - datetime.now(timezone.utc)).total_seconds()
    return {'active': seconds_left > 0, 'days_left': max(0, math.ceil(seconds_left / 86400)), 'ends_at': ends_at.isoformat()}


def human_size(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        size = value / (1024 * 1024 * 1024)
        return f'{size:.0f}GB' if size >= 10 else f'{size:.1f}GB'
    return f'{max(1, round(value / (1024 * 1024)))}MB'


def period_start_iso() -> str:
    """本自然月起点（北京时间）：积分额度和原来的问答次数一样按月归零，不结转。

    用户理解的「每月 1 日」是北京时间 1 日 0 点；若按 UTC 算月，额度要到 1 日上午 8 点才恢复，
    与《数据管理》里写明的口径不符，所以这里固定用东八区。
    """
    beijing = datetime.now(timezone(timedelta(hours=8))).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return beijing.astimezone(timezone.utc).isoformat()


async def credits_this_month(db, user_id: str) -> int:
    """本月已消耗的积分：直接读积分流水合计，所以「扣了多少」永远能对回具体轮次与 token。"""
    row = await fetchone(db, "SELECT COALESCE(SUM(credits),0) AS used FROM usage_logs WHERE user_id=? AND created_at>=?", (user_id, period_start_iso()))
    return int((row['used'] if row else 0) or 0)


async def charge_turn(db, user_id: str, conversation_id: str, message_id: str, model: str, usage: Any) -> dict:
    """把一轮问答的真实用量落成积分流水（用户价 = 成本 × 1.5），并返回本轮账日。"""
    quote = quote_turn(usage)
    tokens = quote['tokens']
    await db.execute(
        "INSERT INTO usage_logs(id,user_id,conversation_id,message_id,model,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,cost_yuan,credits,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, user_id, conversation_id, message_id, model,
         tokens['input_tokens'], tokens['cache_read_tokens'], tokens['cache_write_tokens'], tokens['output_tokens'],
         quote['cost_yuan'], quote['credits'], now()),
    )
    await db.commit()
    return quote


async def owned_skill_count(db, user_id: str) -> int:
    """自己创建（不含内置）的技能数量，用于会员技能配额。"""
    row = await fetchone(db, "SELECT COUNT(*) AS used FROM skills WHERE user_id=? AND status='active' AND source!='builtin'", (user_id,))
    return int(row['used'] or 0) if row else 0


def account_state(row: Any, credits_used: int = 0, skills_used: int = 0) -> dict:
    """会员 + 试用倒计时 + 本月积分额度 + 技能配额合成一份状态，前后端共用同一套口径。"""
    tier = membership_for_user(row)
    limits = MEMBERSHIP_LIMITS[tier]
    trial = trial_state(row)
    trial_active = tier == 'free' and trial['active']
    # 试用期与会员同档额度；试用结束后收紧，避免注册即弃用
    quota = limits['monthly_credits'] if (tier != 'free' or trial_active) else FREE_CREDITS_AFTER_TRIAL
    expires_at = str(row['membership_expires_at'] or '') if row is not None and 'membership_expires_at' in row.keys() else ''
    days_left = 0
    if tier != 'free':
        expiry = parse_moment(expires_at)
        days_left = max(0, math.ceil((expiry - datetime.now(timezone.utc)).total_seconds() / 86400)) if expiry else 0
    else:
        days_left = trial['days_left']
    return {
        'tier': tier,
        'label': limits['label'],
        'trial': {'active': trial_active, 'days_left': trial['days_left'], 'days': TRIAL_DAYS, 'ends_at': trial['ends_at']},
        'period': {'days_left': days_left, 'expires_at': expires_at},
        'limits': {'knowledge_bases': limits['knowledge_bases'], 'storage_bytes': limits['storage_bytes'], 'max_file_bytes': limits['max_file_bytes'], 'monthly_credits': quota, 'skills': limits['skills']},
        'quota': {'credits_limit': quota, 'credits_used': credits_used, 'credits_left': max(0, quota - credits_used), 'skills_limit': limits['skills'], 'skills_used': skills_used, 'skills_left': max(0, limits['skills'] - skills_used)},
        'entitlements': {
            'can_upload': tier != 'free' or trial_active,
            'can_create_knowledge': tier != 'free' or trial_active,
            'can_ask': credits_used < quota,
            'member': tier != 'free',
        },
    }


def stored_preferences(raw: Any) -> dict:
    """把 users.preferences 解析成字典；损坏或为空时回退成空字典。"""
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def ensure_default_knowledge(db, user_id: str):
    """Return the user's default knowledge base, creating it exactly once.

    Login and the knowledge list endpoint both call this function.  That keeps
    existing accounts compatible with the new default workspace while making
    repeated requests idempotent.
    """
    existing = await fetchone(
        db,
        "SELECT * FROM knowledge_bases WHERE user_id=? AND name=? AND status='active' ORDER BY created_at ASC LIMIT 1",
        (user_id, DEFAULT_KNOWLEDGE_NAME),
    )
    if existing:
        return existing
    # Older installations may already have a single user-owned workspace under
    # another name. Promote it to the default workspace so introducing this
    # invariant does not consume a second free-tier slot or orphan documents.
    legacy = await fetchone(
        db,
        "SELECT * FROM knowledge_bases WHERE user_id=? AND status='active' ORDER BY created_at ASC LIMIT 1",
        (user_id,),
    )
    if legacy:
        await db.execute(
            "UPDATE knowledge_bases SET name=?,description=?,icon=?,updated_at=? WHERE id=?",
            (
                DEFAULT_KNOWLEDGE_NAME,
                DEFAULT_KNOWLEDGE_DESCRIPTION,
                DEFAULT_KNOWLEDGE_ICON,
                now(),
                legacy["id"],
            ),
        )
        return await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=?", (legacy["id"],))
    timestamp = now()
    knowledge_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO knowledge_bases(id,user_id,name,description,icon,document_count,visibility,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            knowledge_id,
            user_id,
            DEFAULT_KNOWLEDGE_NAME,
            DEFAULT_KNOWLEDGE_DESCRIPTION,
            DEFAULT_KNOWLEDGE_ICON,
            0,
            "private",
            "active",
            timestamp,
            timestamp,
        ),
    )
    return await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=?", (knowledge_id,))

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    await validate_wechat_credentials()
    await init_db()
    # Backfill the default workspace for accounts created before this rule was
    # introduced. The helper is idempotent, so restarts never create duplicates.
    db = await connect()
    users = await fetchall(db, "SELECT id FROM users WHERE status='active'")
    for user in users:
        await ensure_default_knowledge(db, user["id"])
    # 进程重启后，旧的 running 已不可能继续写回；标记为 interrupted，避免前端误以为任务仍在执行。
    stamp = now()
    await db.execute("UPDATE chat_runs SET status='interrupted',error='服务重启，任务已中断',revision=revision+1,updated_at=?,finished_at=? WHERE status='running'", (stamp, stamp))
    await db.commit()
    await db.close()
    await jobs_service.mark_interrupted()
    # 使用技巧等内容：源文件在 content/ 目录，运行时统一从数据库读。
    # 首次启动发现库里没有内容时自动导入一次（可重复执行，指纹一致即跳过），
    # 因此部署时既有显式脚本这一步，也不会因为漏跑脚本导致接口 503。
    try:
        from .services.content_import import ensure_content_imported, import_warnings
        imported = await ensure_content_imported()
        for item in imported:
            print(f"[content] 已从本地源文件导入 {item['slug']}：{item['entries']} 篇正文、{item['assets']} 张配图（版本 {item['version']}）", flush=True)
        for warning in import_warnings():
            print(f"[content][合规待办] {warning}", flush=True)
    except content_service.ContentError as exc:
        print(f"[content] 内容未导入：{exc}", flush=True)
    if harness_configured():
        print('[harness] official sdk profile enabled; runtimes start on first turn', flush=True)
    reconcile_task = None
    if settings.wechat_payment_mode == 'virtual':
        # 发货推送丢失时的第二确认路径：每 5 分钟扫一次 2 分钟前创建、24 小时内
        # 的待支付订单，向平台查单，已支付则走同一套发货逻辑补发权益。
        async def reconcile_pay_orders():
            while True:
                try:
                    cutoff_new = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
                    cutoff_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                    db = await connect()
                    pending = await fetchall(db, "SELECT p.*,u.openid FROM pay_orders p JOIN users u ON u.id=p.user_id WHERE p.status='pending' AND p.created_at<? AND p.created_at>?", (cutoff_new, cutoff_old))
                    await db.close()
                    for order in pending:
                        try:
                            result = await query_order(order['openid'], order['out_trade_no'])
                            if order_paid_marker(result) in PAID_MARKERS:
                                db = await connect()
                                await deliver_membership(db, order, str(result.get('wx_order_id') or result.get('order_id') or ''), int(order['quantity'] or 1))
                                await db.commit(); await db.close()
                        except Exception as exc:
                            print(f'[pay] reconcile order {order["out_trade_no"]} failed: {exc}', flush=True)
                except Exception as exc:
                    print(f'[pay] reconcile sweep failed: {exc}', flush=True)
                await asyncio.sleep(300)
        reconcile_task = asyncio.create_task(reconcile_pay_orders())
    yield
    if reconcile_task:
        reconcile_task.cancel()
    await close_db_pool()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list or ["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def release_request_database_connections(request: Request, call_next):
    """Return pooled MySQL connections after the response body has finished."""
    try:
        response = await call_next(request)
    except Exception:
        await close_request_connections()
        raise

    # 小程序走容器通道时返回包上限 1000KiB：超过就会被截断，这里提前留痕便于上线前扫一遍。
    length = response.headers.get('content-length') or ''
    if length.isdigit() and str(request.url.path).startswith('/api/') and int(length) > RESPONSE_SIZE_WARN_BYTES:
        print(f"[api] 返回包偏大 {length} 字节：{request.url.path}", flush=True)

    # StreamingResponse has not consumed its async generator when call_next()
    # returns. Keep this request's connections attached until the last chunk is
    # sent (or the client disconnects), otherwise SSE handlers receive a closed
    # MySQL connection on their first yield.
    databases = take_request_connections()
    iterator = getattr(response, 'body_iterator', None)
    if iterator is None or not databases:
        await close_connections(databases)
        return response

    async def tracked_body():
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            await close_connections(databases)

    response.body_iterator = tracked_body()
    return response


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 任务队列里也要能排「后台的整理任务」：请求之外没有 BackgroundTasks，用协程自己管。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def schedule_background(coro) -> None:
    """在请求之外启动后台协程：异常只记日志，不打断外层任务。"""
    task = asyncio.get_event_loop().create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def settle(item: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(item)
        if not item.cancelled() and item.exception():
            logging.getLogger('app.background').exception('后台任务失败', exc_info=item.exception())

    task.add_done_callback(settle)


async def touch_knowledge_used(db, knowledge_id: str) -> None:
    """记一次「使用」：打开某个知识库、或在它里面提问都算。

    单独存 last_used_at 而不是复用 updated_at——后者只在资料增删改时更新，
    拿它给「最近知识库」排序，会把刚看过的库排到后面。
    调用方负责 commit（与同一次请求里已有的写操作合并提交）。
    """
    if not knowledge_id:
        return
    await db.execute("UPDATE knowledge_bases SET last_used_at=? WHERE id=?", (now(), knowledge_id))


PAID_MARKERS = {'PAID', 'SUCCESS', 'PAY_SUCCESS', '2'}


# ---- 知识库邀请分享：一条链接只认第一个接受的好友 ----

KNOWLEDGE_SHARE_TTL_DAYS = 7


def knowledge_view(row: Any, *, live_document_count: int | None = None, source_name: str = '', source_missing: bool = False, subscribed: bool = False, subscription_source: str = '') -> dict:
    """知识库统一视图：自己的库与好友共享过来的库用同一组字段表达。

    共享过来的库（mirror_of 非空）带 read_only=true 与来源名称，前端据此隐藏
    「导入文件 / 删除 / 移动」等写入入口，避免收件人改到分享者的资料。
    """
    data = row_dict(row) or {}
    avatar_id = str(data.get('mirror_of') or data.get('id') or '')
    avatar_url = f"/api/knowledge-avatars/{avatar_id}?v={hashlib.sha1(str(data.get('updated_at') or '').encode()).hexdigest()[:10]}" if data.get('avatar') and avatar_id else ''
    mirror_of = str(data.get('mirror_of') or '')
    name = str(data.get('name') or '')
    count = live_document_count if live_document_count is not None else int(data.get('document_count') or 0)
    return {
        'id': data.get('id') or '',
        'user_id': data.get('user_id') or '',
        'name': name,
        'description': data.get('description') or '',
        'icon': data.get('icon') or DEFAULT_KNOWLEDGE_ICON,
        'avatar': avatar_url,
        'document_count': int(count or 0),
        'visibility': data.get('visibility') or 'private',
        'category': data.get('category') or '',
        'subscribers': int(data.get('subscribers') or 0),
        'published': (data.get('visibility') or '') == 'public',
        'published_at': data.get('published_at') or '',
        'status': data.get('status') or 'active',
        'created_at': data.get('created_at') or '',
        'updated_at': data.get('updated_at') or '',
        'last_used_at': data.get('last_used_at') or '',
        'shared': bool(mirror_of) and not subscribed,
        'read_only': bool(mirror_of),
        'source_name': source_name,
        'source_missing': bool(source_missing),
        'subscribed': bool(subscribed),
        'subscription_source': subscription_source,
        'inbox': name == SHARED_KNOWLEDGE_NAME,
        'publishable': bool(name) and name not in (DEFAULT_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_NAME) and not mirror_of,
        # 「微信用户的知识库」是每个人的私人默认空间，不允许分享；共享过来的库也不能再转发。
        # 「共享知识库」是收件箱：里面放着别人分享给你的内容，分享出去等于把收件箱给别人看。
        'shareable': bool(name) and name not in (DEFAULT_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_NAME) and not mirror_of,
    }


async def unlink_if_unreferenced(db: Any, storage_path: str) -> None:
    """原文可能被「来源库 + 若干个共享库」同时引用：一条活着的记录都没有了才删磁盘文件。"""
    if not storage_path:
        return
    row = await fetchone(db, "SELECT COUNT(*) AS count FROM documents WHERE storage_path=? AND status!='deleted'", (storage_path,))
    if int((row['count'] if row else 0) or 0) > 0:
        return
    await storage.delete(storage_path)


async def stored_file_response(ref: str, *, filename: str = '', media_type: str = 'application/octet-stream') -> FileResponse:
    """Serve a persisted object while keeping COS/local storage opaque to routes."""
    suffix = Path(filename).suffix if filename else ''
    try:
        path = await storage.materialize(ref, suffix=suffix)
    except storage.StorageError as exc:
        raise HTTPException(404, '文件不存在') from exc
    return FileResponse(
        path,
        filename=filename or None,
        media_type=media_type,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )

async def drop_mirror_document(db: Any, knowledge_id: str, document_id: str, storage_path: str) -> None:
    """删掉共享库里的资料条目与切片；原文归零引用后才从磁盘删。"""
    await db.execute('DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)', (document_id,))
    await db.execute('DELETE FROM chunks WHERE document_id=?', (document_id,))
    await db.execute('DELETE FROM documents WHERE id=? AND knowledge_id=?', (document_id, knowledge_id))
    await unlink_if_unreferenced(db, storage_path)


async def sync_mirror(db: Any, row: Any) -> dict:
    """把「好友共享过来的知识库」与来源库对齐（每次读的时候同步一次）。

    共享库只维护条目、目录与切片，不复制文件字节：来源新增 / 改名 / 删除资料后，
    收件人下次打开看到的就是最新的。来源整库被删掉时，共享库保留但标记失效。
    返回 {'source_name': str, 'source_missing': bool, 'count': int}
    """
    mirror_id = str(row['id'])
    owner_id = str(row['user_id'])
    source_id = str(row['mirror_of'] or '')
    if not source_id:
        return {'source_name': '', 'source_missing': False, 'count': int(row['document_count'] or 0)}
    source = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND status='active'", (source_id,))
    mirror_docs = await fetchall(db, "SELECT id,origin_document_id,storage_path,updated_at FROM documents WHERE knowledge_id=? AND status!='deleted'", (mirror_id,))
    if not source:
        for item in mirror_docs:
            await drop_mirror_document(db, mirror_id, str(item['id']), str(item['storage_path'] or ''))
        await db.execute('DELETE FROM folders WHERE knowledge_id=?', (mirror_id,))
        await db.execute("UPDATE knowledge_bases SET document_count=0,mirror_state='source_missing',updated_at=? WHERE id=?", (now(), mirror_id))
        return {'source_name': '', 'source_missing': True, 'count': 0}
    # 目录先对齐：镜像目录与来源目录一一对应，新增 / 改名 / 删除都跟着走
    mirror_folders = {str(item['origin_folder_id'] or ''): str(item['id']) for item in await fetchall(db, 'SELECT id,origin_folder_id FROM folders WHERE knowledge_id=?', (mirror_id,))}
    folder_map: dict[str, str] = {}
    for folder in await fetchall(db, 'SELECT id,name,created_at,updated_at FROM folders WHERE knowledge_id=? ORDER BY created_at', (source_id,)):
        source_folder_id = str(folder['id'])
        target = mirror_folders.get(source_folder_id, '')
        if target:
            folder_map[source_folder_id] = target
            await db.execute('UPDATE folders SET name=?,updated_at=? WHERE id=?', (str(folder['name']), str(folder['updated_at']), target))
        else:
            target = uuid.uuid4().hex
            folder_map[source_folder_id] = target
            await db.execute('INSERT INTO folders(id,knowledge_id,user_id,name,origin_folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (target, mirror_id, owner_id, str(folder['name']), source_folder_id, str(folder['created_at']), str(folder['updated_at'])))
    keep_folders = set(folder_map.values())
    for item in await fetchall(db, 'SELECT id FROM folders WHERE knowledge_id=?', (mirror_id,)):
        if str(item['id']) not in keep_folders:
            await db.execute('UPDATE documents SET folder_id=? WHERE knowledge_id=? AND folder_id=?', ('', mirror_id, str(item['id'])))
            await db.execute('DELETE FROM folders WHERE id=?', (str(item['id']),))
    # 资料对齐：来源有的补进来（连切片一起），来源已经没有的清掉
    existing = {str(item['origin_document_id'] or ''): item for item in mirror_docs}
    changed = False
    count = 0
    for doc in await fetchall(db, "SELECT * FROM documents WHERE knowledge_id=? AND status!='deleted'", (source_id,)):
        source_document_id = str(doc['id'])
        count += 1
        folder_id = folder_map.get(str(doc['folder_id'] or ''), '')
        target = existing.pop(source_document_id, None)
        if target:
            if str(target['updated_at'] or '') == str(doc['updated_at'] or ''):
                continue
            changed = True
            await db.execute(
                'UPDATE documents SET filename=?,file_type=?,file_size=?,page_count=?,status=?,progress=?,error_message=?,'
                'extracted_text=?,organized_title=?,summary=?,tags_json=?,key_points_json=?,organize_status=?,organize_method=?,'
                'organize_error=?,organized_at=?,folder_id=?,updated_at=? WHERE id=?',
                (str(doc['filename']), str(doc['file_type']), int(doc['file_size'] or 0), int(doc['page_count'] or 0), str(doc['status']),
                 int(doc['progress'] or 0), str(doc['error_message'] or ''), str(doc['extracted_text'] or ''), str(doc['organized_title'] or ''),
                 str(doc['summary'] or ''), str(doc['tags_json'] or '[]'), str(doc['key_points_json'] or '[]'), str(doc['organize_status'] or 'pending'),
                 str(doc['organize_method'] or 'local'), str(doc['organize_error'] or ''), str(doc['organized_at'] or ''), folder_id,
                 str(doc['updated_at']), str(target['id'])),
            )
            continue
        changed = True
        mirror_document_id = uuid.uuid4().hex
        await db.execute(
            'INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,page_count,status,progress,error_message,'
            'extracted_text,organized_title,summary,tags_json,key_points_json,organize_status,organize_method,organize_error,organized_at,folder_id,'
            'origin_document_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mirror_document_id, mirror_id, owner_id, str(doc['filename']), str(doc['file_type']), int(doc['file_size'] or 0), str(doc['storage_path']),
             int(doc['page_count'] or 0), str(doc['status']), int(doc['progress'] or 0), str(doc['error_message'] or ''), str(doc['extracted_text'] or ''),
             str(doc['organized_title'] or ''), str(doc['summary'] or ''), str(doc['tags_json'] or '[]'), str(doc['key_points_json'] or '[]'),
             str(doc['organize_status'] or 'pending'), str(doc['organize_method'] or 'local'), str(doc['organize_error'] or ''),
             str(doc['organized_at'] or ''), folder_id, source_document_id, str(doc['created_at']), str(doc['updated_at'])),
        )
        for chunk in await fetchall(db, 'SELECT content,page_number,chunk_index,created_at FROM chunks WHERE document_id=? ORDER BY chunk_index', (source_document_id,)):
            chunk_id = uuid.uuid4().hex
            page = int(chunk['page_number'] or 1)
            await db.execute('INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)', (chunk_id, mirror_document_id, mirror_id, str(chunk['content']), page, int(chunk['chunk_index'] or 0), str(chunk['created_at'])))
            await db.execute('INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)', (str(chunk['content']), chunk_id, mirror_id, str(doc['filename']), page))
    for leftover in existing.values():
        changed = True
        await drop_mirror_document(db, mirror_id, str(leftover['id']), str(leftover['storage_path'] or ''))
    metadata_changed = any((
        str(source[key] or '') != str(row[key] or '')
        for key in ('name', 'description', 'icon', 'avatar')
    ))
    changed = changed or metadata_changed
    await db.execute(
        "UPDATE knowledge_bases SET name=?,description=?,icon=?,avatar=?,document_count=?,mirror_state='ok',mirror_owner=?,updated_at=? WHERE id=?",
        (str(source['name']), str(source['description'] or ''), str(source['icon'] or DEFAULT_KNOWLEDGE_ICON), str(source['avatar'] or ''), count, str(source['user_id']),
         str(source['updated_at'] or '') if changed else str(row['updated_at'] or ''), mirror_id),
    )
    return {'source_name': str(source['name'] or ''), 'source_missing': False, 'count': count}


async def refresh_subscriber_count(db: Any, source_knowledge_id: str) -> int:
    row = await fetchone(db, 'SELECT COUNT(*) AS count FROM knowledge_subscriptions WHERE source_knowledge_id=?', (source_knowledge_id,))
    count = int((row['count'] if row else 0) or 0)
    await db.execute('UPDATE knowledge_bases SET subscribers=? WHERE id=?', (count, source_knowledge_id))
    return count


async def purge_subscription_mirror(db: Any, mirror_knowledge_id: str, user_id: str) -> None:
    """删除广场订阅产生的只读镜像；来源文件只在没有任何资料引用时才清理。"""
    files = await fetchall(db, 'SELECT storage_path FROM documents WHERE knowledge_id=? AND user_id=?', (mirror_knowledge_id, user_id))
    await db.execute('DELETE FROM chunks_fts WHERE knowledge_id=?', (mirror_knowledge_id,))
    await db.execute('DELETE FROM conversations WHERE knowledge_id=? AND user_id=?', (mirror_knowledge_id, user_id))
    await db.execute('DELETE FROM knowledge_bases WHERE id=? AND user_id=?', (mirror_knowledge_id, user_id))
    for row in files:
        await unlink_if_unreferenced(db, str(row['storage_path'] or ''))


async def subscription_source_id(db: Any, user_id: str, mirror_knowledge_id: str) -> str:
    row = await fetchone(db, 'SELECT source_knowledge_id FROM knowledge_subscriptions WHERE user_id=? AND mirror_knowledge_id=?', (user_id, mirror_knowledge_id))
    return str(row['source_knowledge_id'] or '') if row else ''


async def writable_knowledge(db: Any, knowledge_id: str, user_id: str) -> Any:
    """当前用户名下可写的知识库；好友共享过来的库是只读镜像，不允许新增 / 移动 / 删除。"""
    kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb:
        raise HTTPException(404, '知识库不存在')
    if str(kb['mirror_of'] or ''):
        raise HTTPException(403, f"「{kb['name']}」是好友共享给你的知识库，只能阅读和提问，不能改里面的资料")
    return kb


async def writable_knowledge_or_close(db: Any, knowledge_id: str, user_id: str) -> Any:
    """写入口的统一前置检查：拒绝时把连接一并收掉，不在异常路径上漏连接。"""
    try:
        return await writable_knowledge(db, knowledge_id, user_id)
    except HTTPException:
        await db.close()
        raise


def order_paid_marker(result: dict) -> str:
    """Normalize the paid marker across the different field names used by xpay query_order."""
    return str(result.get('status') or result.get('order_status') or result.get('pay_status') or result.get('trade_state') or '').upper()


async def deliver_membership(db, order, wx_order_id: str, quantity: int) -> None:
    """Mark the order paid/delivered and grant (or extend) the membership.

    Shared by the platform deliver push, the manual query fallback and the
    periodic reconciler, so every entry point behaves identically. Renewal
    extends from the current expiry when the same tier is still active,
    instead of resetting the clock and eating the remaining days.
    """
    paid_at = now()
    await db.execute(
        "UPDATE pay_orders SET status='paid',paid_at=COALESCE(NULLIF(paid_at,''),?),wx_order_id=COALESCE(NULLIF(?,''),wx_order_id),quantity=?,deliver_status='delivered',delivered_at=? WHERE out_trade_no=?",
        (paid_at, wx_order_id, max(quantity, 1), paid_at, order['out_trade_no']),
    )
    _amount, _description, tier, days = PLAN_CATALOG.get(order['plan'], (0, '', 'free', 0))
    if tier == 'free' or days <= 0:
        return
    base = datetime.now(timezone.utc)
    user = await fetchone(db, "SELECT * FROM users WHERE id=?", (order['user_id'],))
    if user and str(user['membership'] or '') == tier:
        try:
            current_expiry = datetime.fromisoformat(str(user['membership_expires_at'] or ''))
            if current_expiry.tzinfo is None:
                current_expiry = current_expiry.replace(tzinfo=timezone.utc)
            if current_expiry > base:
                base = current_expiry
        except ValueError:
            pass
    await db.execute(
        "UPDATE users SET membership=?,membership_expires_at=? WHERE id=?",
        (tier, (base + timedelta(days=days * max(quantity, 1))).isoformat(), order['user_id']),
    )


def fts_literal(query: str) -> str:
    # User input is a phrase, not FTS query syntax (quotes, minus, OR, etc.).
    return '"' + query.replace('"', '""') + '"'


_TERM_SPLIT = re.compile(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def query_terms(query: str, limit: int = 24) -> list[str]:
    """把用户问题拆成可用于检索的词。

    FTS5 默认分词器会把一整串连续汉字当成一个整词，中文问句几乎永远命不中，
    所以这里退回到子串检索：按空白与标点切段，长段再切成重叠二元组，
    这样「六险二金」「中粮集团」这类片段都能被命中。
    """
    terms: list[str] = []
    for piece in _TERM_SPLIT.split(query or ''):
        if not piece:
            continue
        if len(piece) <= 6:
            terms.append(piece)
            continue
        terms.extend(piece[i:i + 2] for i in range(len(piece) - 1))
    seen: set[str] = set()
    picked: list[str] = []
    for term in terms:
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        picked.append(term)
    return picked[:limit]


async def retrieve_chunks(db, query: str, user_id: str, knowledge_id: str | None = None, folder_id: str | None = None, limit: int = 5) -> list[dict]:
    """按命中词数给切片打分；命不中就是命不中，不再拿「最近几条」冒充出处。"""
    terms = query_terms(query)
    if not terms:
        return []
    score_sql = ' + '.join("CASE WHEN instr(lower(c.content),lower(?))>0 THEN 1 ELSE 0 END" for _ in terms)
    total = len(terms)
    min_hits = 1 if total <= 2 else max(2, round(total * 0.25))
    where = ["k.user_id=?", "d.status!='deleted'"]
    scope: list[Any] = [*terms, user_id]
    if knowledge_id:
        where.append('c.knowledge_id=?')
        scope.append(knowledge_id)
    if folder_id is not None:
        where.append('d.folder_id=?')
        scope.append(folder_id)
    sql = (
        "SELECT * FROM (SELECT c.content AS content,c.id AS chunk_id,c.document_id AS document_id,c.page_number AS page_number,d.filename AS filename,k.name AS knowledge_name," + score_sql + " AS hits "
        "FROM chunks c JOIN documents d ON d.id=c.document_id JOIN knowledge_bases k ON k.id=c.knowledge_id WHERE " + ' AND '.join(where) + ") AS scored WHERE hits>=? "
        "ORDER BY hits DESC, page_number ASC LIMIT ?"
    )
    rows = await fetchall(db, sql, (*scope, min_hits, limit))
    return [
        {
            'id': row['chunk_id'], 'document_id': row['document_id'], 'filename': row['filename'],
            'knowledge_name': row['knowledge_name'],
            'page_number': row['page_number'], 'content': row['content'], 'hits': int(row['hits'] or 0),
            'score': round(min(0.98, 0.7 + 0.28 * int(row['hits'] or 0) / total), 2),
        }
        for row in rows
    ]


def document_view(row) -> dict:
    value = row_dict(row) or {}
    for field in ('error_message', 'extracted_text', 'organized_title', 'summary', 'organize_error', 'organized_at', 'folder_id'):
        if value.get(field) is None:
            value[field] = ''
    for field in ('tags_json', 'key_points_json'):
        try:
            value[field.removesuffix('_json')] = json.loads(value.get(field) or '[]')
        except json.JSONDecodeError:
            value[field.removesuffix('_json')] = []
        value.pop(field, None)
    return value


def json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or '[]')
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "service": settings.app_name,
        "harness": {
            "enabled": settings.harness_enabled,
            "profile": settings.harness_profile if settings.harness_enabled else None,
            "provider": settings.harness_provider if settings.harness_enabled else None,
            "model": settings.harness_model if settings.harness_enabled else None,
        },
    }


@app.get('/ready')
async def ready() -> dict:
    db = await connect()
    try:
        await fetchone(db, 'SELECT COUNT(*) FROM users')
    finally:
        await db.close()
    return {'ok': True, 'database': 'ready'}


@app.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request) -> dict:
    rate_limit(f"login:{request.client.host if request.client else 'unknown'}", 10, 60, "登录尝试过于频繁，请一分钟后再试")
    if not settings.wechat_appid or not settings.wechat_secret:
        raise HTTPException(503, "服务端未配置微信小程序登录参数")
    try:
        data = await exchange_code_for_session(payload.code)
    except WeChatAuthError as exc:
        if exc.code == -1:
            print(f"[wechat] 登录换取 session 失败：{exc}", flush=True)
            raise HTTPException(502, "微信登录服务暂时不可用，请稍后重试") from exc
        raise HTTPException(401, "微信登录校验失败") from exc
    openid = data["openid"]
    db = await connect()
    row = await fetchone(db, "SELECT * FROM users WHERE openid = ?", (openid,))
    user_id = row["id"] if row else uuid.uuid4().hex
    timestamp = now()
    session_key = data.get("session_key", "")
    if row:
        await db.execute("UPDATE users SET session_key=?,updated_at=? WHERE id=?", (session_key, timestamp, user_id))
    else:
        # 注销过的微信号重新登录：不再给新用户试用（否则可以靠反复注销刷试用与额度）
        seen = await fetchone(db, "SELECT 1 AS hit FROM deleted_accounts WHERE openid_hash=?", (openid_fingerprint(openid),))
        await db.execute("INSERT INTO users(id,openid,nickname,avatar,session_key,trial_used,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (user_id, openid, "微信用户", "", session_key, 1 if seen else 0, timestamp, timestamp))
    await ensure_default_knowledge(db, user_id)
    await db.commit(); await db.close()
    token_version = int(row["token_version"]) if row and row["token_version"] is not None else 0
    return {"token": create_token(user_id, token_version), "user": {"id": user_id, "nickname": row["nickname"] if row else "微信用户", "avatar": row["avatar"] if row else "", "membership": membership_for_user(row) if row else "free"}}


@app.post("/api/auth/logout")
async def logout(user_id: str = Depends(current_user)) -> dict:
    """登出：提升 token 版本号，所有旧 token 立即失效。"""
    db = await connect()
    await db.execute("UPDATE users SET token_version=COALESCE(token_version,0)+1, updated_at=? WHERE id=?", (now(), user_id))
    await db.commit(); await db.close()
    return {"ok": True}


@app.get("/api/me")
async def me(user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    row = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    # 额度只看用户主动新建的个人知识库；默认库、共享收件箱和订阅镜像均不占额度。
    knowledge_usage = await fetchone(db, "SELECT COUNT(*) AS knowledge_bases FROM knowledge_bases WHERE user_id=? AND status='active' AND COALESCE(mirror_of,'')='' AND name NOT IN (?,?)", (user_id, DEFAULT_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_NAME))
    document_usage = await fetchone(db, "SELECT COUNT(*) AS documents, COALESCE(SUM(d.file_size),0) AS storage_bytes FROM documents d JOIN knowledge_bases k ON k.id=d.knowledge_id WHERE d.user_id=? AND d.status!='deleted' AND COALESCE(k.mirror_of,'')=''", (user_id,))
    credits_used = await credits_this_month(db, user_id)
    skills_used = await owned_skill_count(db, user_id)
    await db.close()
    if not row: raise HTTPException(404, "用户不存在")
    state = account_state(row, credits_used, skills_used)
    return {
        "id": row["id"], "nickname": row["nickname"], "avatar": row["avatar"],
        "membership": state['tier'], "membership_label": state['label'],
        "membership_expires_at": row['membership_expires_at'] or '',
        "trial": state['trial'], "period": state['period'], "quota": state['quota'], "entitlements": state['entitlements'],
        "limits": state['limits'],
        "usage": {"knowledge_bases": int(knowledge_usage['knowledge_bases'] or 0), "documents": int(document_usage['documents'] or 0), "storage_bytes": int(document_usage['storage_bytes'] or 0)},
    }


@app.patch("/api/me")
async def update_me(payload: ProfileUpdate, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    columns: list[str] = []
    values: list[Any] = []
    if payload.nickname is not None:
        columns.append("nickname=?")
        values.append(payload.nickname.strip())
    if payload.avatar is not None:
        columns.append("avatar=?")
        values.append(payload.avatar.strip())
    if not columns:
        await db.close()
        raise HTTPException(422, "没有需要更新的字段")
    columns.append("updated_at=?")
    values.append(now())
    values.append(user_id)
    await db.execute(f"UPDATE users SET {', '.join(columns)} WHERE id=? AND status='active'", tuple(values))
    await db.commit()
    row = await fetchone(db, "SELECT id,nickname,avatar FROM users WHERE id=? AND status='active'", (user_id,))
    await db.close()
    if not row: raise HTTPException(404, "用户不存在")
    return row_dict(row)


# ---- 头像 -------------------------------------------------------------------
# 微信只提供「头像昵称填写能力」：<button open-type="chooseAvatar"> 回回调给的是本机临时路径
# （http://tmp/... 或 wxfile://...），换机或重装就失效，所以必须落到服务端。平台不允许静默
# 读取用户头像昵称，这里不尝试任何“授权后自动拿”的写法。
AVATAR_DIR_NAME = 'avatars'
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_MEDIA_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}
# 直传凭证与预签名地址的有效期：够一次上传/预览用，过期即失效
UPLOAD_DIRECT_TTL = 900
ASSET_URL_TTL = 900
# 数据库里的小图（使用技巧配图）直接内联下发；超过就不下发，避免撑爆容器通道 1MiB 的返回包上限
CONTENT_ASSET_INLINE_MAX = 768 * 1024
# 返回包体检线：容器通道上限 1000KiB，超过这条线就写日志，方便上线前定位要瘦身的接口
RESPONSE_SIZE_WARN_BYTES = 900 * 1024


def _avatar_safe_id(user_id: str) -> str:
    """头像文件名只用用户 id 里的安全字符，挡掉 `..` / 通配符这类路径花招。"""
    return re.sub(r'[^0-9A-Za-z_-]', '', user_id or '')


def _avatar_refs(user_id: str) -> list[tuple[str, str]]:
    safe_id = _avatar_safe_id(user_id)
    if not safe_id:
        return []
    return [(suffix, storage.reference(f'{AVATAR_DIR_NAME}/{safe_id}{suffix}')) for suffix in AVATAR_MEDIA_TYPES]


@app.post('/api/me/avatar')
async def upload_avatar(file: UploadFile = File(...), user_id: str = Depends(current_user)) -> dict:
    """保存用户选定的头像，并把地址写回用户资料。

    只保留一份：换头像时旧文件当场删掉，不留垃圾。
    """
    data = await file.read()
    if not data:
        raise HTTPException(422, '头像文件为空')
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(413, '头像不能超过 2MB')
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in AVATAR_MEDIA_TYPES:
        # 微信给的文件名有时没有扩展名，按文件头兜一下，不靠扩展名信任内容
        suffix = '.png' if data.startswith(b'\x89PNG\r\n') else '.jpg'
    safe_id = _avatar_safe_id(user_id)
    if not safe_id:
        raise HTTPException(422, '用户信息不完整')
    try:
        await storage.save_bytes(data, f'{AVATAR_DIR_NAME}/{safe_id}{suffix}', content_type=AVATAR_MEDIA_TYPES[suffix])
        for stale_suffix, stale_ref in _avatar_refs(user_id):
            if stale_suffix != suffix:
                await storage.delete(stale_ref)
    except storage.StorageError as exc:
        raise HTTPException(503, '头像存储失败，请稍后重试') from exc
    avatar_url = f'/api/avatars/{user_id}?v={int(time.time())}'
    db = await connect()
    await db.execute("UPDATE users SET avatar=?, updated_at=? WHERE id=? AND status='active'", (avatar_url, now(), user_id))
    await db.commit(); await db.close()
    return {'avatar': avatar_url}


@app.get('/api/avatars/{user_id}')
async def get_avatar(user_id: str) -> Response:
    """头像读取不挂登录态：小程序 <image> 不能带 Authorization 头。

    安全性不靠鉴权而靠两点：文件名去掉了所有非字母数字字符（不可能路径穿越），
    且用户 id 是随机 32 位十六进制，猜不到别人的。与 /api/content/assets 同一取舍。
    """
    for suffix, ref in _avatar_refs(user_id):
        try:
            data = await storage.read_bytes(ref)
        except storage.StorageError:
            continue
        return Response(content=data, media_type=AVATAR_MEDIA_TYPES[suffix], headers={'Cache-Control': 'public, max-age=300'})
    raise HTTPException(404, '头像不存在')


KNOWLEDGE_AVATAR_DIR_NAME = 'knowledge-avatars'


def _knowledge_avatar_safe_id(knowledge_id: str) -> str:
    return re.sub(r'[^0-9A-Za-z_-]', '', knowledge_id or '')


@app.post('/api/knowledge/{knowledge_id}/avatar')
async def upload_knowledge_avatar(knowledge_id: str, file: UploadFile = File(...), user_id: str = Depends(current_user)) -> dict:
    """Persist a knowledge-base avatar and keep one active object."""
    db = await connect()
    kb = await fetchone(db, "SELECT id,avatar,mirror_of FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb:
        await db.close()
        raise HTTPException(404, '知识库不存在')
    if str(kb['mirror_of'] or ''):
        await db.close()
        raise HTTPException(403, '共享或订阅知识库不能单独更换头像')
    data = await file.read()
    if not data:
        await db.close()
        raise HTTPException(422, '头像文件为空')
    if len(data) > AVATAR_MAX_BYTES:
        await db.close()
        raise HTTPException(413, '头像不能超过 2MB')
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in AVATAR_MEDIA_TYPES:
        suffix = '.png' if data.startswith(b'\x89PNG\r\n') else '.jpg'
    safe_id = _knowledge_avatar_safe_id(knowledge_id)
    if not safe_id:
        await db.close()
        raise HTTPException(422, '知识库信息不完整')
    old_ref = str(kb['avatar'] or '')
    object_key = f'{KNOWLEDGE_AVATAR_DIR_NAME}/{safe_id}-{uuid.uuid4().hex[:10]}{suffix}'
    try:
        avatar_ref = await storage.save_bytes(data, object_key, content_type=AVATAR_MEDIA_TYPES[suffix])
    except storage.StorageError as exc:
        await db.close()
        raise HTTPException(503, '头像存储失败，请稍后重试') from exc
    stamp = now()
    await db.execute('UPDATE knowledge_bases SET avatar=?,updated_at=? WHERE id=? AND user_id=?', (avatar_ref, stamp, knowledge_id, user_id))
    await db.commit()
    await db.close()
    if old_ref and old_ref != avatar_ref:
        await storage.delete(old_ref)
    return {'avatar': f'/api/knowledge-avatars/{knowledge_id}?v={int(time.time())}'}


@app.get('/api/knowledge-avatars/{knowledge_id}')
async def get_knowledge_avatar(knowledge_id: str) -> Response:
    """Knowledge avatars are public because miniprogram <image> cannot send auth headers."""
    db = await connect()
    row = await fetchone(db, 'SELECT avatar FROM knowledge_bases WHERE id=? AND status=? ', (knowledge_id, 'active'))
    await db.close()
    ref = str(row['avatar'] or '') if row else ''
    if not ref:
        raise HTTPException(404, '知识库头像不存在')
    try:
        data = await storage.read_bytes(ref)
    except storage.StorageError as exc:
        raise HTTPException(404, '知识库头像不存在') from exc
    media_type = mimetypes.guess_type(ref)[0] or 'image/jpeg'
    return Response(content=data, media_type=media_type, headers={'Cache-Control': 'public, max-age=300'})


async def enforce_published_knowledge_policy(db: Any, knowledge_id: str, *parts: str) -> str:
    """Unpublish a public knowledge base when newly added content is rejected."""
    kb = await fetchone(db, 'SELECT visibility FROM knowledge_bases WHERE id=? AND status=?', (knowledge_id, 'active'))
    if not kb or str(kb['visibility'] or '') != 'public':
        return ''
    findings = knowledge_policy.review_content(*parts)
    if not findings:
        return ''
    await db.execute("UPDATE knowledge_bases SET visibility='private',published_at='',updated_at=? WHERE id=?", (now(), knowledge_id))
    return findings[0].message

@app.get("/api/me/preferences")
async def read_preferences(user_id: str = Depends(current_user)) -> dict:
    """用户偏好。skills 为 null 表示用户从未设置过，前端据此决定是否用本地值播种。"""
    db = await connect()
    row = await fetchone(db, "SELECT preferences FROM users WHERE id=? AND status='active'", (user_id,))
    await db.close()
    if not row:
        raise HTTPException(404, "用户不存在")
    return {'skills': stored_preferences(row['preferences']).get('skills')}


@app.put("/api/me/preferences")
async def update_preferences(payload: PreferenceUpdate, user_id: str = Depends(current_user)) -> dict:
    """只更新本次传入的字段；未传的字段保持原值，避免误清空用户设定。"""
    db = await connect()
    row = await fetchone(db, "SELECT preferences FROM users WHERE id=? AND status='active'", (user_id,))
    if not row:
        await db.close()
        raise HTTPException(404, "用户不存在")
    stored = stored_preferences(row['preferences'])
    if payload.skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in payload.skills:
            slug = (raw or '').strip()
            if not slug or len(slug) > 40 or slug in seen or not re.fullmatch(r'[a-z0-9-]+', slug):
                continue
            seen.add(slug)
            cleaned.append(slug)
            if len(cleaned) >= MAX_TURN_SKILLS:
                break
        stored['skills'] = cleaned
    await db.execute("UPDATE users SET preferences=?, updated_at=? WHERE id=?", (json.dumps(stored, ensure_ascii=False), now(), user_id))
    await db.commit()
    await db.close()
    return {'skills': stored.get('skills')}


@app.delete("/api/me")
async def delete_me(user_id: str = Depends(current_user)) -> dict:
    """注销账号：把与该用户关联的数据全部删除。

    注销后不能再留下可识别到个人的记录，所以除 documents/knowledge_bases（数据库外键级联）之外，
    这里还要显式清掉：生成的产物与其文件、自建技能与其技能包、客服会话与消息、计费流水。
    依法需要留存的只有网络访问日志（由平台侧留存），其余一律删除，与《数据管理》里的说明一致。
    """
    db = await connect()
    files = await fetchall(db, "SELECT storage_path FROM documents WHERE user_id=?", (user_id,))
    artifacts = await fetchall(db, "SELECT storage_path FROM artifacts WHERE user_id=?", (user_id,))
    skills = await fetchall(db, "SELECT harness FROM skills WHERE user_id=? AND COALESCE(harness,'')<>''", (user_id,))
    knowledge_avatars = await fetchall(db, "SELECT avatar FROM knowledge_bases WHERE user_id=? AND status='active' AND COALESCE(mirror_of,'')='' AND COALESCE(avatar,'')<>''", (user_id,))
    owned_subscriptions = await fetchall(db, 'SELECT s.user_id,s.mirror_knowledge_id FROM knowledge_subscriptions s JOIN knowledge_bases k ON k.id=s.source_knowledge_id WHERE k.user_id=?', (user_id,))
    for item in owned_subscriptions:
        await purge_subscription_mirror(db, str(item['mirror_knowledge_id']), str(item['user_id']))
    subscribed_sources = await fetchall(db, 'SELECT DISTINCT source_knowledge_id FROM knowledge_subscriptions WHERE user_id=?', (user_id,))
    await db.execute('DELETE FROM knowledge_subscriptions WHERE user_id=?', (user_id,))
    for item in subscribed_sources:
        await refresh_subscriber_count(db, str(item['source_knowledge_id']))
    await db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.user_id=?)", (user_id,))
    await db.execute("DELETE FROM artifacts WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM skills WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM skill_likes WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM skill_favorites WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM usage_logs WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM kf_messages WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM kf_sessions WHERE user_id=?", (user_id,))
    # 留一枚加盐指纹：注销本身可以，但同一个微信号不能靠反复注销重新领试用与额度。
    # 指纹不可逆（见 openid_fingerprint），《数据管理》里也对这条留存有说明。
    owner = await fetchone(db, "SELECT openid FROM users WHERE id=?", (user_id,))
    if owner and str(owner['openid'] or ''):
        await db.execute("INSERT OR REPLACE INTO deleted_accounts(openid_hash,deleted_at) VALUES(?,?)", (openid_fingerprint(str(owner['openid'])), now()))
    await db.execute("DELETE FROM users WHERE id=?", (user_id,))
    await db.commit()
    for row in [*files, *artifacts]:
        await unlink_if_unreferenced(db, str(row['storage_path'] or ''))
    await db.close()
    # 技能包是落在磁盘上的目录，删库里的行不够，要连文件一起清（和「删除技能」走同一份实现）
    for row in skills:
        skill_build.remove_package(user_id, str(row['harness'] or ''))
    for _suffix, ref in _avatar_refs(user_id):
        await storage.delete(ref)
    for row in knowledge_avatars:
        await storage.delete(str(row['avatar'] or ''))
    return {"ok": True}



PAY_ORDER_STATUS_LABELS = {'pending': '待支付', 'paid': '已支付', 'refunded': '已退款', 'closed': '已关闭'}
PAY_DELIVER_LABELS = {'pending': '权益待发放', 'delivered': '权益已到账', 'refunded': '权益已回收'}


def beijing_time_label(value: Any) -> str:
    """库里的 UTC ISO 串 → 北京时间「2026-09-21 10:24」；缺失或坏数据返回空串。

    订单页的时间统一由服务端定稿：小程序端 iOS 与 Android 解析 ISO 串的行为并不一致
    （带时区偏移的串在部分旧 iOS 上会算成 NaN），展示层不自己算时间。
    """
    moment = parse_moment(value)
    if not moment:
        return ''
    return moment.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')


def pay_order_status_label(status: str, deliver_status: str) -> str:
    """订单状态文案。已支付但还没发货的单独说「发放中」，不让用户以为权益没到账。"""
    if status == 'paid':
        return '已生效' if deliver_status == 'delivered' else '发放中'
    return PAY_ORDER_STATUS_LABELS.get(status, status or '未知')


def virtual_pay_data(user: Any, plan: str, out_trade_no: str, attach: str) -> dict:
    """构造 requestVirtualPayment 要的下单数据。

    「继续支付」必须复用原单号与原 attach：虚拟支付按 outTradeNo 判重，换单号会变成两笔
    订单，换 attach 会让平台的发货回调对不回原来那条 pay_orders。
    """
    product_id, goods_price = virtual_product(plan)
    body = sign_data(offer_id=settings.wechat_virtual_offer_id, quantity=1, env=settings.wechat_virtual_env,
                    product_id=product_id, goods_price=goods_price, out_trade_no=out_trade_no, attach=attach)
    return {'mode': 'short_series_goods', 'signData': body, 'paySig': calc_pay_sig('requestVirtualPayment', body), 'signature': calc_user_signature(body, user['session_key'])}


def pay_order_view(row: Any) -> dict:
    """订单中心列表的一行：文案、金额、时间都在服务端定稿，前端只负责排版。

    can_pay 只在「待支付 + 后台商品配置齐」时为真：否则按钮点了必然报错，
    不如直接灰掉，用户不会白点一下还看不懂为什么。
    """
    plan = str(row['plan'] or '')
    amount = int(row['amount'] or 0)
    status = str(row['status'] or '')
    deliver_status = str(row['deliver_status'] or '')
    catalog = PLAN_CATALOG.get(plan)
    return {
        'out_trade_no': str(row['out_trade_no'] or ''),
        'plan': plan,
        'plan_label': catalog[1] if catalog else plan,
        'amount': amount,
        'amount_label': f'¥{amount / 100:.2f}',
        'status': status,
        'status_label': pay_order_status_label(status, deliver_status),
        'deliver_status': deliver_status,
        'deliver_label': PAY_DELIVER_LABELS.get(deliver_status, ''),
        'created_at': str(row['created_at'] or ''),
        'created_at_label': beijing_time_label(row['created_at']),
        'paid_at_label': beijing_time_label(row['paid_at']),
        'can_pay': status == 'pending' and settings.wechat_payment_mode == 'virtual' and virtual_configured(plan),
    }


@app.get('/api/pay/plans')
async def pay_plans() -> dict:
    """会员方案与权益。改价只需改 PLAN_CATALOG，前台文案与价格都从这里取。"""
    tiers = []
    for tier in ('plus', 'pro'):
        limits = MEMBERSHIP_LIMITS[tier]
        plans = [
            {'id': plan, 'amount': amount, 'price': f'{amount / 100:.2f}', 'days': days, 'available': virtual_configured(plan)}
            for plan, (amount, _desc, plan_tier, days) in PLAN_CATALOG.items() if plan_tier == tier
        ]
        tiers.append({
            'id': tier, 'label': limits['label'], 'storage_bytes': limits['storage_bytes'], 'storage_label': human_size(limits['storage_bytes']),
            'knowledge_bases': limits['knowledge_bases'], 'monthly_credits': limits['monthly_credits'],
            'max_file_label': human_size(limits['max_file_bytes']), 'plans': plans,
        })
    free = MEMBERSHIP_LIMITS['free']
    return {
        'trial_days': TRIAL_DAYS,
        'free': {
            'label': free['label'], 'storage_label': human_size(free['storage_bytes']), 'knowledge_bases': free['knowledge_bases'],
            'monthly_credits': free['monthly_credits'], 'monthly_credits_after_trial': FREE_CREDITS_AFTER_TRIAL,
        },
        'tiers': tiers,
    }


@app.post("/api/pay/orders")
async def create_pay_order(payload: PayCreateRequest, user_id: str = Depends(current_user)) -> dict:
    if settings.wechat_payment_mode != 'virtual':
        raise HTTPException(503, "当前服务仅配置个人主体虚拟支付")
    if not virtual_configured(payload.plan):
        raise HTTPException(503, "当前方案尚未配置微信道具，请联系管理员完成商品配置")
    amount, _description, _tier, _days = PLAN_CATALOG[payload.plan]
    product_id, goods_price = virtual_product(payload.plan)
    db = await connect(); user = await fetchone(db, "SELECT openid,session_key FROM users WHERE id=?", (user_id,)); await db.close()
    if not user: raise HTTPException(401, "用户不存在，请重新登录")
    if not user["session_key"]:
        raise HTTPException(401, "登录态已失效，请重新登录后再支付")
    out_trade_no = f"LW{datetime.now(timezone.utc):%Y%m%d%H%M%S}{secrets.token_hex(5).upper()}"
    attach = json.dumps({'user_id': user_id, 'plan': payload.plan}, ensure_ascii=False, separators=(',', ':'))
    pay_data = virtual_pay_data(user, payload.plan, out_trade_no, attach)
    try:
        db = await connect()
        await db.execute("INSERT INTO pay_orders(id,user_id,out_trade_no,plan,amount,status,offer_id,product_id,attach,quantity,deliver_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, user_id, out_trade_no, payload.plan, goods_price, "pending", settings.wechat_virtual_offer_id, product_id, attach, 1, "pending", now()))
        await db.commit(); await db.close()
    except Exception:
        try: await db.close()
        except Exception: pass
        raise HTTPException(500, "创建支付订单失败，请重试")
    return {"out_trade_no": out_trade_no, "plan": payload.plan, "amount": amount, "payData": pay_data}


@app.get('/api/pay/orders')
async def list_pay_orders(user_id: str = Depends(current_user)) -> dict:
    """订单中心：只回当前用户自己的订单，最近的在前。

    不做分页：个人账号的支付订单量级很小（最多几十笔），一次取 50 条足够，
    多一层翻页反而要在小程序里多维护一份状态。
    """
    db = await connect()
    rows = await fetchall(db, 'SELECT out_trade_no,plan,amount,status,deliver_status,created_at,paid_at FROM pay_orders WHERE user_id=? ORDER BY created_at DESC LIMIT 50', (user_id,))
    await db.close()
    return {'orders': [pay_order_view(row) for row in rows]}


@app.post('/api/pay/orders/{out_trade_no}/pay')
async def reopen_pay_order(out_trade_no: str, user_id: str = Depends(current_user)) -> dict:
    """订单中心的「继续支付」：复用原单号重新唤起收银台，不新开一笔订单。

    只允许待支付的单子续付；已支付/已退款直接拒绝，避免重复扣费。
    """
    if settings.wechat_payment_mode != 'virtual':
        raise HTTPException(503, "当前服务仅配置个人主体虚拟支付")
    db = await connect()
    order = await fetchone(db, 'SELECT * FROM pay_orders WHERE out_trade_no=? AND user_id=?', (out_trade_no, user_id))
    user = await fetchone(db, 'SELECT session_key FROM users WHERE id=?', (user_id,))
    await db.close()
    if not order:
        raise HTTPException(404, '支付订单不存在')
    if str(order['status'] or '') != 'pending':
        raise HTTPException(409, '该订单已支付或已退款，无需重复支付')
    plan = str(order['plan'] or '')
    # 先报「商品没配好」再报「登录态失效」：和 create_pay_order 的判定顺序一致，否则后台
    # 漏配商品时用户看到的是「请重新登录」，既误导人又找不到真正的原因。
    if not virtual_configured(plan):
        raise HTTPException(503, '当前方案尚未配置微信道具，请联系管理员完成商品配置')
    if not user or not user['session_key']:
        raise HTTPException(401, '登录态已失效，请重新登录后再支付')
    attach = str(order['attach'] or '') or json.dumps({'user_id': user_id, 'plan': plan}, ensure_ascii=False, separators=(',', ':'))
    catalog = PLAN_CATALOG.get(plan)
    return {
        'out_trade_no': out_trade_no,
        'plan': plan,
        'plan_label': catalog[1] if catalog else plan,
        'amount': int(order['amount'] or 0),
        'payData': virtual_pay_data(user, plan, out_trade_no, attach),
    }


def _xml_response(err_code: int, err_msg: str, status_code: int = 200) -> Response:
    return Response(f'<xml><ErrCode>{err_code}</ErrCode><ErrMsg><![CDATA[{err_msg}]]></ErrMsg></xml>', status_code=status_code, media_type='application/xml')


@app.get('/api/pay/notify')
async def pay_notify_verify(signature: str = '', timestamp: str = '', nonce: str = '', echostr: str = '') -> Response:
    """MP 后台保存消息推送 URL 时的 GET 验证：sha1(sort(token,timestamp,nonce)) 比对后原样回 echostr。"""
    token = settings.wechat_virtual_notify_token
    if not token:
        raise HTTPException(503, '未配置 WECHAT_VIRTUAL_NOTIFY_TOKEN')
    digest = hashlib.sha1(''.join(sorted([token, timestamp, nonce])).encode('utf-8')).hexdigest()
    if not signature or digest != signature:
        raise HTTPException(403, '消息推送签名校验失败')
    return Response(echostr, media_type='text/plain')


@app.post("/api/pay/notify")
async def pay_notify(request: Request) -> Response:
    body = await request.body()
    try:
        root = ET.fromstring(body)
        text = lambda path: (root.findtext(path) or '').strip()
        event = text('Event')
        openid, out_trade_no = text('OpenId'), text('OutTradeNo')
        wx_order_id = text('WeChatPayInfo/MchOrderNo')
        if not openid or not out_trade_no:
            raise ValueError('missing order fields')
    except (ET.ParseError, ValueError):
        return _xml_response(1, 'invalid request', 400)

    if event == 'xpay_goods_deliver_notify':
        try:
            product_id, quantity = text('GoodsInfo/ProductId'), int(text('GoodsInfo/Quantity') or '1')
            if not wx_order_id or quantity < 1:
                raise ValueError('missing deliver fields')
        except ValueError:
            return _xml_response(1, 'invalid request', 400)
        db = await connect()
        order = await fetchone(db, "SELECT p.*,u.openid FROM pay_orders p JOIN users u ON u.id=p.user_id WHERE p.out_trade_no=?", (out_trade_no,))
        if not order or order['openid'] != openid or (product_id and order['product_id'] != product_id):
            await db.close()
            return _xml_response(1, 'order not found', 404)
        if order['deliver_status'] == 'delivered' and order['wx_order_id'] == wx_order_id:
            await db.close()
            return _xml_response(0, 'success')
        await deliver_membership(db, order, wx_order_id, quantity)
        await db.commit(); await db.close()
        return _xml_response(0, 'success')

    if event == 'xpay_refund_notify':
        db = await connect()
        order = await fetchone(db, "SELECT * FROM pay_orders WHERE out_trade_no=?", (out_trade_no,))
        if order and order['status'] != 'refunded':
            await db.execute("UPDATE pay_orders SET status='refunded',deliver_status='refunded',wx_order_id=COALESCE(NULLIF(?,''),wx_order_id) WHERE out_trade_no=?", (wx_order_id, out_trade_no))
            # 回收权益：仅当该用户没有其它同档有效订单时才降级，避免误伤续费/升级单
            tier = PLAN_CATALOG.get(order['plan'], (0, '', 'free', 0))[2]
            others = await fetchall(db, "SELECT plan FROM pay_orders WHERE user_id=? AND status='paid' AND deliver_status='delivered' AND id!=?", (order['user_id'], order['id']))
            if tier != 'free' and not any(PLAN_CATALOG.get(o['plan'], (0, '', 'free', 0))[2] == tier for o in others):
                await db.execute("UPDATE users SET membership='free',membership_expires_at='' WHERE id=? AND membership=?", (order['user_id'], tier))
            await db.commit()
        if db:
            await db.close()
        # 退款单即使本系统查不到也确认收货，避免平台 15 次重试刷接口
        return _xml_response(0, 'success')

    # 其它事件类型不属于本支付链路，直接确认避免平台重试
    return _xml_response(0, 'success')


@app.get('/api/pay/orders/{out_trade_no}')
async def get_pay_order(out_trade_no: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); row = await fetchone(db, 'SELECT out_trade_no,plan,amount,status,wx_order_id,deliver_status,created_at,paid_at,delivered_at FROM pay_orders WHERE out_trade_no=? AND user_id=?', (out_trade_no, user_id)); await db.close()
    if not row: raise HTTPException(404, '支付订单不存在')
    return row_dict(row)


@app.post('/api/pay/query')
async def query_pay_order(payload: dict, user_id: str = Depends(current_user)) -> dict:
    out_trade_no = str(payload.get('out_trade_no') or '')
    if not out_trade_no: raise HTTPException(422, '缺少订单号')
    db = await connect(); user = await fetchone(db, 'SELECT openid FROM users WHERE id=?', (user_id,)); order = await fetchone(db, 'SELECT * FROM pay_orders WHERE out_trade_no=? AND user_id=?', (out_trade_no, user_id)); await db.close()
    if not order or not user: raise HTTPException(404, '支付订单不存在')
    try:
        result = await query_order(user['openid'], out_trade_no)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    # Query is the recovery path when the platform push was lost. Normalize the
    # common paid markers and make the local delivery transition idempotent.
    if order_paid_marker(result) in PAID_MARKERS:
        db = await connect()
        fresh = await fetchone(db, 'SELECT * FROM pay_orders WHERE out_trade_no=? AND user_id=?', (out_trade_no, user_id))
        if fresh and fresh['deliver_status'] != 'delivered':
            await deliver_membership(db, fresh, str(fresh['wx_order_id'] or ''), int(fresh['quantity'] or 1))
            await db.commit()
        await db.close()
    return {'out_trade_no': out_trade_no, 'order': result}


# ---- 微信客服（kf-mgnt / kf-message）---------------------------------------
# 前端只需要一个 <button open-type="contact">：点击后由微信拉起原生客服会话窗口，
# 这是官方唯一支持的路径，自己用 web-view 搭聊天页反而拿不到会话额度、也不能转人工。
# 开发者要做的是后端：处理微信推来的消息（回调）、把消息存下来、以及代客服下发消息。
# 全部接口见 services/wechat_kf.py；这里只做路由、鉴权与落库。
# 客服账号管理/代发消息是运营动作，不是普通用户接口，用 KF_ADMIN_TOKEN 单独护住。
KF_MEDIA_SUFFIX = {'image': '.jpg', 'voice': '.amr', 'video': '.mp4', 'shortvideo': '.mp4'}
KF_AUTOREPLY_TEXT = (
    '您好，消息已经收到，我们会尽快回复。\n'
    '如果是订单、退款、注销账号或删除资料，麻烦把订单号或知识库名称一起发过来，处理会快很多；\n'
    '常见问题也可以先看「我的 → 使用技巧」。'
)


async def kf_admin(x_kf_admin_token: str | None = Header(default=None)) -> None:
    """客服管理接口的门槛。

    配了 WECHAT_KF_ADMIN_TOKEN 就必须带头部口令；没配的话开发环境放行（方便本机调），
    生产环境直接关掉——客服会话里能看到用户的提问原文，不能裸奔。
    """
    expected = (settings.wechat_kf_admin_token or '').strip()
    if expected:
        if not x_kf_admin_token or not secrets.compare_digest(x_kf_admin_token, expected):
            raise HTTPException(403, '客服管理口令不正确')
        return
    if settings.app_env == 'production':
        raise HTTPException(503, '未配置 WECHAT_KF_ADMIN_TOKEN，客服管理接口已关闭')


def _kf_http(exc: Exception) -> HTTPException:
    code = getattr(exc, 'code', -1)
    message = getattr(exc, 'message', str(exc))
    return HTTPException(status_code=502, detail=f'{message}（errcode {code}）')


async def _kf_store_media(openid: str, message: dict) -> str:
    """用户发来的图片/语音/视频先落到本机，后台看会话时才能直接看到内容。

    下载失败不影响主流程：消息本体已经入库，丢的只是附件。
    """
    media_id = str(message.get('media_id') or '')
    msg_type = str(message.get('msg_type') or '')
    if not media_id or msg_type not in KF_MEDIA_SUFFIX:
        return ''
    try:
        result = await kf_service.get_temp_media(media_id)
    except (kf_service.WeChatKfError, httpx.HTTPError):
        return ''
    content = result.get('_binary')
    if not content:
        return ''
    safe_openid = re.sub(r'[^0-9A-Za-z_-]', '', openid)[:64] or 'unknown'
    suffix = KF_MEDIA_SUFFIX[msg_type]
    return await storage.save_bytes(content, f'kf/{safe_openid}/{uuid.uuid4().hex}{suffix}')


async def _kf_save_message(db, *, openid: str, user_id: str, role: str, msg_type: str, content: str, media_id: str = '', media_path: str = '', kf_account: str = '', raw: dict | None = None, source: str = 'push') -> str:
    message_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO kf_messages(id,openid,user_id,role,msg_type,content,media_id,media_path,kf_account,raw_json,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, openid, user_id or '', role, msg_type, content or '', media_id or '', media_path or '', kf_account or '', json.dumps(raw or {}, ensure_ascii=False), source, now()),
    )
    return message_id


async def _kf_touch_session(db, openid: str, user_id: str, *, unread_delta: int = 0, autoreply_at: str = '') -> Any:
    stamp = now()
    row = await fetchone(db, 'SELECT * FROM kf_sessions WHERE openid=?', (openid,))
    if row:
        await db.execute(
            "UPDATE kf_sessions SET user_id=CASE WHEN ?!='' THEN ? ELSE user_id END, message_count=message_count+1, unread_count=CASE WHEN unread_count+?<0 THEN 0 ELSE unread_count+? END, last_message_at=CASE WHEN ?!='' THEN ? ELSE last_message_at END, last_autoreply_at=CASE WHEN ?!='' THEN ? ELSE last_autoreply_at END, updated_at=? WHERE openid=?",
            (user_id, user_id, unread_delta, unread_delta, stamp, stamp, autoreply_at, autoreply_at, stamp, openid),
        )
        return row
    await db.execute(
        "INSERT INTO kf_sessions(openid,user_id,message_count,unread_count,last_message_at,last_autoreply_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (openid, user_id or '', 1, max(0, unread_delta), stamp, autoreply_at, stamp, stamp),
    )
    return None


def _kf_autoreply_allowed(row: Any) -> bool:
    if not settings.wechat_kf_autoreply:
        return False
    last = str(row['last_autoreply_at'] or '') if row else ''
    if not last:
        return True
    try:
        previous = datetime.fromisoformat(last)
    except ValueError:
        return True
    cooldown = max(0, int(settings.wechat_kf_autoreply_cooldown_seconds))
    return (datetime.now(timezone.utc) - previous).total_seconds() >= cooldown


async def _kf_handle_inbound(message: dict) -> str:
    """收一条用户消息：落库 + 必要时回一条回执。返回被动回复 XML（空串表示不回）。"""
    openid = str(message.get('from_user') or '')
    if not openid:
        return ''
    msg_type = str(message.get('msg_type') or 'text')
    media_path = await _kf_store_media(openid, message)
    db = await connect()
    try:
        user = await fetchone(db, 'SELECT id FROM users WHERE openid=?', (openid,))
        user_id = str(user['id']) if user else ''
        session = await _kf_touch_session(db, openid, user_id, unread_delta=1)
        await _kf_save_message(
            db,
            openid=openid,
            user_id=user_id,
            role='user',
            msg_type=msg_type,
            content=str(message.get('plain') or ''),
            media_id=str(message.get('media_id') or ''),
            media_path=media_path,
            kf_account=str(message.get('kf_account') or ''),
            raw=message,
        )

        allow_reply = _kf_autoreply_allowed(session)
        if not allow_reply:
            await db.commit()
            return ''

        stamp = now()
        await _kf_save_message(
            db,
            openid=openid,
            user_id=user_id,
            role='kf',
            msg_type='text',
            content=KF_AUTOREPLY_TEXT,
            kf_account=str(message.get('kf_account') or ''),
            source='autoreply',
        )
        await db.execute('UPDATE kf_sessions SET last_autoreply_at=?, updated_at=? WHERE openid=?', (stamp, stamp, openid))
        await db.commit()
    finally:
        await db.close()
    return kf_service.build_text_reply(openid, str(message.get('to_user') or ''), KF_AUTOREPLY_TEXT)


@app.get('/api/wechat/kf/callback')
async def kf_callback_verify(signature: str = '', timestamp: str = '', nonce: str = '', echostr: str = '') -> Response:
    """MP 后台保存「消息推送」URL 时的 GET 验证：验签通过就原样回 echostr。"""
    if not kf_service.push_ready():
        raise HTTPException(503, '未配置 WECHAT_KF_TOKEN，客服消息推送不可用')
    if not kf_service.verify_url_signature(signature, timestamp, nonce):
        raise HTTPException(403, '客服消息推送签名校验失败')
    return Response(echostr, media_type='text/plain')


@app.post('/api/wechat/kf/callback')
async def kf_callback(request: Request, signature: str = '', timestamp: str = '', nonce: str = '', msg_signature: str = '', encrypt_type: str = '') -> Response:
    """收用户消息：安全模式先解密，落库，必要时回一条回执。

    回包要用同一种模式：安全模式回 <Encrypt>，明文模式回明文 XML。
    """
    if not kf_service.push_ready():
        raise HTTPException(503, '未配置 WECHAT_KF_TOKEN，客服消息推送不可用')
    raw = await request.body()
    body = raw.decode('utf-8', errors='replace')
    safe_mode = encrypt_type == 'aes' or bool(msg_signature)
    if safe_mode:
        try:
            encrypt = (ET.fromstring(raw).findtext('Encrypt') or '').strip()
        except ET.ParseError:
            raise HTTPException(400, '客服消息推送体不是合法 XML')
        if not kf_service.verify_encrypt_signature(msg_signature, timestamp, nonce, encrypt):
            raise HTTPException(403, '客服消息推送签名校验失败')
        try:
            decrypted = kf_service.decrypt_message(encrypt)
        except kf_service.WeChatKfError as exc:
            raise _kf_http(exc)
        if decrypted['appid'] and settings.wechat_appid and decrypted['appid'] != settings.wechat_appid:
            raise HTTPException(403, '客服消息推送 appid 不匹配')
        body = decrypted['xml']
    elif not kf_service.verify_url_signature(signature, timestamp, nonce):
        raise HTTPException(403, '客服消息推送签名校验失败')

    try:
        message = kf_service.parse_message(body)
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)

    try:
        reply = await _kf_handle_inbound(message)
    except Exception:  # noqa: BLE001 - 任何异常都不能让微信重试 15 次刷接口
        logging.getLogger('app.kf').exception('处理客服消息失败 openid=%s', message.get('from_user'))
        return Response('success', media_type='text/plain')

    if not reply:
        return Response('success', media_type='text/plain')
    if safe_mode:
        return Response(kf_service.reply_encrypt(reply, timestamp, nonce), media_type='application/xml')
    return Response(reply, media_type='application/xml')


def _kf_account_suffix() -> str:
    """客服账号完整格式是「前缀@小程序微信号」，后台只需要填前缀。

    小程序微信号可以从 MP 后台「设置 → 基本设置」看到，配在 WECHAT_KF_ACCOUNT_SUFFIX。
    没配的话就要求调用方直接传完整账号，避免拼出一个不存在的账号被微信回 40003。
    """
    return (settings.wechat_kf_account_suffix or '').strip().lstrip('@')


def _kf_full_account(value: str) -> str:
    raw = (value or '').strip()
    if not raw:
        raise HTTPException(422, '缺少 kf_account')
    if '@' in raw:
        return raw
    suffix = _kf_account_suffix()
    if not suffix:
        raise HTTPException(422, '请传完整的 kf_account（前缀@小程序微信号），或配置 WECHAT_KF_ACCOUNT_SUFFIX')
    return f'{raw}@{suffix}'


@app.get('/api/wechat/kf/status')
async def kf_status(_: None = Depends(kf_admin)) -> dict:
    """运营自查：凭证、消息推送、自动回执各配没配好。不回任何密钥。"""
    payload = {
        'credential_configured': kf_service.configured(),
        'push_configured': kf_service.push_ready(),
        'safe_mode_configured': bool((settings.wechat_kf_aes_key or '').strip()),
        'admin_token_configured': bool((settings.wechat_kf_admin_token or '').strip()),
        'autoreply': bool(settings.wechat_kf_autoreply),
        'account_suffix': _kf_account_suffix(),
        'callback_url': '/api/wechat/kf/callback',
    }
    if kf_service.configured():
        try:
            payload['accounts'] = await kf_service.list_accounts()
        except kf_service.WeChatKfError as exc:
            payload['accounts_error'] = exc.message
    return payload


@app.get('/api/wechat/kf/accounts')
async def kf_accounts(_: None = Depends(kf_admin)) -> dict:
    """所有客服账号 + 在线客服列表（两个官方接口一起给，运营只看一张表）。"""
    try:
        accounts = await kf_service.list_accounts()
        online = await kf_service.list_online_accounts()
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)
    online_ids = {item['kf_account'] for item in online}
    for item in accounts:
        item['online'] = item['online'] or item['kf_account'] in online_ids
    return {'accounts': accounts, 'online': online}


@app.get('/api/wechat/kf/accounts/online')
async def kf_accounts_online(_: None = Depends(kf_admin)) -> list[dict]:
    try:
        return await kf_service.list_online_accounts()
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)


@app.post('/api/wechat/kf/accounts')
async def kf_account_add(payload: dict, _: None = Depends(kf_admin)) -> dict:
    account = _kf_full_account(str(payload.get('kf_account') or payload.get('prefix') or ''))
    nickname = str(payload.get('nickname') or '').strip()
    if not nickname:
        raise HTTPException(422, '缺少 nickname')
    try:
        await kf_service.add_account(account, nickname)
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)
    return {'ok': True, 'kf_account': account, 'nickname': nickname}


@app.delete('/api/wechat/kf/accounts')
async def kf_account_del(kf_account: str = Query(default=''), _: None = Depends(kf_admin)) -> dict:
    account = _kf_full_account(kf_account)
    try:
        await kf_service.del_account(account)
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)
    return {'ok': True, 'kf_account': account}


@app.post('/api/wechat/kf/accounts/admin')
async def kf_account_set_admin(payload: dict, _: None = Depends(kf_admin)) -> dict:
    kf_openid = str(payload.get('kf_openid') or '').strip()
    if not kf_openid:
        raise HTTPException(422, '缺少 kf_openid（客服的微信号，不是 kf_account）')
    try:
        await kf_service.set_admin(kf_openid)
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)
    return {'ok': True, 'kf_openid': kf_openid, 'admin': True}


@app.delete('/api/wechat/kf/accounts/admin')
async def kf_account_cancel_admin(kf_openid: str = Query(default=''), _: None = Depends(kf_admin)) -> dict:
    if not kf_openid.strip():
        raise HTTPException(422, '缺少 kf_openid')
    try:
        await kf_service.cancel_admin(kf_openid.strip())
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)
    return {'ok': True, 'kf_openid': kf_openid.strip(), 'admin': False}


@app.post('/api/wechat/kf/media')
async def kf_media_upload(file: UploadFile = File(...), media_type: str = Form(default='image'), _: None = Depends(kf_admin)) -> dict:
    """上传临时素材，拿到 media_id 后才能给用户发图片/语音/视频/小程序卡片的封面。"""
    if media_type not in {'image', 'voice', 'video', 'thumb'}:
        raise HTTPException(422, 'media_type 只支持 image / voice / video / thumb')
    data = await file.read()
    if not data:
        raise HTTPException(422, '文件不能为空')
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, '临时素材不能超过 10MB')
    try:
        return await kf_service.upload_temp_media(data, file.filename or 'upload.bin', media_type)
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)


@app.post('/api/wechat/kf/send')
async def kf_send(payload: dict, _: None = Depends(kf_admin)) -> dict:
    """客服主动下发消息（48 小时会话额度内）。发出去的同时记一行，会话历史里能看到。"""
    openid = str(payload.get('openid') or '').strip()
    if not openid:
        raise HTTPException(422, '缺少 openid')
    msg_type = str(payload.get('msg_type') or 'text').strip()
    kf_account = str(payload.get('kf_account') or '').strip()
    content = str(payload.get('content') or '')
    try:
        if msg_type == 'text':
            if not content.strip():
                raise HTTPException(422, '文本内容不能为空')
            result = await kf_service.send_text(openid, content, kf_account, ai_msg=bool(payload.get('ai_msg')))
        elif msg_type == 'image':
            media_id = str(payload.get('media_id') or '')
            if not media_id:
                raise HTTPException(422, '缺少 media_id')
            result = await kf_service.send_image(openid, media_id, kf_account)
        elif msg_type == 'miniprogrampage':
            thumb_media_id = str(payload.get('thumb_media_id') or '')
            pagepath = str(payload.get('pagepath') or '')
            if not thumb_media_id or not pagepath:
                raise HTTPException(422, '小程序卡片需要 thumb_media_id 与 pagepath')
            result = await kf_service.send_miniprogrampage(openid, str(payload.get('title') or ''), pagepath, thumb_media_id, str(payload.get('appid') or ''), kf_account)
        elif msg_type == 'news':
            articles = payload.get('articles')
            if not isinstance(articles, list) or not articles:
                raise HTTPException(422, 'articles 不能为空')
            result = await kf_service.send_news(openid, articles, kf_account)
        elif msg_type == 'raw':
            raw_payload = payload.get('payload')
            if not isinstance(raw_payload, dict) or not raw_payload:
                raise HTTPException(422, '原始下发需要 payload')
            result = await kf_service.send_raw(raw_payload)
        else:
            raise HTTPException(422, 'msg_type 只支持 text / image / miniprogrampage / news / raw')
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)

    summary = content or {'image': '[图片]', 'miniprogrampage': f"[小程序卡片] {payload.get('title') or ''}", 'news': f"[图文] {len(payload.get('articles') or [])} 篇", 'raw': '[自定义消息]'}.get(msg_type, '')
    db = await connect()
    try:
        user = await fetchone(db, 'SELECT id FROM users WHERE openid=?', (openid,))
        user_id = str(user['id']) if user else ''
        await _kf_save_message(db, openid=openid, user_id=user_id, role='kf', msg_type=msg_type, content=str(summary), media_id=str(payload.get('media_id') or ''), kf_account=kf_account, raw=dict(payload), source='outbound')
        await db.execute(
            "INSERT INTO kf_outbox(id,openid,msg_type,payload_json,state,error,kf_account,created_at,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, openid, msg_type, json.dumps(result, ensure_ascii=False), 'sent', '', kf_account, now(), now()),
        )
        await _kf_touch_session(db, openid, user_id, unread_delta=0)
        await db.commit()
    finally:
        await db.close()
    return {'ok': True, 'openid': openid, 'msg_type': msg_type, 'result': result}


@app.post('/api/wechat/kf/typing')
async def kf_typing(payload: dict, _: None = Depends(kf_admin)) -> dict:
    """输入状态：人工在后台打字时让用户看到「客服正在输入」。"""
    openid = str(payload.get('openid') or '').strip()
    if not openid:
        raise HTTPException(422, '缺少 openid')
    command = 'Typing' if str(payload.get('command') or 'Typing') == 'Typing' else 'CancelTyping'
    try:
        result = await kf_service.typing(openid, command)
    except kf_service.WeChatKfError as exc:
        raise _kf_http(exc)
    return {'ok': True, 'command': command, 'result': result}


@app.get('/api/wechat/kf/sessions')
async def kf_sessions(limit: int = Query(default=50, ge=1, le=200), only_unread: bool = Query(default=False), _: None = Depends(kf_admin)) -> list[dict]:
    """客服会话列表：按最后一条消息时间倒序，带未读数，让运营先看最久没人管的。"""
    db = await connect()
    try:
        clause = 'WHERE unread_count > 0' if only_unread else ''
        rows = await fetchall(db, f'SELECT * FROM kf_sessions {clause} ORDER BY last_message_at DESC LIMIT ?', (limit,))
        sessions = []
        for row in rows:
            item = row_dict(row) or {}
            last = await fetchone(db, 'SELECT role,msg_type,content,created_at FROM kf_messages WHERE openid=? ORDER BY created_at DESC LIMIT 1', (row['openid'],))
            item['last_message'] = row_dict(last)
            sessions.append(item)
        return sessions
    finally:
        await db.close()


@app.get('/api/wechat/kf/sessions/{openid}/messages')
async def kf_session_messages(openid: str, limit: int = Query(default=100, ge=1, le=500), mark_read: bool = Query(default=True), _: None = Depends(kf_admin)) -> dict:
    db = await connect()
    try:
        rows = await fetchall(db, 'SELECT * FROM kf_messages WHERE openid=? ORDER BY created_at DESC LIMIT ?', (openid, limit))
        messages = [row_dict(row) for row in reversed(rows)]
        if mark_read:
            await db.execute('UPDATE kf_sessions SET unread_count=0, updated_at=? WHERE openid=?', (now(), openid))
            await db.commit()
        session = await fetchone(db, 'SELECT * FROM kf_sessions WHERE openid=?', (openid,))
        return {'openid': openid, 'session': row_dict(session), 'messages': messages}
    finally:
        await db.close()


@app.post('/api/wechat/kf/sessions/{openid}/read')
async def kf_session_read(openid: str, _: None = Depends(kf_admin)) -> dict:
    db = await connect()
    try:
        await db.execute('UPDATE kf_sessions SET unread_count=0, updated_at=? WHERE openid=?', (now(), openid))
        await db.commit()
    finally:
        await db.close()
    return {'ok': True, 'openid': openid}


@app.get('/api/wechat/kf/unread')
async def kf_unread(_: None = Depends(kf_admin)) -> dict:
    """未读总数：运营面板上的小红点就靠它，不用把整个会话列表拉下来。"""
    db = await connect()
    try:
        row = await fetchone(db, 'SELECT COUNT(*) AS sessions, COALESCE(SUM(unread_count),0) AS messages FROM kf_sessions WHERE unread_count > 0')
        return {'sessions': int(row['sessions'] or 0), 'messages': int(row['messages'] or 0)}
    finally:
        await db.close()


@app.get("/api/knowledge")
async def list_knowledge(user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    await db.commit()
    rows = await fetchall(db, "SELECT * FROM knowledge_bases WHERE user_id=? AND status='active' ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, updated_at DESC", (user_id, DEFAULT_KNOWLEDGE_NAME))
    subscription_sources = {str(item['mirror_knowledge_id']): str(item['source_knowledge_id']) for item in await fetchall(db, 'SELECT source_knowledge_id,mirror_knowledge_id FROM knowledge_subscriptions WHERE user_id=?', (user_id,))}
    items = []
    for row in rows:
        source_name, missing, count = '', False, int(row['document_count'] or 0)
        subscription_source = subscription_sources.get(str(row['id']), '')
        if str(row['mirror_of'] or ''):
            # 共享 / 订阅镜像：读列表时就与来源对齐，来源更新后这里自动是最新的
            info = await sync_mirror(db, row)
            source_name, missing, count = info['source_name'], info['source_missing'], info['count']
        items.append(knowledge_view(row, live_document_count=count, source_name=source_name, source_missing=missing, subscribed=bool(subscription_source), subscription_source=subscription_source))
    await db.commit()
    await db.close()
    return items


@app.get("/api/market")
async def market_knowledge(query: str = Query(default="", max_length=80), category: str = Query(default="", max_length=40), user_id: str | None = Depends(current_user_optional)) -> list[dict]:
    """Return explicitly published knowledge bases plus the viewer's subscription state."""
    db = await connect()
    subscribed = {str(item['source_knowledge_id']) for item in await fetchall(db, 'SELECT source_knowledge_id FROM knowledge_subscriptions WHERE user_id=?', (user_id,))} if user_id else set()
    clauses = ["k.visibility='public'", "k.status='active'"]
    params: list[str] = []
    if query.strip():
        clauses.append("(k.name LIKE ? OR k.description LIKE ?)")
        term = f"%{query.strip()}%"
        params.extend([term, term])
    if category.strip():
        clauses.append("k.category=?")
        params.append(category.strip())
    rows = await fetchall(db, f"SELECT k.id,k.user_id,k.name,k.description,k.icon,k.avatar,k.category,k.subscribers,k.document_count,k.published_at,k.updated_at,u.nickname AS publisher_name,u.avatar AS publisher_avatar FROM knowledge_bases k JOIN users u ON u.id=k.user_id WHERE {' AND '.join(clauses)} ORDER BY k.subscribers DESC,k.published_at DESC,k.updated_at DESC LIMIT 50", tuple(params))
    await db.close()
    items = []
    for row in rows:
        value = {key: item for key, item in (row_dict(row) or {}).items() if key != 'user_id'}
        value.update({
            'avatar': (f"/api/knowledge-avatars/{row['id']}?v={hashlib.sha1(str(row['updated_at'] or '').encode()).hexdigest()[:10]}" if row['avatar'] else ''),
            'publisher_name': str(row['publisher_name'] or '微信用户'),
            'publisher_avatar': str(row['publisher_avatar'] or ''),
            'documents': int(row['document_count'] or 0),
            'subscribers': int(row['subscribers'] or 0),
            'subscribed': str(row['id']) in subscribed,
            'owned': bool(user_id and str(row['user_id']) == user_id),
        })
        items.append(value)
    return items


@app.get('/api/subscriptions')
async def list_subscriptions(user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect()
    rows = await fetchall(db, 'SELECT k.* FROM knowledge_bases k JOIN knowledge_subscriptions s ON s.mirror_knowledge_id=k.id WHERE s.user_id=? AND k.status=? ORDER BY s.created_at DESC', (user_id, 'active'))
    items = []
    for row in rows:
        info = await sync_mirror(db, row)
        items.append(knowledge_view(row, live_document_count=info['count'], source_name=info['source_name'], source_missing=info['source_missing'], subscribed=True, subscription_source=str(row['mirror_of'] or '')))
    await db.commit()
    await db.close()
    return items


@app.post('/api/subscriptions')
async def subscribe_knowledge(payload: KnowledgeSubscriptionCreate, user_id: str = Depends(current_user)) -> dict:
    rate_limit(f'kb-subscribe:{user_id}', 30, 60, '操作过于频繁，请稍后再试')
    source_id = payload.knowledge_id.strip()
    db = await connect()
    source = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND visibility='public' AND status='active'", (source_id,))
    if not source:
        await db.close(); raise HTTPException(404, '公开知识库不存在或已下架')
    if str(source['user_id'] or '') == user_id:
        await db.close(); raise HTTPException(409, '这是你自己的知识库，无需订阅')
    existing = await fetchone(db, 'SELECT mirror_knowledge_id FROM knowledge_subscriptions WHERE user_id=? AND source_knowledge_id=?', (user_id, source_id))
    if existing:
        mirror = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (str(existing['mirror_knowledge_id']), user_id))
        if mirror:
            info = await sync_mirror(db, mirror)
            subscribers = await refresh_subscriber_count(db, source_id)
            await db.commit(); await db.close()
            return {'knowledge_id': str(mirror['id']), 'name': str(mirror['name'] or ''), 'documents': int(info['count'] or 0), 'subscribers': subscribers, 'already': True, 'message': f"已经在订阅中，共 {int(info['count'] or 0)} 份资料"}
        await db.execute('DELETE FROM knowledge_subscriptions WHERE user_id=? AND source_knowledge_id=?', (user_id, source_id))
    timestamp = now()
    mirror_id = uuid.uuid4().hex
    await db.execute(
        'INSERT INTO knowledge_bases(id,user_id,name,description,icon,avatar,document_count,visibility,status,created_at,updated_at,mirror_of,mirror_owner,mirror_state,mirror_token,mirror_at)'
        ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (mirror_id, user_id, str(source['name']), str(source['description'] or ''), str(source['icon'] or DEFAULT_KNOWLEDGE_ICON), str(source['avatar'] or ''), 0, 'private', 'active',
         timestamp, timestamp, source_id, str(source['user_id']), 'ok', '', timestamp),
    )
    await db.execute('INSERT INTO knowledge_subscriptions(user_id,source_knowledge_id,mirror_knowledge_id,created_at) VALUES(?,?,?,?)', (user_id, source_id, mirror_id, timestamp))
    mirror = await fetchone(db, 'SELECT * FROM knowledge_bases WHERE id=?', (mirror_id,))
    info = await sync_mirror(db, mirror)
    subscribers = await refresh_subscriber_count(db, source_id)
    await db.commit(); await db.close()
    return {'knowledge_id': mirror_id, 'name': str(source['name'] or ''), 'documents': int(info['count'] or 0), 'subscribers': subscribers, 'already': False, 'message': f"已订阅「{source['name']}」，共 {int(info['count'] or 0)} 份资料"}


@app.delete('/api/subscriptions/{source_knowledge_id}')
async def unsubscribe_knowledge(source_knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    rate_limit(f'kb-unsubscribe:{user_id}', 30, 60, '操作过于频繁，请稍后再试')
    db = await connect()
    row = await fetchone(db, 'SELECT mirror_knowledge_id FROM knowledge_subscriptions WHERE user_id=? AND source_knowledge_id=?', (user_id, source_knowledge_id))
    if not row:
        subscribers = await refresh_subscriber_count(db, source_knowledge_id)
        await db.commit(); await db.close()
        return {'ok': True, 'removed': False, 'subscribers': subscribers}
    await db.execute('DELETE FROM knowledge_subscriptions WHERE user_id=? AND source_knowledge_id=?', (user_id, source_knowledge_id))
    await purge_subscription_mirror(db, str(row['mirror_knowledge_id']), user_id)
    subscribers = await refresh_subscriber_count(db, source_knowledge_id)
    await db.commit(); await db.close()
    return {'ok': True, 'removed': True, 'subscribers': subscribers}


async def publish_knowledge_worker(job_id: str, *, knowledge_id: str, user_id: str) -> dict:
    """后台做发布前的合规扫描与分类：最多 80 份资料的正文扫描是 CPU 活，不占请求。"""
    await jobs_service.set_progress(job_id, '正在做内容合规检查…')
    db = await connect()
    try:
        kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
        if not kb:
            raise RuntimeError('知识库不存在')
        documents = await fetchall(
            db,
            "SELECT filename,organized_title,summary,extracted_text FROM documents WHERE knowledge_id=? AND status='completed' ORDER BY updated_at DESC LIMIT 80",
            (knowledge_id,),
        )
        if not documents:
            raise RuntimeError('知识库还没有完成解析的资料，暂时不能发布')
        review_parts = [str(kb['name'] or ''), str(kb['description'] or '')]
        for document in documents:
            review_parts.extend([
                str(document['organized_title'] or ''),
                str(document['filename'] or ''),
                str(document['summary'] or ''),
                str(document['extracted_text'] or '')[:6000],
            ])
        findings = knowledge_policy.review_content(*review_parts)
        if findings:
            raise RuntimeError(f'{findings[0].message}请修改或删除相关内容后再发布。')
        category = knowledge_policy.classify_content(*review_parts)
        published_at = now()
        await db.execute(
            "UPDATE knowledge_bases SET visibility='public',category=?,published_at=?,updated_at=? WHERE id=? AND user_id=? AND status='active'",
            (category, published_at, published_at, knowledge_id, user_id),
        )
        await db.commit()
        count = await fetchone(db, 'SELECT COUNT(*) AS count FROM knowledge_subscriptions WHERE source_knowledge_id=?', (knowledge_id,))
    finally:
        await db.close()
    return {
        'ok': True,
        'published': True,
        'category': category,
        'published_at': published_at,
        'subscribers': int(count['count'] or 0) if count else 0,
        'reviewed_documents': len(documents),
        'message': f'已发布到「{category}」分类',
    }


@app.post('/api/knowledge/{knowledge_id}/publish')
async def publish_knowledge(knowledge_id: str, payload: KnowledgePublishUpdate, user_id: str = Depends(current_user)) -> dict:
    """上架 / 下架知识库广场。

    下架是瞬时动作，当场做完；上架要通读最多 80 份资料的正文做合规检查与分类，
    可能超过容器通道单次调用上限，所以只做校验后排队，客户端轮询 /api/jobs/{id}。
    """
    rate_limit(f'kb-publish:{user_id}', 20, 60, '操作过于频繁，请稍后再试')
    db = await connect()
    try:
        kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
        if not kb:
            raise HTTPException(404, '知识库不存在')
        if str(kb['mirror_of'] or ''):
            raise HTTPException(403, '共享或订阅知识库不能发布到广场')
        if str(kb['name'] or '') in (DEFAULT_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_NAME):
            raise HTTPException(403, '系统默认知识库不能发布到广场，请新建个人知识库后再发布')
        if not payload.published:
            await db.execute("UPDATE knowledge_bases SET visibility='private',published_at='',updated_at=? WHERE id=? AND user_id=?", (now(), knowledge_id, user_id))
            await db.commit()
            return {'ok': True, 'published': False, 'message': '已从知识库广场下架'}
        if not payload.acknowledged:
            raise HTTPException(422, '请先确认内容合规声明')
        ready = await fetchone(db, "SELECT COUNT(*) AS count FROM documents WHERE knowledge_id=? AND status='completed'", (knowledge_id,))
        if not int((ready['count'] if ready else 0) or 0):
            raise HTTPException(422, '知识库还没有完成解析的资料，暂时不能发布')
    finally:
        await db.close()
    job_id = await jobs_service.create(user_id, 'kb_publish', {'knowledge_id': knowledge_id})
    jobs_service.spawn(job_id, lambda jid: publish_knowledge_worker(jid, knowledge_id=knowledge_id, user_id=user_id))
    return {'pending': True, 'job_id': job_id}



@app.post("/api/knowledge")
async def create_knowledge(payload: KnowledgeCreate, user_id: str = Depends(current_user)) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "知识库名称不能为空")
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    # 配额只算用户主动新建的个人知识库；默认库、共享收件箱和订阅镜像都不占名额。
    count = await fetchone(db, "SELECT COUNT(*) AS count FROM knowledge_bases WHERE user_id=? AND status='active' AND COALESCE(mirror_of,'')='' AND name NOT IN (?,?)", (user_id, DEFAULT_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_NAME))
    limits = limits_for_user(user)
    if not account_state(user, 0)['entitlements']['can_create_knowledge']:
        await db.close(); raise HTTPException(403, "免费试用已结束，开通会员后可继续新建知识库")
    if int(count['count']) >= limits['knowledge_bases']:
        await db.close(); raise HTTPException(403, f"{limits['label']}最多创建 {limits['knowledge_bases']} 个知识库，请升级会员")
    if name == DEFAULT_KNOWLEDGE_NAME:
        await db.close()
        raise HTTPException(409, "默认知识库已存在")
    item = (uuid.uuid4().hex, user_id, name, payload.description.strip(), payload.icon, 0, "active", now(), now())
    await db.execute("INSERT INTO knowledge_bases(id,user_id,name,description,icon,document_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", item); await db.commit(); await db.close()
    return {"id": item[0], "user_id": user_id, "name": name, "description": item[3], "icon": payload.icon, "document_count": 0, "status": "active", "created_at": item[7], "updated_at": item[8]}


@app.delete("/api/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id)); user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    if not kb:
        await db.close(); raise HTTPException(404, "知识库不存在")
    avatar_ref = str(kb['avatar'] or '') if not str(kb['mirror_of'] or '') else ''
    subscription_source = await subscription_source_id(db, user_id, knowledge_id)
    if subscription_source:
        await db.execute('DELETE FROM knowledge_subscriptions WHERE user_id=? AND source_knowledge_id=?', (user_id, subscription_source))
    for subscription in await fetchall(db, 'SELECT user_id,mirror_knowledge_id FROM knowledge_subscriptions WHERE source_knowledge_id=?', (knowledge_id,)):
        mirror_id = str(subscription['mirror_knowledge_id'])
        if mirror_id != knowledge_id:
            await purge_subscription_mirror(db, mirror_id, str(subscription['user_id']))
    files = await fetchall(db, "SELECT storage_path FROM documents WHERE knowledge_id=? AND user_id=?", (knowledge_id, user_id))
    await db.execute("DELETE FROM chunks_fts WHERE knowledge_id=?", (knowledge_id,))
    await db.execute('DELETE FROM conversations WHERE knowledge_id=? AND user_id=?', (knowledge_id, user_id))
    # 邀请链接跟着库一起作废：源库没了，留着 rows 只会让清理和排查变脏
    await db.execute("DELETE FROM knowledge_shares WHERE knowledge_id=?", (knowledge_id,))
    await db.execute("DELETE FROM knowledge_bases WHERE id=? AND user_id=?", (knowledge_id, user_id))
    if subscription_source:
        await refresh_subscriber_count(db, subscription_source)
    await db.commit(); await db.close()
    # 这个库可能正被好友共享着：共享过来的镜像要同步标记失效，不能继续当没事一样可读；
    # 磁盘上的原文也可能还被共享库引用，所以统一走「没人引用才删」的判断。
    db2 = await connect()
    for mirror in await fetchall(db2, "SELECT * FROM knowledge_bases WHERE mirror_of=? AND status='active'", (knowledge_id,)):
        await sync_mirror(db2, mirror)
    for row in files:
        await unlink_if_unreferenced(db2, str(row["storage_path"] or ''))
    await db2.commit(); await db2.close()
    if avatar_ref:
        await storage.delete(avatar_ref)
    return {"ok": True}


@app.get("/api/knowledge/{knowledge_id}")
async def knowledge_detail(knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id));
    if not kb: await db.close(); raise HTTPException(404, "知识库不存在")
    # 打开知识库就算一次使用：最近知识库列表按这个时间从左到右排
    await touch_knowledge_used(db, knowledge_id); await db.commit()
    subscription_source = await subscription_source_id(db, user_id, knowledge_id)
    # 好友共享过来的库：打开先与来源对齐，回来的一定是最新一版
    info = await sync_mirror(db, kb) if str(kb['mirror_of'] or '') else {'source_name': '', 'source_missing': False, 'count': 0}
    docs = await fetchall(db, "SELECT id,filename,file_type,file_size,page_count,status,progress,error_message,organized_title,summary,tags_json,key_points_json,organize_status,organize_method,organized_at,folder_id,created_at,updated_at FROM documents WHERE knowledge_id=? AND status!='deleted' ORDER BY created_at DESC", (knowledge_id,)); await db.close()
    view = knowledge_view(kb, live_document_count=len(docs), source_name=info['source_name'], source_missing=info['source_missing'], subscribed=bool(subscription_source), subscription_source=subscription_source)
    return {"knowledge": view, "documents": [document_view(x) for x in docs], "read_only": view['read_only']}


SUGGESTION_FALLBACK = [
    '这个知识库主要包含哪些内容？',
    '帮我总结各文件的核心要点',
    '这些资料之间有什么关联？',
    '最值得优先看哪几份资料？',
]


async def _generate_suggestions(scope_name: str, samples: list[dict], scope_label: str = '知识库') -> list[str]:
    """根据资料范围名称 + 文件清单用 LLM 猜用户最想问的问题（返回空列表表示失败）。"""
    from .services.llm import provider_config
    config = provider_config('')
    if not config:
        return []
    base_url, api_key, model = config
    endpoint = f"{base_url.rstrip('/')}/chat/completions" if base_url.rstrip('/').endswith('/v1') else f"{base_url.rstrip('/')}/v1/chat/completions"
    listing = '\n'.join(
        f"- {str(r.get('filename') or '')}" + (f"：{str(r.get('summary') or '')[:60]}" if r.get('summary') else '')
        for r in samples[:20]
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10)) as client:
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={
                "model": model, "temperature": 0.7, "max_tokens": 400,
                "messages": [
                    {"role": "system", "content": f"你是知识库助手。根据给出的{scope_label}名称与文件清单，猜测用户最想问的 4 个问题（要能直接由这些文件回答）。只输出 JSON 数组（字符串元素），不要输出任何其他内容。每个问题不超过 30 个字，用中文。"},
                    {"role": "user", "content": f"{scope_label}名称：{scope_name}\n文件清单：\n{listing}"},
                ],
            })
            response.raise_for_status()
            text = str(response.json()["choices"][0]["message"]["content"] or "").strip()
        start, end = text.find('['), text.rfind(']')
        if start < 0 or end <= start:
            return []
        parsed = json.loads(text[start:end + 1])
        if not isinstance(parsed, list):
            return []
        return [str(q).strip() for q in parsed if str(q).strip()][:6]
    except Exception:
        return []


async def suggestions_worker(job_id: str, *, knowledge_id: str, folder_id: str, fingerprint: str, scope_name: str, rows: list[dict], scope: str) -> dict:
    """后台生成推荐问题并写缓存：模型调用可能到几十秒，不能占着请求等。"""
    await jobs_service.set_progress(job_id, '正在生成推荐问题…')
    questions = await _generate_suggestions(scope_name, rows, '文件夹' if folder_id else '知识库')
    if not questions:
        questions = list(SUGGESTION_FALLBACK)
    payload = json.dumps(questions, ensure_ascii=False)
    db = await connect()
    try:
        if folder_id:
            await db.execute("INSERT INTO folder_suggestions(knowledge_id,folder_id,fingerprint,questions_json,created_at) VALUES(?,?,?,?,?) ON CONFLICT(knowledge_id,folder_id) DO UPDATE SET fingerprint=excluded.fingerprint,questions_json=excluded.questions_json,created_at=excluded.created_at", (knowledge_id, folder_id, fingerprint, payload, now()))
        else:
            await db.execute("INSERT INTO knowledge_suggestions(knowledge_id,fingerprint,questions_json,created_at) VALUES(?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET fingerprint=excluded.fingerprint,questions_json=excluded.questions_json,created_at=excluded.created_at", (knowledge_id, fingerprint, payload, now()))
        await db.commit()
    finally:
        await db.close()
    return {"questions": questions, "scope": scope}


@app.get("/api/knowledge/{knowledge_id}/suggestions")
async def knowledge_suggestions(knowledge_id: str, folder_id: str = "", user_id: str = Depends(current_user)) -> dict:
    """推荐问题：LLM 按文件清单生成一次，按 数量+最新更新时间 指纹缓存，资料变化自动失效。

    传 folder_id 时范围收窄到该文件夹（文件夹会话就该问这个文件夹里的资料）；
    没有任何已完成资料时直接返回空列表，前端不展示无意义的提问提示。
    命中缓存直接返回；未命中时登记后台任务并回 pending=true，前端轮询任务结果
    ——模型生成常常超过云托管单次调用 15s 的上限。
    """
    db = await connect()
    kb = await fetchone(db, "SELECT id,name FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb:
        await db.close(); raise HTTPException(404, "知识库不存在")
    folder = None
    if folder_id:
        folder = await fetchone(db, "SELECT id,name FROM folders WHERE id=? AND knowledge_id=? AND user_id=?", (folder_id, knowledge_id, user_id))
        if not folder:
            await db.close(); raise HTTPException(404, "文件夹不存在")
    scope_sql = " AND folder_id=?" if folder else ""
    scope_params: tuple = (knowledge_id, folder_id) if folder else (knowledge_id,)
    stats = await fetchone(db, f"SELECT COUNT(*) AS n, COALESCE(MAX(updated_at),'') AS latest FROM documents WHERE knowledge_id=? AND status!='deleted'{scope_sql}", scope_params)
    fingerprint = f"{stats['n']}:{stats['latest']}"
    if folder:
        cached = await fetchone(db, "SELECT fingerprint,questions_json FROM folder_suggestions WHERE knowledge_id=? AND folder_id=?", (knowledge_id, folder_id))
    else:
        cached = await fetchone(db, "SELECT fingerprint,questions_json FROM knowledge_suggestions WHERE knowledge_id=?", (knowledge_id,))
    if cached and str(cached['fingerprint'] or '') == fingerprint:
        try:
            questions = json.loads(cached['questions_json'])
        except json.JSONDecodeError:
            questions = []
        await db.close()
        return {"questions": questions, "scope": "folder" if folder else "knowledge"}
    # 没有已完成资料时不必生成：让前端直接不展示提问提示
    if not int(stats['n'] or 0):
        await db.close()
        return {"questions": [], "scope": "folder" if folder else "knowledge"}
    rows = await fetchall(db, f"SELECT filename,summary FROM documents WHERE knowledge_id=? AND status='completed'{scope_sql} ORDER BY updated_at DESC LIMIT 20", scope_params)
    await db.close()
    scope_name = f"{kb['name']} / {folder['name']}" if folder else kb['name']
    scope = "folder" if folder else "knowledge"
    job_id = await jobs_service.create(user_id, 'kb_suggestions', {'knowledge_id': knowledge_id, 'folder_id': folder_id})
    jobs_service.spawn(job_id, lambda jid: suggestions_worker(jid, knowledge_id=knowledge_id, folder_id=folder_id, fingerprint=fingerprint, scope_name=scope_name, rows=[dict(r) for r in rows], scope=scope))
    return {"questions": [], "pending": True, "job_id": job_id, "scope": scope}



@app.get("/api/knowledge/{knowledge_id}/folders")
async def list_folders(knowledge_id: str, user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect()
    kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb: await db.close(); raise HTTPException(404, "知识库不存在")
    rows = await fetchall(db, "SELECT id,name,created_at,(SELECT COUNT(*) FROM documents WHERE folder_id=folders.id AND status!='deleted') AS document_count FROM folders WHERE knowledge_id=? AND user_id=? ORDER BY created_at ASC", (knowledge_id, user_id))
    await db.close()
    return [row_dict(r) for r in rows]


@app.post("/api/knowledge/{knowledge_id}/folders")
async def create_folder(payload: FolderCreate, knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb: await db.close(); raise HTTPException(404, "知识库不存在")
    await writable_knowledge_or_close(db, knowledge_id, user_id)
    name = payload.name.strip()
    dup = await fetchone(db, "SELECT id FROM folders WHERE knowledge_id=? AND name=?", (knowledge_id, name))
    if dup: await db.close(); raise HTTPException(409, "已存在同名文件夹")
    count = await fetchone(db, "SELECT COUNT(*) AS c FROM folders WHERE knowledge_id=?", (knowledge_id,))
    if int(count["c"]) >= 50: await db.close(); raise HTTPException(413, "文件夹数量已达上限（50 个）")
    folder_id = uuid.uuid4().hex; timestamp = now()
    await db.execute("INSERT INTO folders(id,knowledge_id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?,?)", (folder_id, knowledge_id, user_id, name, timestamp, timestamp))
    await db.commit(); await db.close()
    return {"id": folder_id, "name": name, "document_count": 0}


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str, user_id: str = Depends(current_user)) -> dict:
    """删除文件夹：其中文档移回根目录，不删除文档本身。"""
    db = await connect()
    folder = await fetchone(db, "SELECT id,knowledge_id FROM folders WHERE id=? AND user_id=?", (folder_id, user_id))
    if not folder: await db.close(); raise HTTPException(404, "文件夹不存在")
    await writable_knowledge_or_close(db, str(folder['knowledge_id']), user_id)
    await db.execute("UPDATE documents SET folder_id='',updated_at=? WHERE folder_id=?", (now(), folder_id))
    await db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    await db.commit(); await db.close()
    return {"ok": True}


@app.patch("/api/documents/{document_id}/move")
async def move_document(document_id: str, payload: DocumentMove, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    doc = await fetchone(db, "SELECT id,knowledge_id FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not doc: await db.close(); raise HTTPException(404, "文档不存在")
    await writable_knowledge_or_close(db, str(doc["knowledge_id"]), user_id)
    if payload.folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (payload.folder_id, user_id, doc["knowledge_id"]))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
    await db.execute("UPDATE documents SET folder_id=?,updated_at=? WHERE id=?", (payload.folder_id, now(), document_id))
    await db.commit(); await db.close()
    return {"ok": True}


async def index_document(document_id: str) -> dict:
    """抽取正文 → 分块 → 全文索引 → 合规检查，并把结果写回文档行。

    直传回执、服务端转发上传、重新解析三条路径共用这一份实现，保证同一份资料
    不管从哪进来，检索、预览、整理的行为完全一致。

    按「后台任务」设计：解析（尤其 OCR）可能远超云托管单次调用 15s 的上限，
    调用方先把文档行建好再排它，前端靠文档状态轮询看进度。
    """
    db = await connect()
    row = await fetchone(db, "SELECT * FROM documents WHERE id=? AND status!='deleted'", (document_id,))
    await db.close()
    if not row:
        return {"status": "missing"}
    knowledge_id = str(row["knowledge_id"])
    filename = str(row["filename"] or "")
    suffix = str(row["file_type"] or "")
    db = await connect()
    try:
        # 重新解析前先清掉旧切片，否则同一份资料会在检索里出现两遍
        await db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)", (document_id,))
        await db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
        await db.execute("UPDATE documents SET status='processing',progress=15,error_message='',updated_at=? WHERE id=?", (now(), document_id))
        await db.commit()
    finally:
        await db.close()
    extracted = ""
    moderation_message = ""
    try:
        parsed_path = await storage.materialize(str(row["storage_path"] or ""), suffix=suffix)
        try:
            text, pages = extract_text(parsed_path, suffix)
            chunks = split_chunks(text)
        finally:
            parsed_path.unlink(missing_ok=True)
        extracted = text
        db = await connect()
        try:
            await db.execute("UPDATE documents SET page_count=?,status='embedding',progress=70,extracted_text=?,updated_at=? WHERE id=?", (pages, text, now(), document_id))
            for index, content in enumerate(chunks):
                chunk_id = uuid.uuid4().hex
                page = min(pages, index + 1) if pages else 1
                await db.execute("INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)", (chunk_id, document_id, knowledge_id, content, page, index, now()))
                await db.execute("INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)", (content, chunk_id, knowledge_id, filename, page))
            await db.execute("UPDATE documents SET status='completed',progress=100,updated_at=? WHERE id=?", (now(), document_id))
            await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (knowledge_id, now(), knowledge_id))
            moderation_message = await enforce_published_knowledge_policy(db, knowledge_id, filename, text)
            await db.commit()
        finally:
            await db.close()
    except Exception as exc:
        db = await connect()
        try:
            await db.execute("UPDATE documents SET status='failed',progress=0,error_message=?,updated_at=? WHERE id=?", (str(exc), now(), document_id))
            await db.commit()
        finally:
            await db.close()
        return {"status": "failed", "progress": 0, "error_message": str(exc)}
    # 没有可抽取正文（旧版 Office 格式、无文字图片）时不排整理任务，避免落一条空摘要；
    # 这类文件仍可正常打开原文预览。
    organized = bool(extracted.strip())
    if organized:
        await organize_document(document_id)
    return {"status": "completed", "progress": 100, "moderation_message": moderation_message, "organized": organized}


async def prepare_document_upload(*, knowledge_id: str, user_id: str, folder_id: str, client_name: str) -> dict:
    """文档上传的前置校验：库归属、可写、目录、配额、类型白名单。

    直传（小程序）与服务端转发（本地存储 / 测试）两条路共用，口径必须一致。
    """
    db = await connect()
    try:
        kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
        if not kb:
            raise HTTPException(404, "知识库不存在")
        await writable_knowledge(db, knowledge_id, user_id)
        user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
        if not user:
            raise HTTPException(404, "用户不存在")
        if folder_id:
            folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (folder_id, user_id, knowledge_id))
            if not folder:
                raise HTTPException(404, "目标文件夹不存在")
        limits = limits_for_user(user)
        if not account_state(user, 0)['entitlements']['can_upload']:
            raise HTTPException(403, "免费试用已结束，开通会员后可继续添加资料")
        safe_name = Path(client_name or "upload").name or "upload"
        suffix = Path(safe_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(415, "暂不支持该文件类型")
        return {"document_id": uuid.uuid4().hex, "safe_name": safe_name, "suffix": suffix, "limits": limits}
    finally:
        await db.close()


async def register_document_row(*, document_id: str, knowledge_id: str, user_id: str, safe_name: str, suffix: str, storage_ref: str, size: int, folder_id: str, status: str, progress: int) -> None:
    db = await connect()
    try:
        timestamp = now()
        await db.execute(
            "INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, knowledge_id, user_id, safe_name, suffix, size, storage_ref, status, progress, folder_id, timestamp, timestamp),
        )
        await db.commit()
    finally:
        await db.close()


@app.post("/api/uploads/direct")
async def uploads_direct(payload: UploadDirectRequest, user_id: str = Depends(current_user)) -> dict:
    """签发对象存储直传凭证（小程序上传的唯一通道）。

    云托管私有协议单次请求体上限 100KiB，文件字节过不了容器，所以文件由客户端
    直接写进对象存储。凭证按 COS「POST Object」规则签发，且在 policy 里锁死
    对象 key 与体积上限——客户端只能写这一个位置、写不了超过配额的体积。
    """
    if settings.storage_backend != 'cos':
        # 本地存储没有直传能力：调用方回退到服务端转发上传
        return {'mode': 'local'}
    if payload.kind == 'document':
        prepared = await prepare_document_upload(knowledge_id=payload.knowledge_id, user_id=user_id, folder_id=payload.folder_id, client_name=payload.filename)
        document_id = prepared['document_id']; safe_name = prepared['safe_name']; suffix = prepared['suffix']; limits = prepared['limits']
        db = await connect()
        try:
            used = await storage_used_bytes(db, user_id)
        finally:
            await db.close()
        if used >= limits['storage_bytes']:
            raise HTTPException(413, f"{limits['label']}可用空间 {human_size(limits['storage_bytes'])} 已满，升级会员可继续添加")
        key = f"uploads/documents/{document_id}_{safe_name}"
        storage_ref = storage.ref_from_full_key(storage.full_key(key))
        await register_document_row(document_id=document_id, knowledge_id=payload.knowledge_id, user_id=user_id, safe_name=safe_name, suffix=suffix, storage_ref=storage_ref, size=0, folder_id=payload.folder_id, status='pending_upload', progress=0)
        try:
            form = await storage.post_form(key, max_bytes=limits['max_file_bytes'], expires=UPLOAD_DIRECT_TTL)
        except storage.StorageError as exc:
            raise HTTPException(503, "对象存储暂不可用，请稍后重试") from exc
        return {'mode': 'cos', 'document_id': document_id, 'filename': safe_name, 'max_bytes': limits['max_file_bytes'], **form}
    if payload.kind == 'avatar':
        suffix = (payload.suffix or Path(payload.filename or '').suffix).lower()
        if suffix not in AVATAR_MEDIA_TYPES:
            suffix = '.jpg'
        safe_id = _avatar_safe_id(user_id)
        if not safe_id:
            raise HTTPException(422, '用户信息不完整')
        try:
            form = await storage.post_form(f'{AVATAR_DIR_NAME}/{safe_id}{suffix}', max_bytes=AVATAR_MAX_BYTES, expires=UPLOAD_DIRECT_TTL)
        except storage.StorageError as exc:
            raise HTTPException(503, "对象存储暂不可用，请稍后重试") from exc
        return {'mode': 'cos', 'suffix': suffix, 'max_bytes': AVATAR_MAX_BYTES, **form}
    if payload.kind != 'knowledge-avatar':
        raise HTTPException(422, 'kind 只支持 document / avatar / knowledge-avatar')
    knowledge_id = payload.knowledge_id.strip()
    db = await connect()
    kb = await fetchone(db, "SELECT id,mirror_of FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    await db.close()
    if not kb:
        raise HTTPException(404, '知识库不存在')
    if str(kb['mirror_of'] or ''):
        raise HTTPException(403, '共享或订阅知识库不能单独更换头像')
    suffix = (payload.suffix or Path(payload.filename or '').suffix).lower()
    if suffix not in AVATAR_MEDIA_TYPES:
        suffix = '.jpg'
    safe_id = _knowledge_avatar_safe_id(knowledge_id)
    if not safe_id:
        raise HTTPException(422, '知识库信息不完整')
    key = f'{KNOWLEDGE_AVATAR_DIR_NAME}/{safe_id}-{uuid.uuid4().hex[:10]}{suffix}'
    try:
        form = await storage.post_form(key, max_bytes=AVATAR_MAX_BYTES, expires=UPLOAD_DIRECT_TTL)
    except storage.StorageError as exc:
        raise HTTPException(503, "对象存储暂不可用，请稍后重试") from exc
    return {'mode': 'cos', 'suffix': suffix, 'max_bytes': AVATAR_MAX_BYTES, **form}


@app.post('/api/knowledge/{knowledge_id}/documents/{document_id}/complete')
async def complete_document_upload(background_tasks: BackgroundTasks, knowledge_id: str, document_id: str, payload: UploadCompleteRequest, user_id: str = Depends(current_user)) -> dict:
    """直传回执：核对对象、按真实体积复核配额，然后排后台解析。

    体积与配额都以服务端读到的对象信息为准，客户端上报的任何数字都不采信。
    """
    db = await connect()
    row = await fetchone(db, "SELECT * FROM documents WHERE id=? AND user_id=? AND knowledge_id=? AND status!='deleted'", (document_id, user_id, knowledge_id))
    if not row:
        await db.close(); raise HTTPException(404, '文档不存在')
    storage_ref = str(row['storage_path'] or '')
    if payload.key and storage.ref_from_full_key(payload.key) != storage_ref:
        await db.close(); raise HTTPException(409, '上传对象与登记的文档不一致')
    user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    limits = limits_for_user(user)
    try:
        size = await storage.object_size(storage_ref)
    except storage.StorageError as exc:
        await db.close(); raise HTTPException(404, '上传的文件不存在或已失效') from exc
    if size <= 0:
        await db.close(); raise HTTPException(422, '上传的文件为空')
    if size > limits['max_file_bytes']:
        await db.close()
        await storage.delete(storage_ref)
        raise HTTPException(413, f"单文件不能超过 {human_size(limits['max_file_bytes'])}")
    if await storage_used_bytes(db, user_id) + size > limits['storage_bytes']:
        await db.close()
        await storage.delete(storage_ref)
        raise HTTPException(413, f"{limits['label']}可用空间 {human_size(limits['storage_bytes'])} 已满，升级会员可继续添加")
    await db.execute("UPDATE documents SET file_size=?,status='processing',progress=15,updated_at=? WHERE id=?", (size, now(), document_id))
    await db.commit()
    await db.close()
    background_tasks.add_task(index_document, document_id)
    return {"id": document_id, "filename": str(row['filename'] or ''), "status": "processing", "progress": 15, "error_message": "", "organize_status": 'pending', "moderation_message": ""}


@app.post('/api/me/avatar/complete')
async def complete_avatar_upload(payload: UploadCompleteRequest, user_id: str = Depends(current_user)) -> dict:
    """头像直传回执：核对对象后写回用户资料，并删掉旧格式的遗留文件。"""
    suffix = Path(payload.key or '').suffix.lower()
    safe_id = _avatar_safe_id(user_id)
    if not payload.key or suffix not in AVATAR_MEDIA_TYPES or not safe_id or storage.full_key(f'{AVATAR_DIR_NAME}/{safe_id}{suffix}') != payload.key:
        raise HTTPException(409, '上传对象与头像不一致')
    ref = storage.ref_from_full_key(payload.key)
    try:
        size = await storage.object_size(ref)
    except storage.StorageError as exc:
        raise HTTPException(404, '上传的文件不存在或已失效') from exc
    if size <= 0 or size > AVATAR_MAX_BYTES:
        await storage.delete(ref)
        raise HTTPException(413, '头像不能超过 2MB')
    avatar_url = f'/api/avatars/{user_id}?v={int(time.time())}'
    db = await connect()
    await db.execute("UPDATE users SET avatar=?, updated_at=? WHERE id=? AND status='active'", (avatar_url, now(), user_id))
    await db.commit(); await db.close()
    for stale_suffix, stale_ref in _avatar_refs(user_id):
        if stale_suffix != suffix:
            await storage.delete(stale_ref)
    return {'avatar': avatar_url}


@app.post('/api/knowledge/{knowledge_id}/avatar/complete')
async def complete_knowledge_avatar_upload(knowledge_id: str, payload: UploadCompleteRequest, user_id: str = Depends(current_user)) -> dict:
    """知识库头像直传回执：写回新对象并删掉上一版。"""
    db = await connect()
    kb = await fetchone(db, "SELECT id,avatar,mirror_of FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb:
        await db.close(); raise HTTPException(404, '知识库不存在')
    if str(kb['mirror_of'] or ''):
        await db.close(); raise HTTPException(403, '共享或订阅知识库不能单独更换头像')
    old_ref = str(kb['avatar'] or '')
    safe_id = _knowledge_avatar_safe_id(knowledge_id)
    suffix = Path(payload.key or '').suffix.lower()
    if not payload.key or not safe_id or suffix not in AVATAR_MEDIA_TYPES or not payload.key.startswith(f'{settings.cos_prefix.strip().strip("/")}/{KNOWLEDGE_AVATAR_DIR_NAME}/{safe_id}-'):
        await db.close(); raise HTTPException(409, '上传对象与知识库头像不一致')
    ref = storage.ref_from_full_key(payload.key)
    try:
        size = await storage.object_size(ref)
    except storage.StorageError as exc:
        await db.close(); raise HTTPException(404, '上传的文件不存在或已失效') from exc
    if size <= 0 or size > AVATAR_MAX_BYTES:
        await db.close()
        await storage.delete(ref)
        raise HTTPException(413, '头像不能超过 2MB')
    stamp = now()
    await db.execute('UPDATE knowledge_bases SET avatar=?,updated_at=? WHERE id=? AND user_id=?', (ref, stamp, knowledge_id, user_id))
    await db.commit(); await db.close()
    if old_ref and old_ref != ref:
        await storage.delete(old_ref)
    return {'avatar': f'/api/knowledge-avatars/{knowledge_id}?v={int(time.time())}'}


@app.post("/api/knowledge/{knowledge_id}/documents")
async def upload_document(background_tasks: BackgroundTasks, knowledge_id: str, file: UploadFile = File(...), folder_id: str = Form(default=""), x_upload_filename: str | None = Header(default=None), user_id: str = Depends(current_user)) -> dict:
    """服务端转发式上传：本地存储模式与测试用。

    小程序在云托管下走对象存储直传（/api/uploads/direct）：容器通道的请求体
    上限 100KiB，文件字节不可能经这里转发。
    """
    prepared = await prepare_document_upload(knowledge_id=knowledge_id, user_id=user_id, folder_id=folder_id, client_name=unquote(x_upload_filename) if x_upload_filename else (file.filename or ''))
    document_id = prepared['document_id']; safe_name = prepared['safe_name']; suffix = prepared['suffix']; limits = prepared['limits']
    data = await file.read()
    if len(data) > limits['max_file_bytes']:
        raise HTTPException(413, f"单文件不能超过 {human_size(limits['max_file_bytes'])}")
    db = await connect()
    try:
        if await storage_used_bytes(db, user_id) + len(data) > limits['storage_bytes']:
            raise HTTPException(413, f"{limits['label']}可用空间 {human_size(limits['storage_bytes'])} 已满，升级会员可继续添加")
        try:
            storage_ref = await storage.save_bytes(data, f"uploads/documents/{document_id}_{safe_name}", content_type=file.content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream")
        except storage.StorageError as exc:
            raise HTTPException(503, "文件存储失败，请稍后重试") from exc
        timestamp = now()
        await db.execute("INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, knowledge_id, user_id, safe_name, suffix, len(data), storage_ref, "processing", 15, folder_id, timestamp, timestamp))
        await db.commit()
    finally:
        await db.close()
    background_tasks.add_task(index_document, document_id)
    return {"id": document_id, "filename": safe_name, "status": "processing", "progress": 15, "error_message": "", "organize_status": 'pending', "moderation_message": ""}


async def _storage_asset(ref: str, *, filename: str = '', media_type: str = 'application/octet-stream') -> dict:
    """对象存储资源 → 短时效直链；本地存储没有直链概念，如实回报让调用方决定回退。"""
    if not ref:
        raise HTTPException(404, '资源不存在')
    url = await storage.presigned_get(ref, expires=ASSET_URL_TTL)
    if not url:
        return {'mode': 'local', 'filename': filename, 'mime': media_type}
    return {'mode': 'cos', 'url': url, 'filename': filename, 'mime': media_type}


async def _resolve_asset(path: str, user_id: str | None) -> dict:
    """把一条后端资源路径换成客户端能直接取用的形式。

    权限口径与原接口逐条对齐：公开资源（头像 / 配图 / 分享文件）匿名可解析，
    文档与产物必须带登录态且只能解析自己的。这里只换交付方式，不换可见范围。
    """
    if path.startswith('/api/content/assets/'):
        asset = await content_service.asset(Path(path[len('/api/content/assets/'):]).name)
        if not asset:
            raise HTTPException(404, '资源不存在')
        if len(asset['bytes']) > CONTENT_ASSET_INLINE_MAX:
            raise HTTPException(413, '配图过大，暂不支持下发')
        return {'mode': 'inline', 'mime': asset['mime'], 'base64': base64.b64encode(asset['bytes']).decode('ascii')}
    if path.startswith('/api/article-assets/'):
        parts = path[len('/api/article-assets/'):].split('/')
        if len(parts) != 2 or not parts[0].isalnum():
            raise HTTPException(404, '资源不存在')
        name = Path(parts[1]).name
        return await _storage_asset(storage.reference(f'article_assets/{parts[0]}/{name}'), filename=name, media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream')
    if path.startswith('/api/avatars/'):
        for suffix, ref in _avatar_refs(path[len('/api/avatars/'):]):
            try:
                if await storage.exists(ref):
                    return await _storage_asset(ref, media_type=AVATAR_MEDIA_TYPES[suffix])
            except storage.StorageError:
                continue
        raise HTTPException(404, '头像不存在')
    if path.startswith('/api/knowledge-avatars/'):
        knowledge_id = path[len('/api/knowledge-avatars/'):]
        db = await connect()
        row = await fetchone(db, 'SELECT avatar FROM knowledge_bases WHERE id=? AND status=?', (knowledge_id, 'active'))
        await db.close()
        ref = str(row['avatar'] or '') if row else ''
        if not ref:
            raise HTTPException(404, '知识库头像不存在')
        return await _storage_asset(ref, media_type=mimetypes.guess_type(ref)[0] or 'image/jpeg')
    if path.startswith('/api/documents/') and path.endswith('/download'):
        if not user_id:
            raise HTTPException(401, '请先登录')
        document_id = path[len('/api/documents/'):-len('/download')]
        db = await connect()
        row = await fetchone(db, "SELECT storage_path,filename FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
        await db.close()
        if not row:
            raise HTTPException(404, '文档不存在')
        name = str(row['filename'] or '')
        return await _storage_asset(str(row['storage_path'] or ''), filename=name, media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream')
    if path.startswith('/api/artifacts/') and path.endswith('/download'):
        if not user_id:
            raise HTTPException(401, '请先登录')
        artifact_id = path[len('/api/artifacts/'):-len('/download')]
        db = await connect()
        row = await artifact_service.owned(db, artifact_id, user_id)
        await db.close()
        if not row:
            raise HTTPException(404, '文件不存在')
        return await _storage_asset(artifact_service.stored_ref(row), filename=str(row['filename']), media_type=str(row['mime'] or 'application/octet-stream'))
    if path.startswith('/api/shares/') and '/files/' in path:
        share_id, _, raw_index = path[len('/api/shares/'):].partition('/files/')
        if not valid_share_id(share_id) or not raw_index.isdigit() or int(raw_index) > 32:
            raise HTTPException(404, '文件不存在或已失效')
        db = await connect()
        card = await fetchone(db, 'SELECT user_id,files_json FROM share_cards WHERE id=?', (share_id,))
        if not card:
            await db.close(); raise HTTPException(404, '文件不存在或已失效')
        files = decode_sources(card['files_json'])
        index = int(raw_index)
        if index >= len(files):
            await db.close(); raise HTTPException(404, '文件不存在或已失效')
        row = await fetchone(db, 'SELECT id,filename,mime,storage_path FROM artifacts WHERE id=? AND user_id=?', (str(files[index].get('artifact_id') or ''), card['user_id']))
        await db.close()
        if not row:
            raise HTTPException(404, '文件不存在或已失效')
        return await _storage_asset(str(row['storage_path'] or ''), filename=str(row['filename']), media_type=str(row['mime'] or 'application/octet-stream'))
    raise HTTPException(404, '不支持的资源路径')


@app.post('/api/assets/resolve')
async def resolve_assets(payload: AssetResolveRequest, user_id: str | None = Depends(current_user_optional)) -> dict:
    """批量解析资源路径：小程序走容器通道时用它换预签名地址或内联字节。

    一次能解析多条，图片列表因此只需要一个来回；单条失败不影响其余条目。
    """
    items: dict[str, dict] = {}
    for raw in payload.paths[:32]:
        original = str(raw or '')
        path = original.split('?', 1)[0].split('#', 1)[0]
        if not path:
            continue
        try:
            items[original] = await _resolve_asset(path, user_id)
        except HTTPException as exc:
            items[original] = {'error': str(exc.detail)}
    return {'items': items}


@app.get('/api/jobs/{job_id}')
async def job_status(job_id: str, user_id: str = Depends(current_user)) -> dict:
    """长任务进度：客户端轮询它，替代一个可能超过云托管 15s 上限的同步请求。"""
    row = await jobs_service.load(job_id)
    if not row or str(row['user_id']) != user_id:
        raise HTTPException(404, '任务不存在')
    return jobs_service.view(row)


async def process_article_import(document_id: str, *, user_id: str, url: str, limits: dict) -> None:
    """后台抓取公众号文章：正文配图要逐张下载，耗时不可控，不能占着请求等。

    失败原因写回文档行，前端在资料列表里看到的就是同一套错误提示。
    """
    assets_dir = settings.upload_path / "article_assets" / document_id

    async def fail(message: str) -> None:
        shutil.rmtree(assets_dir, ignore_errors=True)
        db = await connect()
        try:
            await db.execute("UPDATE documents SET status='failed',progress=0,error_message=?,updated_at=? WHERE id=?", (message, now(), document_id))
            await db.commit()
        finally:
            await db.close()

    db = await connect()
    try:
        await db.execute("UPDATE documents SET status='processing',progress=10,updated_at=? WHERE id=?", (now(), document_id))
        await db.commit()
    finally:
        await db.close()
    try:
        article = await fetch_wechat_article(url, assets_dir, f"/api/article-assets/{document_id}")
    except ArticleFetchError as exc:
        await fail(str(exc))
        return
    full_html = build_document_html(article["title"], article["account"], article["html"], url.strip())
    safe_name = f"{article['title'].replace('/', '_')[:60]}.html"
    html_data = full_html.encode('utf-8')
    asset_files = [path for path in assets_dir.iterdir() if path.is_file()] if assets_dir.is_dir() else []
    file_size = len(html_data) + sum(path.stat().st_size for path in asset_files)
    db = await connect()
    try:
        over_quota = await storage_used_bytes(db, user_id) + file_size > limits['storage_bytes']
    finally:
        await db.close()
    if over_quota:
        await fail(f"{limits['label']}可用空间 {human_size(limits['storage_bytes'])} 已满，升级会员可继续添加")
        return
    try:
        for asset_path in asset_files:
            await storage.save_file(asset_path, f"article_assets/{document_id}/{asset_path.name}", content_type=mimetypes.guess_type(asset_path.name)[0] or 'application/octet-stream')
        storage_ref = await storage.save_bytes(html_data, f"uploads/documents/{document_id}_{safe_name}", content_type='text/html; charset=utf-8')
    except storage.StorageError:
        await fail("文章资源存储失败，请稍后重试")
        return
    finally:
        shutil.rmtree(assets_dir, ignore_errors=True)
    db = await connect()
    try:
        await db.execute("UPDATE documents SET filename=?,file_type='.html',file_size=?,storage_path=?,updated_at=? WHERE id=?", (safe_name, file_size, storage_ref, now(), document_id))
        await db.commit()
    finally:
        await db.close()
    await index_document(document_id)


@app.post("/api/knowledge/{knowledge_id}/import-article")
async def import_article(payload: ArticleImportRequest, background_tasks: BackgroundTasks, knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    """导入微信公众号文章：先登记一条 processing 的资料，再后台抓取入库。

    抓取要下载正文里的每张配图，整体耗时经常超过云托管单次调用的 15s 上限，
    所以这里只做校验与登记，进度由前端看资料状态轮询。
    """
    rate_limit(f"import:{user_id}", 10, 60, "导入太频繁了，请稍后再试")
    db = await connect()
    kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    limits = limits_for_user(user)
    if not kb:
        await db.close(); raise HTTPException(404, "知识库不存在")
    await writable_knowledge_or_close(db, knowledge_id, user_id)
    if payload.folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (payload.folder_id, user_id, knowledge_id))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
    if not account_state(user, 0)['entitlements']['can_upload']:
        await db.close(); raise HTTPException(403, "免费试用已结束，开通会员后可继续添加资料")
    document_id = uuid.uuid4().hex
    title = payload.url.strip()[:60]
    await db.execute("INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, knowledge_id, user_id, title, ".html", 0, "", "processing", 10, payload.folder_id, now(), now()))
    await db.commit(); await db.close()
    background_tasks.add_task(process_article_import, document_id, user_id=user_id, url=payload.url, limits=limits)
    return {"id": document_id, "filename": title, "title": title, "account": "", "image_count": 0, "status": "processing", "progress": 10, "error_message": "", "organize_status": 'pending', "moderation_message": ""}



@app.get("/api/article-assets/{document_id}/{filename}")
async def article_asset(document_id: str, filename: str) -> Response:
    """文章图片资源。文档 ID 为随机不可枚举串，资源不含私密信息，无需登录态。"""
    safe = Path(filename).name
    if not document_id.isalnum() or safe != filename:
        raise HTTPException(404, "资源不存在")
    ref = storage.reference(f"article_assets/{document_id}/{safe}")
    try:
        data = await storage.read_bytes(ref)
    except storage.StorageError as exc:
        raise HTTPException(404, "资源不存在") from exc
    media_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)


# --- 运营文案（使用技巧） -------------------------------------------------
# 内容源文件在仓库 content/ 目录里维护，部署时导入数据库（scripts/import_content.py，
# 或服务首次启动的自动导入），运行时统一从数据库读：
#   * 改文案不需要发版，也不需要把 content/ 目录带进容器；
#   * 正文的 Markdown 在导入时解析成 blocks 存库，请求时不重复解析。
# 这三条接口不挂登录态：内容不含用户数据，且小程序 <image> 无法携带
# Authorization 头，配图必须能匿名取到。


@app.get("/api/content/tips")
async def tips_index() -> dict:
    try:
        return await content_service.tips_index()
    except content_service.ContentError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/content/tips/{entry_id}")
async def tips_entry(entry_id: str) -> dict:
    try:
        entry = await content_service.tip_entry(entry_id)
    except content_service.ContentError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not entry:
        raise HTTPException(404, "技巧不存在")
    return entry


@app.get("/api/content/legal")
async def legal_index() -> dict:
    """合规文档清单（标题、摘要、更新日期）。

    与使用技巧一样匿名可读：内容里没有用户数据，而且是「上线审核要能直接看到」
    的材料，不能因为没登录就打不开。
    """
    try:
        return await content_service.legal_index()
    except content_service.ContentError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/content/legal/{doc_id}")
async def legal_document(doc_id: str) -> dict:
    """合规文档正文：按 block 结构下发，前端用原生组件渲染。"""
    try:
        doc = await content_service.legal_doc(doc_id)
    except content_service.ContentError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc


@app.get("/api/content/assets/{filename}")
async def content_asset(filename: str) -> Response:
    asset = await content_service.asset(filename)
    if not asset:
        raise HTTPException(404, "资源不存在")
    return Response(
        content=asset["bytes"],
        media_type=asset["mime"],
        headers={
            "Cache-Control": "public, max-age=86400",
            "ETag": f'"{hashlib.sha1(asset["bytes"]).hexdigest()}"',
        },
    )


@app.post("/api/documents/{document_id}/retry")
async def retry_document(document_id: str, background_tasks: BackgroundTasks, user_id: str = Depends(current_user)) -> dict:
    """重新解析：解析耗时不可控，只登记状态、排后台任务，前端看文档状态轮询。"""
    db = await connect(); row = await fetchone(db, "SELECT * FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not row:
        await db.close(); raise HTTPException(404, "文档不存在")
    await writable_knowledge_or_close(db, str(row["knowledge_id"]), user_id)
    if not await storage.exists(str(row["storage_path"] or '')):
        await db.close(); raise HTTPException(404, "原文文件不存在，无法重新解析")
    await db.execute("UPDATE documents SET status='processing',progress=15,error_message='',updated_at=? WHERE id=?", (now(), document_id))
    await db.commit(); await db.close()
    background_tasks.add_task(index_document, document_id)
    return {"id": document_id, "status": "processing", "progress": 15, "organize_status": "pending", "moderation_message": ""}


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); row = await fetchone(db, "SELECT storage_path,knowledge_id FROM documents WHERE id=? AND user_id=?", (document_id, user_id));
    if not row:
        await db.close(); return {"ok": True}
    await writable_knowledge_or_close(db, str(row["knowledge_id"]), user_id)
    storage_path = str(row["storage_path"] or "")
    await db.execute('DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)', (document_id,))
    await db.execute('DELETE FROM chunks WHERE document_id=?', (document_id,))
    await db.execute("UPDATE documents SET status='deleted',updated_at=? WHERE id=?", (now(), document_id))
    await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (row["knowledge_id"], now(), row["knowledge_id"]))
    await db.commit()
    # 原文可能被好友共享过去的库引用：一条引用都没有了才删磁盘文件
    await unlink_if_unreferenced(db, storage_path)
    await db.close()
    return {"ok": True}


@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: str, user_id: str = Depends(current_user)) -> FileResponse:
    db = await connect(); row = await fetchone(db, "SELECT storage_path,filename FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id)); await db.close()
    if not row: raise HTTPException(404, "文档不存在")
    return await stored_file_response(str(row['storage_path'] or ''), filename=str(row['filename']), media_type=mimetypes.guess_type(str(row['filename']))[0] or "application/octet-stream")


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, user_id: str = Depends(current_user)) -> FileResponse:
    """下载 / 转发工具产物：产物按用户收窄，别人的 id 拿不到文件。"""
    db = await connect(); row = await artifact_service.owned(db, artifact_id, user_id); await db.close()
    if not row:
        raise HTTPException(404, "文件不存在")
    return await stored_file_response(str(artifact_service.stored_ref(row)), filename=str(row["filename"]), media_type=str(row["mime"] or "application/octet-stream"))


@app.get("/api/artifacts/{artifact_id}/preview")
async def preview_artifact(artifact_id: str, user_id: str = Depends(current_user)) -> dict:
    """站内预览：文本类产物直接给正文（Markdown / 纯文本 / 代码）；
    图片与文档类交前端走微信原生预览（图片预览 / 内置文档渲染器）。"""
    db = await connect(); row = await artifact_service.owned(db, artifact_id, user_id); await db.close()
    if not row:
        raise HTTPException(404, "文件不存在")
    view = artifact_service.view(row)
    storage_ref = artifact_service.stored_ref(row)
    if not await storage.exists(storage_ref):
        raise HTTPException(404, "文件已不在服务器上")
    if view["kind"] == "text":
        text, complete = artifact_service.preview_text(await storage.read_bytes(storage_ref))
        view["text"] = text
        view["truncated"] = not complete
    return view


@app.get("/api/documents/{document_id}")
async def document_detail(document_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); row = await fetchone(db, "SELECT id,filename,file_type,file_size,page_count,status,progress,error_message,extracted_text,organized_title,summary,tags_json,key_points_json,organize_status,organize_method,organize_error,organized_at,created_at,updated_at,storage_path FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not row: await db.close(); raise HTTPException(404, "文档不存在")
    # 打开一份资料就算一次阅读：最近知识列表按这个时间从上到下排
    await db.execute("UPDATE documents SET last_viewed_at=? WHERE id=?", (now(), document_id)); await db.commit(); await db.close()
    view = document_view(row)
    if row["file_type"] == ".html":
        try:
            view["content_html"] = (await storage.read_bytes(str(row["storage_path"] or ''))).decode("utf-8", errors="ignore")
        except storage.StorageError:
            view["content_html"] = ""
    return view


@app.patch("/api/documents/{document_id}/tags")
async def update_document_tags(document_id: str, payload: DocumentTagUpdate, user_id: str = Depends(current_user)) -> dict:
    tags = []
    seen = set()
    for value in payload.tags:
        tag = str(value).strip()[:24]
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
        if len(tags) >= 12:
            break
    db = await connect()
    row = await fetchone(db, "SELECT id,knowledge_id FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not row:
        await db.close()
        raise HTTPException(404, "文档不存在")
    await writable_knowledge_or_close(db, str(row["knowledge_id"]), user_id)
    await db.execute("UPDATE documents SET tags_json=?,updated_at=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), now(), document_id))
    await db.commit()
    updated = await fetchone(db, "SELECT id,filename,file_type,file_size,page_count,status,progress,error_message,organized_title,summary,tags_json,key_points_json,organize_status,organize_method,organized_at,created_at,updated_at FROM documents WHERE id=?", (document_id,))
    await db.close()
    return document_view(updated)


@app.get("/api/models")
async def list_models() -> list[dict]:
    return settings.chat_model_list


@app.get("/api/search")
async def search(q: str = Query(min_length=1), knowledge_id: str | None = None, user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect()
    rows = await retrieve_chunks(db, q, user_id, knowledge_id, None, limit=30)
    await db.close()
    return [{"id": r["id"], "document_id": r["document_id"], "content": r["content"], "filename": r["filename"], "page_number": r["page_number"], "knowledge_name": r["knowledge_name"], "score": r["score"]} for r in rows]


@app.get("/api/recent")
async def recent_overview(limit: int = Query(default=20, ge=1, le=100), user_id: str = Depends(current_user)) -> dict:
    """「最近」页数据源：最近更新的资料库 + 最近动过的文档/文件夹。"""
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    await db.commit()
    # 最近知识库：按「最近使用」排（打开过、提问过都算）；从没用过的退回资料更新时间，
    # 前端按这个顺序从左到右横向排列。
    kbs = await fetchall(
        db,
        "SELECT id,name,description,icon,document_count,updated_at,created_at,last_used_at FROM knowledge_bases"
        " WHERE user_id=? AND status='active'"
        " ORDER BY CASE WHEN COALESCE(last_used_at,'')='' THEN updated_at ELSE last_used_at END DESC LIMIT 8",
        (user_id,),
    )
    # 资料按「看过的时间」排：打开过就顶到最前，没打开过的退回资料更新时间
    docs = await fetchall(db, "SELECT id,knowledge_id,filename,file_type,file_size,status,created_at,updated_at,last_viewed_at FROM documents WHERE user_id=? AND status!='deleted' ORDER BY CASE WHEN COALESCE(last_viewed_at,'')='' THEN updated_at ELSE last_viewed_at END DESC LIMIT ?", (user_id, limit))
    folders = await fetchall(db, "SELECT id,knowledge_id,name,created_at,updated_at FROM folders WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    await db.close()
    items = [
        {
            'id': row['id'], 'kind': 'document', 'name': row['filename'], 'file_type': row['file_type'] or '',
            'file_size': int(row['file_size'] or 0), 'status': row['status'] or 'uploaded',
            'knowledge_id': row['knowledge_id'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'last_viewed_at': row['last_viewed_at'] or '',
        }
        for row in docs
    ] + [
        {
            'id': row['id'], 'kind': 'folder', 'name': row['name'], 'file_type': '',
            'file_size': 0, 'status': 'folder',
            'knowledge_id': row['knowledge_id'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'last_viewed_at': '',
        }
        for row in folders
    ]
    # 文档与文件夹混合排序：文档看「打开时间」，文件夹看「内容变动时间」
    items.sort(key=lambda item: item['last_viewed_at'] or item['updated_at'] or '', reverse=True)
    return {'knowledge': [row_dict(row) for row in kbs], 'items': items[:limit]}


async def make_sources(db, knowledge_id: str, query: str, user_id: str, folder_id: str = '') -> list[dict]:
    # 范围语义（与产品入口一致）：
    #   从知识库首页进入的对话（folder_id 为空）= 整个知识库，根目录和各文件夹里的资料都算数；
    #   从某个文件夹进入的对话 = 只检索该文件夹，不外溢到根目录或其它文件夹。
    rows = await retrieve_chunks(db, query, user_id, knowledge_id, folder_id or None, limit=5)
    return [{"id": r["id"], "document_id": r["document_id"], "filename": r["filename"], "page_number": r["page_number"], "quote": r["content"][:180], "score": r["score"]} for r in rows]


async def remaining_storage(db, user_id: str, limits: dict) -> int:
    """该用户当前还剩多少可用空间（每次登记产物都重算，一轮内多个产物不会超配额）。"""
    return max(0, int(limits['storage_bytes']) - await storage_used_bytes(db, user_id))


async def storage_used_bytes(db, user_id: str) -> int:
    """已用空间只算自己的资料：好友共享过来的库与来源共用同一份原文，不重复计费。"""
    row = await fetchone(db, "SELECT COALESCE(SUM(d.file_size),0) AS bytes FROM documents d JOIN knowledge_bases k ON k.id=d.knowledge_id WHERE d.user_id=? AND d.status!='deleted' AND COALESCE(k.mirror_of,'')=''", (user_id,))
    return int((row['bytes'] if row else 0) or 0)


# 聊天任务与请求连接解绑：请求断开只取消订阅，后台任务继续执行并持续写 chat_runs。
_CHAT_RUN_TASKS: dict[str, asyncio.Task] = {}
_CHAT_RUN_CANCELS: set[str] = set()
_CHAT_RUN_TERMINAL = {'completed', 'error', 'cancelled', 'interrupted'}


def _decode_json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def chat_run_view(row: Any) -> dict:
    """把数据库行转成前端可续订的任务快照。"""
    return {
        # 下方字段保持稳定的 camel/snake 命名，前端直接用同一份结构刷新整条回答。
        'id': row['id'],
        'conversation_id': row['conversation_id'],
        'user_message_id': row['user_message_id'] or '',
        'knowledge_id': row['knowledge_id'],
        'folder_id': row['folder_id'] or '',
        'status': row['status'],
        'model': row['model'] or '',
        'mode': row['mode'] or 'knowledge',
        'thinking': row['thinking'] or 'quick',
        'answer': row['answer'] or '',
        'sources': _decode_json_list(row['sources_json']),
        'trace': _decode_json_list(row['trace_json']),
        'reason': row['reason'] or '',
        'artifacts': _decode_json_list(row['artifacts_json']),
        'error': row['error'] or '',
        'revision': int(row['revision'] or 0),
        'duration_ms': int(row['duration_ms'] or 0),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'finished_at': row['finished_at'] or '',
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat/runs")
async def create_chat_run(payload: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(current_user)) -> dict:
    rate_limit(f"chat:{user_id}", 15, 60, "提问太频繁啦，喝口水休息一下再试")
    if not harness_configured():
        raise HTTPException(503, "Harness 未启用，聊天接口不会回落到直连模型")
    if payload.plan:
        raise HTTPException(400, "当前公开 Harness SDK 不支持 /plan 传输接口")
    db = await connect()
    account = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    state = account_state(account, await credits_this_month(db, user_id))
    limits = limits_for_user(account)
    if not state['entitlements']['can_ask']:
        limit = state['quota']['credits_limit']
        await db.close()
        raise HTTPException(429, f"本月 {limit} 积分已用完，开通会员可继续提问")
    if payload.knowledge_id:
        kb = await fetchone(db, "SELECT id,name FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (payload.knowledge_id, user_id))
    else:
        kb = await fetchone(
            db,
            "SELECT id,name FROM knowledge_bases WHERE user_id=? AND status='active' ORDER BY created_at ASC LIMIT 1",
            (user_id,),
        )
    if not kb:
        await db.close()
        raise HTTPException(404, "知识库不存在")
    knowledge_id = kb["id"]
    # 文件夹隔离：校验文件夹属于当前知识库，后续检索/整理知识/会话全部限定在该文件夹内
    folder_id = payload.folder_id or ''
    folder_name = ''
    if folder_id:
        folder = await fetchone(db, "SELECT id,name FROM folders WHERE id=? AND knowledge_id=? AND user_id=?", (folder_id, knowledge_id, user_id))
        if not folder:
            await db.close()
            raise HTTPException(404, "文件夹不存在")
        folder_name = folder["name"]
    # 在这个知识库里提问也算使用
    await touch_knowledge_used(db, knowledge_id)
    # Only application-owned retrieval is assembled here. The official
    # Harness agent owns history, compaction, planning and tool iteration.
    sources: list[dict] = []
    context = ''
    if payload.mode == 'knowledge':
        sources = await make_sources(db, knowledge_id, payload.content, user_id, folder_id)
        organized_sql = (
            "SELECT filename,organized_title,summary,tags_json,key_points_json FROM documents WHERE knowledge_id=? AND status='completed' AND organize_status='completed'"
            + (" AND folder_id=?" if folder_id else "")
            + " ORDER BY organized_at DESC LIMIT 20"
        )
        organized = await fetchall(
            db, organized_sql, (knowledge_id, folder_id) if folder_id else (knowledge_id,)
        )
        wiki_context = "\n\n".join(
            f"《{row['organized_title'] or row['filename']}》\n摘要：{row['summary']}\n要点：{'；'.join(json_list(row['key_points_json']))}"
            for row in organized
        )
        source_context = "\n\n".join(
            f"[{index + 1}] {source['filename']} 第{source['page_number']}页\n{source['quote']}"
            for index, source in enumerate(sources)
        )
        scope_note = (
            f"当前问答范围限定在文件夹「{folder_name}」内，只能依据该文件夹中的文件回答。"
            if folder_id else "当前问答范围是整个知识库。"
        )
        context = (
            f"资料库：{kb['name']}\n{scope_note}\n\n"
            f"已整理知识：\n{wiki_context or '暂无整理条目'}\n\n"
            f"原文片段：\n{source_context or '暂无匹配原文'}"
        )
    conversation_id = payload.conversation_id or uuid.uuid4().hex
    if payload.conversation_id:
        # 归属校验即隔离边界：会话必须同时属于当前用户、当前知识库、当前文件夹（根目录 ''），
        owner = await fetchone(db, 'SELECT id,folder_id,harness_session_id FROM conversations WHERE id=? AND user_id=? AND knowledge_id=?', (conversation_id, user_id, knowledge_id))
        if not owner or (owner['folder_id'] or '') != folder_id:
            await db.close(); raise HTTPException(404, '对话不存在')
        active = await fetchone(db, "SELECT id FROM chat_runs WHERE conversation_id=? AND user_id=? AND status='running' LIMIT 1", (conversation_id, user_id))
        if active:
            await db.close()
            raise HTTPException(409, '当前对话还有任务正在执行，请先等待完成或停止任务')
    if not payload.conversation_id:
        harness_session_id = uuid.uuid4().hex
        await db.execute("INSERT INTO conversations(id,user_id,knowledge_id,folder_id,title,harness_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (conversation_id, user_id, knowledge_id, folder_id, payload.content[:32], harness_session_id, now(), now()))
    else:
        harness_session_id = str(owner['harness_session_id'] or conversation_id)

    # 应用侧保留最近消息，仅在官方 runtime 回收且 session id 冲突时作为数据补回。
    history_rows = await fetchall(
        db,
        "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 24",
        (conversation_id,),
    )
    history = [{'role': row['role'], 'content': row['content']} for row in reversed(history_rows)]
    user_message_id = uuid.uuid4().hex
    await db.execute("INSERT INTO messages(id,conversation_id,role,content,sources_json,created_at) VALUES(?,?,?,?,?,?)", (user_message_id, conversation_id, "user", payload.content, "[]", now())); await db.commit()
    # 本轮技能：只有用户明确选择的技能才会注入——内置技能走技能包，我的技能走技能指令。
    # 别人的私有技能在这里解析为空，保证「我的技能」严格隔离。
    # 本轮技能（可多选）：payload.skills 优先，为空时兼容旧的单选 skill 字段。
    # 只有用户明确选择的技能才会注入；别人的私有技能在这里解析为空，保证「我的技能」严格隔离。
    saved_skill_ids = stored_preferences(account['preferences'] if account else '{}').get('skills')
    if not isinstance(saved_skill_ids, list):
        saved_skill_ids = []
    requested_skills = select_turn_skill_ids(payload.skills, payload.skill, saved_skill_ids)
    turn_skills = await resolve_turn_skills(db, user_id, requested_skills)
    for item in turn_skills:
        await db.execute('UPDATE skills SET use_count=COALESCE(use_count,0)+1 WHERE id=?', (item['id'],))
    if turn_skills:
        await db.commit()
    # The official SDK owns model routing and tool iteration. The frontend's
    # model field is intentionally ignored on this path.
    turn_model = settings.harness_model
    turn_plan = bool(payload.plan)
    run_id = uuid.uuid4().hex
    run_created_at = now()
    await db.execute(
        "INSERT INTO chat_runs(id,user_id,conversation_id,user_message_id,knowledge_id,folder_id,status,model,mode,thinking,answer,sources_json,trace_json,reason,artifacts_json,error,revision,cancel_requested,duration_ms,created_at,updated_at,finished_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, user_id, conversation_id, user_message_id, knowledge_id, folder_id, 'running',
            turn_model, payload.mode, payload.thinking, '', json.dumps(sources, ensure_ascii=False), '[]', '', '[]', '', 0, 0, 0,
            run_created_at, run_created_at, ''
        ),
    )
    await db.commit()
    run_row = await fetchone(db, "SELECT * FROM chat_runs WHERE id=?", (run_id,))
    run_snapshot = chat_run_view(run_row)
    # 后台任务自己获取数据库连接；请求连接在响应返回时由中间件回收，不能跟任务生命周期绑定。
    await db.close()

    async def events():
        nonlocal db, harness_session_id
        db = await connect()
        answer = ""
        # 过程区随正文一起落库：思考文字单独累积（增量太多，不入过程节点列表），
        # 步骤 / 工具 / 技能 / 子智能体 / 提示 按原顺序留存，重进对话时才能复原。
        started_at = time.monotonic()
        trace_items: list[dict] = []
        reason_parts: list[str] = []
        # 工具产物：agent 本轮生成的文件（报告 / 表格 / 演示稿 / 图 …）随消息落库。
        # 知识库问答里的产物同时登记进知识库，纯对话（执行规划）只在对话里给出文件。
        produced_artifacts: list[dict] = []
        # 本轮真实用量（harness 每个 assistant/message 事件的 usage 已在服务层累加），
        # 拿到后按「成本 × 1.5」的积分口径落一条流水。
        turn_usage: dict | None = None
        save_artifacts_to_kb = bool(state['entitlements']['can_upload']) and payload.mode != 'web'
        revision = 0
        last_flush = 0.0
        last_cancel_check = 0.0

        async def persist(force: bool = False, status: str = 'running', error: str = '') -> None:
            nonlocal revision, last_flush
            current = time.monotonic()
            if not force and current - last_flush < 0.18:
                return
            last_flush = current
            revision += 1
            finished = now() if status in _CHAT_RUN_TERMINAL else ''
            await db.execute(
                "UPDATE chat_runs SET answer=?,trace_json=?,reason=?,artifacts_json=?,status=?,error=?,revision=?,duration_ms=?,updated_at=?,finished_at=? WHERE id=? AND status!='cancelled'",
                (
                    answer, json.dumps(trace_items, ensure_ascii=False), ''.join(reason_parts).strip(),
                    json.dumps(produced_artifacts, ensure_ascii=False), status, error, revision,
                    int((time.monotonic() - started_at) * 1000), now(), finished, run_id,
                ),
            )
            await db.commit()

        async def cancel_requested() -> bool:
            nonlocal last_cancel_check
            if run_id in _CHAT_RUN_CANCELS:
                return True
            current = time.monotonic()
            if current - last_cancel_check < 0.8:
                return False
            last_cancel_check = current
            row = await fetchone(db, "SELECT cancel_requested,status FROM chat_runs WHERE id=?", (run_id,))
            return bool(row and (row['cancel_requested'] or row['status'] == 'cancelled'))

        if await cancel_requested():
            raise asyncio.CancelledError
        yield f"data: {json.dumps({'type':'meta','conversation_id':conversation_id,'sources':sources}, ensure_ascii=False)}\n\n"
        try:
            async for event in stream_answer(
                question=payload.content,
                context=context,
                model=turn_model,
                session_id=harness_session_id or conversation_id,
                thinking=payload.thinking,
                skills=turn_skills,
                mode=payload.mode,
                user_id=user_id,
                history=history,
                plan=turn_plan,
            ):
                kind = event.get('kind')
                if await cancel_requested():
                    raise asyncio.CancelledError
                if kind == 'session':
                    # runtime 重启时官方 SDK 会换一个新 session id；应用侧会话 id 不变。
                    new_session_id = str(event.get('session_id') or '').strip()
                    if new_session_id and new_session_id != harness_session_id:
                        harness_session_id = new_session_id
                        await db.execute("UPDATE conversations SET harness_session_id=?,updated_at=? WHERE id=? AND user_id=?", (new_session_id, now(), conversation_id, user_id))
                        await db.commit()
                    continue
                if kind == 'progress':
                    yield f"data: {json.dumps({'type':'progress','label':event['text']}, ensure_ascii=False)}\n\n"
                    continue
                if kind == 'trace':
                    # 过程节点：思考 / 步骤 / 工具 / 技能 / 子智能体 / 提示
                    item = event.get('item') or {}
                    if item:
                        if item.get('kind') == 'reason':
                            reason_parts.append(str(item.get('text') or ''))
                        else:
                            trace_items.append(item)
                        await persist(force=True)
                        yield f"data: {json.dumps({'type':'trace', **item}, ensure_ascii=False)}\n\n"
                    continue
                if kind == 'artifact':
                    # 工具产物回流：把运行时工作区里新生成的文件收成可预览/下载的产物。
                    # 收不动（空文件 / 过大 / 已消失 / 超出一轮上限）时静默跳过，不影响回答正文。
                    discovery = event.get('artifact') or {}
                    if len(produced_artifacts) >= artifact_service.MAX_ARTIFACTS_PER_TURN:
                        continue
                    record = await artifact_service.register(
                        user_id=user_id,
                        source=Path(str(discovery.get('path') or '')),
                        conversation_id=conversation_id,
                        knowledge_id=knowledge_id,
                        folder_id=folder_id,
                        save_to_knowledge=save_artifacts_to_kb,
                        storage_room=await remaining_storage(db, user_id, limits),
                        description=str(discovery.get('description') or ''),
                    )
                    if not record:
                        continue
                    produced_artifacts.append(record)
                    # 生成物入库后照旧做一次整理（与上传同一条链路）：摘要 / 标签 / 要点
                    if record.get('document_id') and background_tasks is not None:
                        background_tasks.add_task(organize_document, record['document_id'])
                    await persist(force=True)
                    yield f"data: {json.dumps({'type':'artifact','artifact':record}, ensure_ascii=False)}\n\n"
                    continue
                if kind == 'usage':
                    # 用量事件不进正文、也不落过程节点，只用于计费
                    turn_usage = event.get('usage') or turn_usage
                    continue
                if kind != 'text':
                    continue
                piece = event['text']
                answer += piece
                await persist()
                yield f"data: {json.dumps({'type':'delta','content':piece}, ensure_ascii=False)}\n\n"
            if await cancel_requested():
                raise asyncio.CancelledError
            if not answer.strip():
                # 网关断流等情况导致零产出：不发 done（否则前端落一个空气泡），
                # 发 error 让前端提示重试
                await persist(force=True, status='error', error='网络波动，本次回答未完成，请重新发送。')
                yield f"data: {json.dumps({'type':'error','message':'网络波动，本次回答未完成，请重新发送。'}, ensure_ascii=False)}\n\n"
                return
            assistant_message_id = uuid.uuid4().hex
            await db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,sources_json,trace_json,reason,duration_ms,artifacts_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    assistant_message_id, conversation_id, "assistant", answer,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(trace_items, ensure_ascii=False),
                    "".join(reason_parts).strip(),
                    int((time.monotonic() - started_at) * 1000),
                    json.dumps(produced_artifacts, ensure_ascii=False),
                    now(),
                ),
            ); await db.commit()
            # Harness usage is authoritative. The estimate is only a safety net
            # for older runtimes that omit usage events; it never creates another
            # model call.
            turn_quote = None
            try:
                billable = turn_usage if total_tokens(turn_usage) > 0 else estimate_usage(
                    len(payload.content) + len(context), len(answer)
                )
                turn_quote = await charge_turn(db, user_id, conversation_id, assistant_message_id, turn_model, billable)
            except Exception as exc:
                print(f'[credits] 结算失败：{exc}', flush=True)
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conversation_id)); await db.commit()
            await persist(force=True, status='completed')
            done_payload = {'type': 'done'}
            if turn_quote:
                used_row = await fetchone(db, "SELECT COALESCE(SUM(credits),0) AS used FROM usage_logs WHERE user_id=? AND created_at>=?", (user_id, period_start_iso()))
                done_payload['credits'] = turn_quote['credits']
                done_payload['credits_used'] = int((used_row['used'] if used_row else 0) or 0)
                done_payload['credits_limit'] = state['quota']['credits_limit']
            yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
        except Exception as exc:
            await persist(force=True, status='error', error=str(exc))
            yield f"data: {json.dumps({'type':'error','message':str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            await db.close()
    async def consume_run() -> None:
        try:
            async for _chunk in events():
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                error_db = await connect()
                await error_db.execute("UPDATE chat_runs SET status='error',error=?,revision=revision+1,updated_at=?,finished_at=? WHERE id=? AND status='running'", (str(exc), now(), now(), run_id))
                await error_db.commit()
                await error_db.close()
            except Exception:
                pass
            print(f'[chat-run] {run_id} failed: {exc}', flush=True)

    run_task = asyncio.create_task(consume_run(), name=f'chat-run:{run_id}')
    _CHAT_RUN_TASKS[run_id] = run_task

    def task_done(_task: asyncio.Task) -> None:
        _CHAT_RUN_TASKS.pop(run_id, None)
        _CHAT_RUN_CANCELS.discard(run_id)

    run_task.add_done_callback(task_done)
    return run_snapshot


@app.get("/api/chat/runs/{run_id}")
async def get_chat_run(run_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    row = await fetchone(db, "SELECT * FROM chat_runs WHERE id=? AND user_id=?", (run_id, user_id))
    await db.close()
    if not row:
        raise HTTPException(404, '任务不存在')
    return chat_run_view(row)


@app.get("/api/chat/runs/{run_id}/stream")
async def chat_run_stream(run_id: str, after: int = Query(default=0, ge=0), user_id: str = Depends(current_user)) -> StreamingResponse:
    db = await connect()
    row = await fetchone(db, "SELECT * FROM chat_runs WHERE id=? AND user_id=?", (run_id, user_id))
    if not row:
        await db.close()
        raise HTTPException(404, '任务不存在')

    async def run_events():
        last_revision = -1
        last_answer = ''
        sent_meta = False
        try:
            while True:
                current = await fetchone(db, "SELECT * FROM chat_runs WHERE id=? AND user_id=?", (run_id, user_id))
                if not current:
                    yield _sse({'type': 'error', 'message': '任务不存在'})
                    return
                snapshot = chat_run_view(current)
                if not sent_meta:
                    yield _sse({'type': 'meta', 'conversation_id': snapshot['conversation_id'], 'sources': snapshot['sources'], 'run_id': run_id})
                    sent_meta = True
                revision = int(snapshot['revision'] or 0)
                if revision != last_revision:
                    yield _sse({'type': 'snapshot', 'run': snapshot})
                    answer = str(snapshot.get('answer') or '')
                    if answer != last_answer:
                        delta = answer[len(last_answer):] if answer.startswith(last_answer) else answer
                        if delta:
                            yield _sse({'type': 'delta', 'content': delta})
                        last_answer = answer
                    last_revision = revision
                status = snapshot['status']
                if status in _CHAT_RUN_TERMINAL:
                    if status == 'completed':
                        yield _sse({'type': 'done'})
                    elif status == 'cancelled':
                        yield _sse({'type': 'cancelled'})
                    else:
                        yield _sse({'type': 'error', 'message': snapshot.get('error') or '任务未完成'})
                    return
                await asyncio.sleep(0.2)
        finally:
            await db.close()

    return StreamingResponse(run_events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


@app.post("/api/chat/runs/{run_id}/stop")
async def stop_chat_run(run_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    row = await fetchone(db, "SELECT * FROM chat_runs WHERE id=? AND user_id=?", (run_id, user_id))
    if not row:
        await db.close()
        raise HTTPException(404, '任务不存在')
    if row['status'] in _CHAT_RUN_TERMINAL:
        view = chat_run_view(row)
        await db.close()
        return view
    _CHAT_RUN_CANCELS.add(run_id)
    stamp = now()
    await db.execute(
        "UPDATE chat_runs SET status='cancelled',cancel_requested=1,revision=revision+1,updated_at=?,finished_at=? WHERE id=? AND user_id=? AND status='running'",
        (stamp, stamp, run_id, user_id),
    )
    await db.commit()
    current = await fetchone(db, "SELECT * FROM chat_runs WHERE id=? AND user_id=?", (run_id, user_id))
    view = chat_run_view(current)
    await db.close()
    task = _CHAT_RUN_TASKS.get(run_id)
    if task and not task.done():
        task.cancel()
    # The public Harness SDK has no per-turn cancel RPC. Closing this user's
    # runtime is the supported interruption boundary.
    await asyncio.to_thread(cancel_user_runtime, user_id)
    return view


@app.post("/api/harness/preheat")
async def preheat_harness(user_id: str = Depends(current_user)) -> dict:
    """进会话页时提前把运行时拉起来，用官方 SDK 的 start/initialize 路径。

    实测冷启动：全新 DSH_HOME 约 3.8s（官方运行时要在 home 下物化整套 profile 代理包），
    同一个 home 再次启动约 0.7s。这里放到后台付掉，请求立即返回；失败不影响后续问答。
    """
    if not harness_configured():
        return {"ok": False, "reason": "disabled"}
    asyncio.create_task(asyncio.to_thread(harness_warm, user_id))
    return {"ok": True}


@app.get("/api/conversations/{conversation_id}/active-run")
async def active_chat_run(conversation_id: str, user_id: str = Depends(current_user)) -> dict | None:
    db = await connect()
    row = await fetchone(
        db,
        "SELECT * FROM chat_runs WHERE conversation_id=? AND user_id=? AND status='running' ORDER BY created_at DESC LIMIT 1",
        (conversation_id, user_id),
    )
    await db.close()
    return chat_run_view(row) if row else None


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(current_user)) -> StreamingResponse:
    snapshot = await create_chat_run(payload, background_tasks, user_id)
    return await chat_run_stream(snapshot['id'], 0, user_id)


@app.post("/api/chat/plan-review")
async def plan_review(payload: PlanReviewRequest, user_id: str = Depends(current_user)) -> dict:
    """The public Python SDK exposes no /plan transport or review RPC.

    Returning a protocol error is intentional: silently pretending a plan was
    accepted would fork behavior from official Harness.
    """
    raise HTTPException(410, "当前公开 Harness SDK 不支持 /plan 评审接口")


@app.get("/api/conversations")
async def conversations(
    knowledge_id: str = Query(default="", max_length=64),
    folder_id: str | None = Query(default=None, max_length=64),
    q: str = Query(default="", max_length=80),
    user_id: str = Depends(current_user),
) -> list[dict]:
    """历史对话列表：第一层永远是 user_id，再按知识库 / 文件夹收窄。

    folder_id 区分「未传」与「根目录」：None=不限文件夹；空串=只看知识库根目录会话。
    """
    clauses = ["user_id=?"]
    params: list[Any] = [user_id]
    if knowledge_id:
        clauses.append("knowledge_id=?")
        params.append(knowledge_id)
    if folder_id is not None:
        clauses.append("COALESCE(folder_id,'')=?")
        params.append(folder_id)
    if q.strip():
        clauses.append("title LIKE ?")
        params.append(f"%{q.strip()}%")
    # 置顶的会话排在最前，其余仍按最近更新排序
    db = await connect(); rows = await fetchall(db, f"SELECT * FROM conversations WHERE {' AND '.join(clauses)} ORDER BY pinned DESC, updated_at DESC LIMIT 100", tuple(params)); await db.close()
    return [row_dict(r) for r in rows]


@app.post("/api/conversations/{conversation_id}/pin")
async def pin_conversation(conversation_id: str, payload: ConversationPinUpdate, user_id: str = Depends(current_user)) -> dict:
    """置顶 / 取消置顶历史对话：范围严格限定在当前用户名下。"""
    db = await connect()
    owner = await fetchone(db, "SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
    if not owner:
        await db.close(); raise HTTPException(404, '对话不存在')
    stamp = now()
    await db.execute(
        "UPDATE conversations SET pinned=?, pinned_at=? WHERE id=? AND user_id=?",
        (1 if payload.pinned else 0, stamp if payload.pinned else '', conversation_id, user_id),
    )
    await db.commit()
    row = await fetchone(db, "SELECT id,pinned FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
    await db.close()
    return {'id': conversation_id, 'pinned': bool(row['pinned']) if row else payload.pinned}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, user_id: str = Depends(current_user)) -> dict:
    """删除历史对话：会话与其中的消息一并删除，删除范围严格限定在当前用户名下。"""
    db = await connect()
    owner = await fetchone(db, "SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
    if not owner:
        await db.close(); raise HTTPException(404, '对话不存在')
    await db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    await db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
    await db.commit(); await db.close()
    return {'ok': True}


# 历史回放只要时间线骨架：工具入参/输出单条可达几 KB～几十 KB，是整段历史里最大的一块，
# 去掉它们才能把返回包压进容器通道 1MiB 的上限；当轮的实时过程仍然带完整明细。
HISTORY_TRACE_DROP_FIELDS = ('tool_input', 'tool_output', 'tool_meta', 'tool_result_meta')
HISTORY_MESSAGE_LIMIT = 100


def history_trace(raw: Any) -> list[dict]:
    try:
        parsed = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {key: value for key, value in item.items() if key not in HISTORY_TRACE_DROP_FIELDS}
        for item in parsed
        if isinstance(item, dict)
    ]


@app.get("/api/conversations/{conversation_id}")
async def conversation_detail(conversation_id: str, limit: int = Query(default=HISTORY_MESSAGE_LIMIT, ge=1, le=300), user_id: str = Depends(current_user)) -> list[dict]:
    """历史对话回放：默认回最近 100 条消息，够翻回最近几轮，也不至于把返回包撑爆。"""
    db = await connect()
    rows = await fetchall(
        db,
        "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.conversation_id=? AND c.user_id=? ORDER BY m.created_at DESC LIMIT ?",
        (conversation_id, user_id, limit),
    )
    await db.close()
    # 一并回放过程区（思考 / 步骤 / 工具 / 技能 / 子智能体）与耗时：不返回这些字段，
    # 前端重新进入对话时就只剩正文了。
    messages = []
    for row in reversed(rows):
        view = row_dict(row)
        messages.append({
            **view,
            "sources": decode_sources(row["sources_json"]),
            "trace": history_trace(view.get("trace_json")),
            "reason": view.get("reason") or "",
            # 工具产物（生成的文件）与过程区一样回放：重进对话时文件卡还在，可以继续预览/下载
            "artifacts": [item for item in artifact_service.decode(view.get("artifacts_json")) if isinstance(item, dict)],
            "duration_ms": int(view.get("duration_ms") or 0),
        })
    return messages



@app.post('/api/shares')
async def create_share(payload: ShareCreate, user_id: str = Depends(current_user)) -> dict:
    """把用户勾选的回答与文件落成分享卡片，供其转发给微信好友。

    只保存问答正文、出处文件名与产物文件名，不保存分享者的昵称 / 头像 / openid：
    好友点开卡片看到的是内容本身，而不是分享者的账号信息。
    文件只存产物 id，公开页凭分享 id 取文件，再按卡片作者校验归属。
    """
    rate_limit(f"share:{user_id}", 30, 60, '分享过于频繁，请稍后再试')
    sources = [{'filename': item.filename, 'page_number': item.page_number, 'url': item.url} for item in payload.sources if item.filename or item.url]
    share_id = secrets.token_urlsafe(9)
    db = await connect()
    files: list[dict] = []
    for item in payload.files:
        row = await fetchone(db, 'SELECT id,filename,file_type,file_size FROM artifacts WHERE id=? AND user_id=?', (item.artifact_id, user_id))
        if not row:
            continue  # 不是自己的产物就不放进分享内容，宁少勿错
        name = str(row['filename'] or item.name or '文件')[:200]
        # 后缀一并落库：公开页要按类型决定走图片预览还是微信文档渲染器，不回查产物表
        suffix = str(row['file_type'] or '').lstrip('.').lower() or Path(name).suffix.lstrip('.').lower()
        files.append({'artifact_id': row['id'], 'name': name, 'size': int(row['file_size'] or 0), 'suffix': suffix})
    await db.execute(
        'INSERT INTO share_cards(id,user_id,title,knowledge_name,question,answer,sources_json,files_json,views,created_at) VALUES(?,?,?,?,?,?,?,?,0,?)',
        (share_id, user_id, payload.title, payload.knowledge_name, payload.question, payload.answer,
         json.dumps(sources, ensure_ascii=False), json.dumps(files, ensure_ascii=False), now()),
    )
    # 分享卡片给好友看一段时间就够：顺手清掉半年前的记录，避免只增不减
    await db.execute('DELETE FROM share_cards WHERE created_at < ?', ((datetime.now(timezone.utc) - timedelta(days=180)).isoformat(),))
    await db.commit(); await db.close()
    return {'id': share_id}


def valid_share_id(share_id: str) -> bool:
    """分享 id 是 url-safe 随机串：长度与字符集都要卡死，避免被拿来探测。"""
    if not 6 <= len(share_id) <= 32:
        return False
    return all(char.isascii() and (char.isalnum() or char in '-_') for char in share_id)


def valid_share_token(token: str) -> bool:
    """知识库邀请 token 同样是 url-safe 随机串，长度按 secrets.token_urlsafe(24) 卡死。"""
    if not 16 <= len(token) <= 96:
        return False
    return all(char.isascii() and (char.isalnum() or char in '-_') for char in token)


async def knowledge_share_state(db: Any, share: Any, user_id: str) -> str:
    """一条邀请链接对当前访客的状态：open=还没人领 / mine=我领的 / taken=别人领的。"""
    if int(share['revoked'] or 0):
        return 'revoked'
    expires_at = str(share['expires_at'] or '')
    if expires_at and expires_at <= now():
        return 'expired'
    accepted_by = str(share['accepted_by'] or '')
    if not accepted_by:
        return 'open'
    return 'mine' if accepted_by == user_id else 'taken'


@app.get('/api/shares/{share_id}')
async def read_share(share_id: str, request: Request) -> dict:
    """公开只读：好友点开分享卡片时读取内容，不需要登录。

    返回体里没有任何用户身份字段，只回内容与出处。
    """
    if not valid_share_id(share_id):
        raise HTTPException(404, '分享内容不存在或已失效')
    rate_limit(f"share-read:{request.client.host if request.client else 'unknown'}", 120, 60, '访问过于频繁，请稍后再试')
    db = await connect()
    row = await fetchone(db, 'SELECT id,title,knowledge_name,question,answer,sources_json,files_json,views,created_at FROM share_cards WHERE id=?', (share_id,))
    if not row:
        await db.close(); raise HTTPException(404, '分享内容不存在或已失效')
    await db.execute('UPDATE share_cards SET views=COALESCE(views,0)+1 WHERE id=?', (share_id,))
    await db.commit(); await db.close()
    files = decode_sources(row['files_json'])
    # 文件只对外暴露序号与文件名：产物 id 是取件凭证，不写进公开返回体
    public_files = [
        {'index': index, 'name': str(item.get('name') or '文件'), 'size': int(item.get('size') or 0), 'suffix': str(item.get('suffix') or '')}
        for index, item in enumerate(files)
    ]
    return {'id': row['id'], 'title': row['title'] or '', 'question': row['question'] or '', 'answer': row['answer'], 'knowledge_name': row['knowledge_name'] or '', 'sources': decode_sources(row['sources_json']), 'files': public_files, 'views': int(row['views'] or 0) + 1, 'created_at': row['created_at']}


@app.get('/api/shares/{share_id}/files/{index}')
async def download_share_file(share_id: str, index: int, request: Request) -> FileResponse:
    """公开只读：好友从分享页取走作者勾选的文件。

    取件范围被分享卡片钉死：只能拿这张卡片列出的第 index 个文件，
    且必须仍然属于卡片作者；作者删掉产物后这里立即 404。
    """
    if not valid_share_id(share_id) or index < 0 or index > 32:
        raise HTTPException(404, '文件不存在或已失效')
    rate_limit(f"share-file:{request.client.host if request.client else 'unknown'}", 120, 60, '访问过于频繁，请稍后再试')
    db = await connect()
    card = await fetchone(db, 'SELECT user_id,files_json FROM share_cards WHERE id=?', (share_id,))
    if not card:
        await db.close(); raise HTTPException(404, '文件不存在或已失效')
    files = decode_sources(card['files_json'])
    if index >= len(files):
        await db.close(); raise HTTPException(404, '文件不存在或已失效')
    artifact_id = str(files[index].get('artifact_id') or '')
    row = await fetchone(db, 'SELECT id,filename,mime,storage_path FROM artifacts WHERE id=? AND user_id=?', (artifact_id, card['user_id']))
    await db.close()
    if not row:
        raise HTTPException(404, '文件不存在或已失效')
    return await stored_file_response(str(row['storage_path'] or ''), filename=str(row['filename']), media_type=str(row['mime'] or 'application/octet-stream'))


# 分享到知识库时不做 OCR 的图片类：图片正文靠模型看图 + 人工，不强占用请求时间
SHARE_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp'}


async def register_knowledge_document(db: Any, *, knowledge_id: str, user_id: str, storage_ref: str, name: str, size: int, folder_id: str = '') -> tuple[str, bool]:
    """把一份落在磁盘上的文件登记成知识库文档：分块 + 全文索引 + 计数。

    上传、对话内容存知识库、好友分享收件三条路径共用这一份实现，保证三种来源
    出来的文档在检索、预览、整理上的行为完全一致。返回 (document_id, 是否有正文)。
    """
    document_id = uuid.uuid4().hex
    timestamp = now()
    suffix = Path(name).suffix.lower()
    await db.execute(
        "INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,organize_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (document_id, knowledge_id, user_id, name, suffix, size, storage_ref, 'processing', 15, folder_id, 'pending', timestamp, timestamp),
    )
    text = ''
    pages = 1
    parsed_path = None
    try:
        if suffix not in SHARE_IMAGE_SUFFIXES:
            parsed_path = await storage.materialize(storage_ref, suffix=suffix)
            text, pages = extract_text(parsed_path, suffix)
    except Exception as exc:  # 解析失败不影响文件本身的预览与下载
        print(f'[share] 解析失败 {name}: {exc}', flush=True)
    finally:
        if parsed_path is not None:
            parsed_path.unlink(missing_ok=True)
    chunks = split_chunks(text) if text.strip() else []
    for index, chunk in enumerate(chunks):
        chunk_id = uuid.uuid4().hex
        await db.execute(
            'INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)',
            (chunk_id, document_id, knowledge_id, chunk, min(pages, index + 1), index, timestamp),
        )
        await db.execute(
            'INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)',
            (chunk, chunk_id, knowledge_id, name, min(pages, index + 1)),
        )
    await db.execute(
        "UPDATE documents SET status='completed',progress=100,page_count=?,extracted_text=?,organize_status=?,updated_at=? WHERE id=?",
        (max(1, pages), text, 'processing' if text.strip() else 'pending', now(), document_id),
    )
    await db.execute(
        "UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?",
        (knowledge_id, now(), knowledge_id),
    )
    if text.strip():
        await enforce_published_knowledge_policy(db, knowledge_id, name, text)
    return document_id, bool(text.strip())


async def share_to_knowledge_worker(job_id: str, *, knowledge_id: str, user_id: str, title: str, content: str, artifact_ids: list[str], folder_id: str) -> dict:
    """把选中的正文与文件落进知识库。

    每个文件都要复制 + 抽取正文（可能含 OCR），整体耗时不可控，所以放在任务里跑。
    """
    await jobs_service.set_progress(job_id, '正在写入知识库…')
    db = await connect()
    to_organize: list[str] = []
    try:
        kb = await fetchone(db, "SELECT id,name FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
        if not kb:
            raise RuntimeError('知识库不存在')
        user = await fetchone(db, 'SELECT * FROM users WHERE id=?', (user_id,))
        limits = limits_for_user(user)
        room = limits['storage_bytes'] - await storage_used_bytes(db, user_id)
        if room <= 0:
            raise RuntimeError(f"{limits['label']}可用空间已满，升级会员可继续添加")

        saved: list[dict] = []
        skipped: list[str] = []

        async def store(storage_ref: str, name: str, size: int) -> str:
            """登记成知识库文档：分块 + 全文索引 + 计数，和上传走同一份实现。"""
            document_id, has_text = await register_knowledge_document(
                db, knowledge_id=knowledge_id, user_id=user_id,
                storage_ref=storage_ref, name=name, size=size, folder_id=folder_id,
            )
            if has_text:
                # 有正文才排整理任务：空正文排了也是一条空摘要
                to_organize.append(document_id)
            return document_id

        if content:
            name = f'{title}.md'
            document_id = uuid.uuid4().hex
            body = f'# {title}\n\n{content}\n'
            data = body.encode('utf-8')
            if len(data) > room:
                raise RuntimeError(f"{limits['label']}可用空间不足，先清理或升级会员")
            storage_ref = await storage.save_bytes(data, f'uploads/documents/{document_id}_{name}', content_type='text/markdown; charset=utf-8')
            room -= len(data)
            await store(storage_ref, name, len(data))
            saved.append({'name': name, 'kind': 'text'})

        for artifact_id in artifact_ids:
            row = await fetchone(db, 'SELECT * FROM artifacts WHERE id=? AND user_id=?', (artifact_id, user_id))
            if not row:
                continue
            source_ref = artifact_service.stored_ref(row)
            name = Path(str(row['filename'] or '文件')).name[:120] or '文件'
            if not await storage.exists(source_ref):
                skipped.append(name)
                continue
            size = int(row['file_size'] or 0)
            if size > room:
                skipped.append(name)
                continue
            document_id = uuid.uuid4().hex
            try:
                destination_ref = await storage.copy_ref(source_ref, f'uploads/documents/{document_id}_{name}')
            except storage.StorageError:
                skipped.append(name)
                continue
            room -= size
            await store(destination_ref, name, size)
            saved.append({'name': name, 'kind': 'file'})

        await db.commit()
    finally:
        await db.close()
    for document_id in to_organize:
        schedule_background(organize_document(document_id))
    return {
        'knowledge_id': knowledge_id,
        'knowledge_name': kb['name'],
        'saved': saved,
        'skipped': skipped,
        'message': f'已存入「{kb["name"]}」' + (f'，{len(skipped)} 个文件因空间不足未存入' if skipped else ''),
    }


@app.post('/api/shares/to-knowledge')
async def share_to_knowledge(payload: ShareToKnowledge, user_id: str = Depends(current_user)) -> dict:
    """把对话里勾选的内容与文件存进指定知识库。

    正文按 Markdown 文档入库（可检索、可被 AI 整理），勾选的产物复制一份进知识库，
    对话里的原件不动。文件要逐个抽取正文，可能超过容器通道单次调用上限，
    所以只做校验后排队，客户端轮询 /api/jobs/{id} 拿 saved / skipped。
    """
    rate_limit(f"share-kb:{user_id}", 60, 60, '保存过于频繁，请稍后再试')
    title = re.sub(r'[\\/:*?"<>|]', ' ', (payload.title or '').strip())[:60].strip() or '对话内容'
    content = (payload.content or '').strip()
    folder_id = payload.folder_id or ''
    if not content and not payload.artifact_ids:
        raise HTTPException(400, '没有选中可保存的内容')
    db = await connect()
    try:
        kb = await fetchone(db, "SELECT id,name FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (payload.knowledge_id, user_id))
        if not kb:
            raise HTTPException(404, '知识库不存在')
        if folder_id:
            folder = await fetchone(db, 'SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?', (folder_id, user_id, payload.knowledge_id))
            if not folder:
                raise HTTPException(404, '目标文件夹不存在')
        user = await fetchone(db, 'SELECT * FROM users WHERE id=?', (user_id,))
        if not account_state(user, 0)['entitlements']['can_upload']:
            raise HTTPException(403, '免费试用已结束，开通会员后可继续添加资料')
    finally:
        await db.close()
    job_id = await jobs_service.create(user_id, 'share_import', {'knowledge_id': payload.knowledge_id, 'files': len(payload.artifact_ids)})
    jobs_service.spawn(job_id, lambda jid: share_to_knowledge_worker(
        jid, knowledge_id=payload.knowledge_id, user_id=user_id, title=title,
        content=content, artifact_ids=list(payload.artifact_ids), folder_id=folder_id,
    ))
    return {'pending': True, 'job_id': job_id}



# 好友点开分享卡片后落地的那个知识库：固定名字、每个用户只有一个。
# 它是「收件箱」而不是用户自建的资料库，所以不占「最多 N 个资料库」的名额，容量仍按会员档位统一算。
SHARED_KNOWLEDGE_NAME = '共享知识库'
SHARED_KNOWLEDGE_DESCRIPTION = '好友通过分享链接发来的对话与文件，都收在这里'
SHARED_KNOWLEDGE_ICON = 'book'
SHARED_FOLDER_NAME = '分享的文件'


async def ensure_shared_knowledge(db: Any, user_id: str) -> Any:
    """拿到（必要时创建）这个用户的「共享知识库」。"""
    row = await fetchone(db, "SELECT id,name FROM knowledge_bases WHERE user_id=? AND name=? AND status='active'", (user_id, SHARED_KNOWLEDGE_NAME))
    if row:
        return row
    knowledge_id = uuid.uuid4().hex
    timestamp = now()
    await db.execute(
        "INSERT INTO knowledge_bases(id,user_id,name,description,icon,document_count,visibility,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (knowledge_id, user_id, SHARED_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_DESCRIPTION, SHARED_KNOWLEDGE_ICON, 0, 'private', 'active', timestamp, timestamp),
    )
    return await fetchone(db, 'SELECT id,name FROM knowledge_bases WHERE id=?', (knowledge_id,))


async def ensure_shared_folder(db: Any, user_id: str, knowledge_id: str) -> str:
    """共享知识库里专门放文件的那一层：文件归到「分享的文件」，对话正文放根目录。"""
    row = await fetchone(db, 'SELECT id FROM folders WHERE knowledge_id=? AND user_id=? AND name=?', (knowledge_id, user_id, SHARED_FOLDER_NAME))
    if row:
        return str(row['id'])
    folder_id = uuid.uuid4().hex
    timestamp = now()
    await db.execute('INSERT INTO folders(id,knowledge_id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?,?)', (folder_id, knowledge_id, user_id, SHARED_FOLDER_NAME, timestamp, timestamp))
    return folder_id


async def claim_share_worker(job_id: str, *, share_id: str, user_id: str) -> dict:
    """把好友分享的内容收进「共享知识库」。

    分流规则：
      · 卡片带文件 → 复制一份进「共享知识库 / 分享的文件」，拷不动的记在 skipped 里，不静默丢；
      · 卡片带正文 → 正文落成一篇 Markdown 放在共享知识库根目录，能被检索、能被继续追问。
    收件记录落在 share_claims：同一个人重复点开同一张卡片不会重复入库。
    文件要复制 + 抽取正文，可能超过容器通道单次调用上限，所以整段放在任务里跑。
    """
    await jobs_service.set_progress(job_id, '正在收取分享内容…')
    db = await connect()
    to_organize: list[str] = []
    try:
        card = await fetchone(db, 'SELECT id,user_id,title,knowledge_name,question,answer,sources_json,files_json FROM share_cards WHERE id=?', (share_id,))
        if not card:
            raise RuntimeError('分享内容不存在或已失效')

        shared = await ensure_shared_knowledge(db, user_id)
        knowledge_id = str(shared['id'])
        knowledge_name = str(shared['name'])

        claimed = await fetchone(db, 'SELECT knowledge_id,folder_id,documents_json FROM share_claims WHERE share_id=? AND user_id=?', (share_id, user_id))
        if claimed:
            documents = decode_sources(claimed['documents_json'])
            return {'knowledge_id': str(claimed['knowledge_id']), 'knowledge_name': knowledge_name, 'folder_id': str(claimed['folder_id'] or ''), 'documents': documents, 'skipped': [], 'already': True, 'message': f'这条分享已经在你的「{knowledge_name}」里了'}

        files = decode_sources(card['files_json'])
        answer = str(card['answer'] or '').strip()
        question = str(card['question'] or '').strip()
        if not answer and not files:
            raise RuntimeError('这条分享没有可收取的内容')

        user = await fetchone(db, 'SELECT * FROM users WHERE id=?', (user_id,))
        limits = limits_for_user(user)
        if not account_state(user, 0)['entitlements']['can_upload']:
            raise RuntimeError('免费试用已结束，开通会员后可继续收下分享内容')
        room = limits['storage_bytes'] - await storage_used_bytes(db, user_id)
        if room <= 0:
            raise RuntimeError(f"{limits['label']}可用空间已满，升级会员后可继续收下分享内容")

        folder_id = ''
        saved: list[dict] = []
        skipped: list[str] = []

        # 正文：一句话也能当一篇资料存下来，好友后续可以直接在这篇上追问
        if answer:
            raw_title = str(card['title'] or question or '分享的内容')
            title = re.sub(r'[\\/:*?"<>|]', ' ', raw_title).strip()[:60] or '分享的内容'
            name = f'{title}.md'
            body = f'# {title}\n\n'
            if question:
                body += f'**好友的提问**\n\n{question}\n\n'
            body += answer + '\n'
            sources = decode_sources(card['sources_json'])
            if sources:
                lines = []
                for index, item in enumerate(sources):
                    label = str(item.get('filename') or item.get('url') or '出处')
                    page = int(item.get('page_number') or 0)
                    lines.append(f'{index + 1}. {label}' + (f'（第 {page} 页）' if page else ''))
                body += '\n\n---\n\n**参考出处**\n\n' + '\n'.join(lines) + '\n'
            data = body.encode('utf-8')
            if len(data) > room:
                raise RuntimeError(f"{limits['label']}可用空间不足，先清理或升级会员")
            document_id = uuid.uuid4().hex
            storage_ref = await storage.save_bytes(data, f'uploads/documents/{document_id}_{name}', content_type='text/markdown; charset=utf-8')
            room -= len(data)
            registered, has_text = await register_knowledge_document(
                db, knowledge_id=knowledge_id, user_id=user_id, storage_ref=storage_ref, name=name, size=len(data),
            )
            if has_text:
                to_organize.append(registered)
            saved.append({'name': name, 'kind': 'text', 'document_id': registered})

        # 文件：拷一份到收件人的空间里，原件仍在分享者名下，两边互不影响
        for item in files:
            artifact_id = str(item.get('artifact_id') or '')
            row = await fetchone(db, 'SELECT id,filename,file_size,storage_path FROM artifacts WHERE id=? AND user_id=?', (artifact_id, card['user_id']))
            if not row:
                skipped.append(str(item.get('name') or '文件')); continue
            source_ref = str(row['storage_path'] or '')
            name = Path(str(row['filename'] or item.get('name') or '文件')).name[:120] or '文件'
            if not await storage.exists(source_ref):
                skipped.append(name); continue
            size = int(row['file_size'] or 0)
            if size > room:
                skipped.append(name); continue
            if not folder_id:
                folder_id = await ensure_shared_folder(db, user_id, knowledge_id)
            document_id = uuid.uuid4().hex
            try:
                destination_ref = await storage.copy_ref(source_ref, f'uploads/documents/{document_id}_{name}')
            except storage.StorageError:
                skipped.append(name); continue
            room -= size
            registered, has_text = await register_knowledge_document(
                db, knowledge_id=knowledge_id, user_id=user_id, storage_ref=destination_ref, name=name, size=size, folder_id=folder_id,
            )
            if has_text:
                to_organize.append(registered)
            saved.append({'name': name, 'kind': 'file', 'document_id': registered})

        await db.execute(
            'INSERT OR REPLACE INTO share_claims(share_id,user_id,knowledge_id,folder_id,documents_json,created_at) VALUES(?,?,?,?,?,?)',
            (share_id, user_id, knowledge_id, folder_id, json.dumps(saved, ensure_ascii=False), now()),
        )
        await db.commit()
    finally:
        await db.close()
    for document_id in to_organize:
        schedule_background(organize_document(document_id))
    if not saved:
        return {'knowledge_id': knowledge_id, 'knowledge_name': knowledge_name, 'folder_id': folder_id, 'documents': [], 'skipped': skipped, 'already': False, 'message': '分享内容没能存下来，请稍后重试'}
    tail = f'，{len(skipped)} 个文件因空间不足未存入' if skipped else ''
    return {'knowledge_id': knowledge_id, 'knowledge_name': knowledge_name, 'folder_id': folder_id, 'documents': saved, 'skipped': skipped, 'already': False, 'message': f'已收进「{knowledge_name}」{tail}'}


@app.post('/api/shares/{share_id}/claim')
async def claim_share(share_id: str, user_id: str = Depends(current_user)) -> dict:
    """把好友分享给你的内容收进「共享知识库」。

    分享页对好友是公开的，但「收进来」是收件人的动作，所以这一步要登录（小程序里就是微信登录）。
    同一张卡片谁来点就收进谁的共享知识库，彼此不串。内容多时要复制文件并抽取正文，
    可能超过容器通道单次调用上限，所以只做校验后排队，客户端轮询 /api/jobs/{id}。
    """
    if not valid_share_id(share_id):
        raise HTTPException(404, '分享内容不存在或已失效')
    rate_limit(f"share-claim:{user_id}", 60, 60, '收件过于频繁，请稍后再试')
    db = await connect()
    try:
        card = await fetchone(db, 'SELECT id FROM share_cards WHERE id=?', (share_id,))
        if not card:
            raise HTTPException(404, '分享内容不存在或已失效')
    finally:
        await db.close()
    job_id = await jobs_service.create(user_id, 'share_import', {'share_id': share_id})
    jobs_service.spawn(job_id, lambda jid: claim_share_worker(jid, share_id=share_id, user_id=user_id))
    return {'pending': True, 'job_id': job_id}



# ---- 知识库邀请：把「整个资料库」分享给微信好友 ----

@app.post('/api/knowledge/{knowledge_id}/share')
async def create_knowledge_share(knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    """生成「邀请好友一起用这个资料库」的分享链接。

    安全模型：一条链接只认第一个接受的好友。谁先点「接受」，链接就绑到谁身上；
    之后无论是接受者转发，还是群里其他人转发，别人再打开只会看到「已被领取」。
    要再邀请下一位好友，重新分享一次（会生成一条新链接）。
    """
    rate_limit(f'kb-share:{user_id}', 30, 60, '操作过于频繁，请稍后再试')
    db = await connect()
    kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb:
        await db.close(); raise HTTPException(404, '知识库不存在')
    kb_name = str(kb['name'] or '')
    if kb_name in (DEFAULT_KNOWLEDGE_NAME, SHARED_KNOWLEDGE_NAME):
        await db.close(); raise HTTPException(403, f'「{kb_name}」不能分享给好友')
    if str(kb['mirror_of'] or ''):
        await db.close(); raise HTTPException(403, '好友共享给你的知识库不能再分享出去')
    # 同一个知识库同时只保留一条待领取的链接：重复分享复用同一张卡片，不会给好友发一堆失效链接
    pending = await fetchone(
        db,
        "SELECT token,expires_at FROM knowledge_shares WHERE knowledge_id=? AND owner_user_id=? AND revoked=0 AND accepted_by='' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
        (knowledge_id, user_id, now()),
    )
    if pending:
        token, expires_at = str(pending['token']), str(pending['expires_at'])
    else:
        token = secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=KNOWLEDGE_SHARE_TTL_DAYS)).isoformat()
        await db.execute('INSERT INTO knowledge_shares(token,knowledge_id,owner_user_id,created_at,expires_at) VALUES(?,?,?,?,?)', (token, knowledge_id, user_id, now(), expires_at))
    await db.commit(); await db.close()
    return {'token': token, 'path': f'/package-features/pages/kb-share/index?kb={token}', 'name': str(kb['name'] or ''), 'expires_at': expires_at, 'days': KNOWLEDGE_SHARE_TTL_DAYS}


@app.get('/api/knowledge-shares/{token}')
async def read_knowledge_share(token: str, user_id: str = Depends(current_user_optional)) -> dict:
    """分享落地页的预览：只给名称、简介和资料数量，不带内容、不带分享者身份。"""
    rate_limit(f'kb-share-read:{token[:16]}', 200, 60, '访问过于频繁，请稍后再试')
    if not valid_share_token(token):
        raise HTTPException(404, '分享链接无效')
    db = await connect()
    share = await fetchone(db, 'SELECT * FROM knowledge_shares WHERE token=?', (token,))
    if not share:
        await db.close(); raise HTTPException(404, '分享链接无效')
    state = await knowledge_share_state(db, share, user_id)
    kb = await fetchone(db, "SELECT id,name,description,icon,document_count,updated_at FROM knowledge_bases WHERE id=? AND status='active'", (str(share['knowledge_id']),))
    joined = ''
    if user_id:
        mirror = await fetchone(db, "SELECT id FROM knowledge_bases WHERE user_id=? AND mirror_token=? AND status='active'", (user_id, token))
        joined = str(mirror['id']) if mirror else ''
    # 分享者自己点开这条链接：这就是他自己的库，不该出现「接受」，直接给「打开知识库」
    if user_id and str(share['owner_user_id'] or '') == user_id:
        state = 'mine'
        joined = joined or str(share['knowledge_id'])
    await db.close()
    if not kb:
        return {'state': 'missing', 'available': False, 'name': '', 'description': '', 'document_count': 0, 'expires_at': str(share['expires_at'] or ''), 'joined_knowledge_id': ''}
    return {
        'state': state,
        'available': state in {'open', 'mine'},
        'name': str(kb['name'] or ''),
        'description': str(kb['description'] or ''),
        'document_count': int(kb['document_count'] or 0),
        'expires_at': str(share['expires_at'] or ''),
        'joined_knowledge_id': joined,
    }


@app.post('/api/knowledge-shares/{token}/accept')
async def accept_knowledge_share(token: str, user_id: str = Depends(current_user)) -> dict:
    """接受好友的知识库邀请：在自己的「共享知识库」里挂一份只读镜像。"""
    rate_limit(f'kb-accept:{user_id}', 30, 60, '操作过于频繁，请稍后再试')
    if not valid_share_token(token):
        raise HTTPException(404, '分享链接无效')
    db = await connect()
    share = await fetchone(db, 'SELECT * FROM knowledge_shares WHERE token=?', (token,))
    if not share:
        await db.close(); raise HTTPException(404, '分享链接无效')
    # 分享者自己点「接受」：这是他自己的库。这一步必须卡在「领取」之前，
    # 否则链接会先被自己领走，真正的好友反而看到「已被领取」。
    source_owner = await fetchone(db, 'SELECT user_id FROM knowledge_bases WHERE id=?', (str(share['knowledge_id']),))
    if source_owner and str(source_owner['user_id'] or '') == user_id:
        await db.close(); raise HTTPException(409, '这是你自己的知识库，无需接受')
    state = await knowledge_share_state(db, share, user_id)
    if state == 'revoked':
        await db.close(); raise HTTPException(410, '分享者已关闭这条链接，请让对方重新分享')
    if state == 'expired':
        await db.close(); raise HTTPException(410, '这条分享链接已过期，请让好友重新分享')
    if state == 'taken':
        await db.close(); raise HTTPException(403, '这条分享链接已被其他好友领取，请让分享者重新发一条')
    if state == 'open':
        # 原子领取：并发下只有把 accepted_by 从空写成自己的那次请求算领到手
        cursor = await db.execute("UPDATE knowledge_shares SET accepted_by=?,accepted_at=? WHERE token=? AND accepted_by='' AND revoked=0", (user_id, now(), token))
        await db.commit()
        if cursor.rowcount == 0:
            fresh = await fetchone(db, 'SELECT accepted_by FROM knowledge_shares WHERE token=?', (token,))
            await db.close()
            if not fresh or str(fresh['accepted_by'] or '') != user_id:
                raise HTTPException(403, '这条分享链接已被其他好友领取，请让分享者重新发一条')
    source = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND status='active'", (str(share['knowledge_id']),))
    if not source:
        await db.close(); raise HTTPException(404, '来源知识库已被删除')
    mirror = await fetchone(db, "SELECT * FROM knowledge_bases WHERE user_id=? AND mirror_token=? AND status='active'", (user_id, token))
    created = False
    if not mirror:
        mirror_id = uuid.uuid4().hex
        timestamp = now()
        await db.execute(
            'INSERT INTO knowledge_bases(id,user_id,name,description,icon,avatar,document_count,visibility,status,created_at,updated_at,mirror_of,mirror_owner,mirror_state,mirror_token,mirror_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mirror_id, user_id, str(source['name']), str(source['description'] or ''), str(source['icon'] or DEFAULT_KNOWLEDGE_ICON), str(source['avatar'] or ''), 0, 'private', 'active',
             timestamp, timestamp, str(source['id']), str(source['user_id']), 'ok', token, timestamp),
        )
        await db.commit()
        mirror = await fetchone(db, 'SELECT * FROM knowledge_bases WHERE id=?', (mirror_id,))
        created = True
    info = await sync_mirror(db, mirror)
    await db.commit(); await db.close()
    return {
        'knowledge_id': str(mirror['id']), 'name': str(mirror['name'] or ''), 'documents': int(info['count'] or 0),
        'already': not created, 'read_only': True,
        'message': f"已加入「{mirror['name']}」，{int(info['count'] or 0)} 份资料可以随时提问",
    }


@app.post('/api/knowledge-shares/{token}/revoke')
async def revoke_knowledge_share(token: str, user_id: str = Depends(current_user)) -> dict:
    """分享者关闭一条邀请链接：已经领走这个共享库的好友也会同时失去它。"""
    if not valid_share_token(token):
        raise HTTPException(404, '分享链接无效')
    db = await connect()
    share = await fetchone(db, 'SELECT * FROM knowledge_shares WHERE token=? AND owner_user_id=?', (token, user_id))
    if not share:
        await db.close(); raise HTTPException(404, '分享链接不存在')
    await db.execute('UPDATE knowledge_shares SET revoked=1 WHERE token=?', (token,))
    mirrors = await fetchall(db, 'SELECT id FROM knowledge_bases WHERE mirror_token=?', (token,))
    for item in mirrors:
        mirror_id = str(item['id'])
        for doc in await fetchall(db, "SELECT id,storage_path FROM documents WHERE knowledge_id=? AND status!='deleted'", (mirror_id,)):
            await drop_mirror_document(db, mirror_id, str(doc['id']), str(doc['storage_path'] or ''))
        await db.execute('DELETE FROM folders WHERE knowledge_id=?', (mirror_id,))
        await db.execute('DELETE FROM conversations WHERE knowledge_id=?', (mirror_id,))
        await db.execute('DELETE FROM knowledge_bases WHERE id=?', (mirror_id,))
    await db.commit(); await db.close()
    return {'ok': True, 'removed': len(mirrors)}


# ---- 技能：技能广场（所有人可用）与我的技能（用户级隔离）----

# 与前端技能编辑页的可选图标一致；保留早期用过的图标名，避免旧技能在编辑时被改写
SKILL_ICONS = {'skill-node', 'knowledge-pick', 'book', 'ppt', 'image', 'sousuo', 'sliders', 'wangluo', 'atom', 'robot', 'liebiao', 'shuju', 'history', 'dui', 'dengpao', 'tag',
               # 内置技能用的专属图标（每个技能一个语义，前端 utils/icons.ts 里有同名映射）
               'skill-organize', 'skill-report', 'skill-deck', 'skill-diagram', 'skill-contract',
               'skill-meeting', 'skill-data', 'skill-reading', 'skill-research', 'skill-writing'}

SKILL_NAME_MAX = 30
# 技能配额不再写死在这里：跟随会员档位（MEMBERSHIP_LIMITS[*]['skills']）


def skill_payload(row: Any, viewer: str) -> dict:
    keys = set(row.keys())
    owner = row['user_id'] or ''
    return {
        'id': row['id'],
        'name': row['name'],
        'summary': row['summary'] or '',
        'prompt': row['prompt'] or '',
        'icon': row['icon'] or 'skill-node',
        'developer_wechat': row['developer_wechat'] or '',
        'source': row['source'] or 'custom',
        'builtin': (row['source'] or '') == 'builtin',
        'visibility': row['visibility'] or 'private',
        'published': (row['visibility'] or '') == 'public',
        'is_owner': bool(owner) and owner == viewer,
        'harness': row['harness'] or '',
        'use_count': int(row['use_count'] or 0),
        'like_count': int(row['like_count'] or 0),
        'favorite_count': int(row['favorite_count'] or 0),
        'liked': bool(row['liked']) if 'liked' in keys else False,
        'favorited': bool(row['favorited']) if 'favorited' in keys else False,
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def clean_skill_form(payload: SkillForm) -> dict[str, str]:
    icon = payload.icon if payload.icon in SKILL_ICONS else 'skill-node'
    return {
        'name': payload.name.strip()[:SKILL_NAME_MAX],
        'summary': payload.summary.strip(),
        'prompt': payload.prompt.strip(),
        'developer_wechat': payload.developer_wechat.strip(),
        'icon': icon,
    }


MAX_TURN_SKILLS = 8


async def resolve_turn_skills(db, user_id: str, skill_ids: list[str]) -> list[dict[str, str]]:
    """把前端选中的技能（可多选）解析成本轮要注入的内容。

    只有用户明确选择（id 非空）才会注入：内置技能走仓库内的技能包 slug，
    我的技能走技能指令文本；别人的私有技能在这里解析为空，保证用户级隔离。
    重复 id 只保留一次，顺序保持用户选择的先后。
    """
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in skill_ids or []:
        slug = (raw or '').strip()
        if not slug or len(slug) > 40 or slug in seen:
            continue
        seen.add(slug)
        row = await fetchone(db, "SELECT * FROM skills WHERE id=? AND status='active'", (slug,))
        if not row:
            continue
        owner = row['user_id'] or ''
        if owner and owner != user_id and (row['visibility'] or '') != 'public':
            continue
        harness = (row['harness'] or '').strip()
        # 内置技能包走仓库里的 slug；用户自建技能走 skill-creator 生成的技能包
        # （harness=usr-xxx，安装在该租户的技能根下）。两者都是「真技能包加载」，
        # 只有技能包缺失（被人为删掉、导入的历史数据）时才回落到指令注入。
        packaged = bool(harness) and (
            harness in SKILL_LABELS or skill_build.package_installed(user_id, harness)
        )
        if packaged:
            resolved.append({'id': slug, 'harness': harness, 'prompt': '', 'label': row['name'] or ''})
        else:
            resolved.append({'id': slug, 'harness': '', 'prompt': row['prompt'] or '', 'label': row['name'] or ''})
        if len(resolved) >= MAX_TURN_SKILLS:
            break
    return resolved


@app.get('/api/skills')
async def list_skills(
    scope: str = Query(default='market', pattern='^(market|mine)$'),
    q: str = Query(default='', max_length=40),
    user_id: str = Depends(current_user),
) -> list[dict]:
    """技能广场（所有人发布的技能 + 内置技能）与我的技能（仅自己可见）。"""
    liked = "EXISTS(SELECT 1 FROM skill_likes l WHERE l.skill_id=s.id AND l.user_id=?)"
    favorited = "EXISTS(SELECT 1 FROM skill_favorites f WHERE f.skill_id=s.id AND f.user_id=?)"
    keyword = q.strip()
    pattern = f'%{keyword}%'
    db = await connect()
    if scope == 'mine':
        rows = await fetchall(
            db,
            f"SELECT s.*, {liked} AS liked, {favorited} AS favorited FROM skills s WHERE s.user_id=? AND s.status='active' AND (?='' OR s.name LIKE ? OR s.summary LIKE ?) ORDER BY s.updated_at DESC LIMIT 200",
            (user_id, user_id, user_id, keyword, pattern, pattern),
        )
    else:
        rows = await fetchall(
            db,
            f"SELECT s.*, {liked} AS liked, {favorited} AS favorited FROM skills s WHERE s.status='active' AND s.visibility='public' AND (?='' OR s.name LIKE ? OR s.summary LIKE ?) ORDER BY (s.source='builtin') DESC, (s.favorite_count + s.like_count) DESC, s.created_at DESC LIMIT 200",
            (user_id, user_id, keyword, pattern, pattern),
        )
    await db.close()
    return [skill_payload(row, user_id) for row in rows]



@app.get('/api/skills/{skill_id}')
async def read_skill(skill_id: str, user_id: str = Depends(current_user)) -> dict:
    """读取单个技能：技能编辑页回填用。

    内置技能与已发布到广场的技能所有人可读；「我的技能」默认私有，只有作者本人能读到。
    """
    slug = (skill_id or '').strip()
    if not slug or len(slug) > 40:
        raise HTTPException(404, '技能不存在')
    db = await connect()
    row = await fetchone(db, "SELECT * FROM skills WHERE id=? AND status='active'", (slug,))
    if not row:
        await db.close(); raise HTTPException(404, '技能不存在')
    owner = row['user_id'] or ''
    if owner and owner != user_id and (row['visibility'] or '') != 'public':
        await db.close(); raise HTTPException(404, '技能不存在')
    payload = skill_payload(row, user_id)
    await db.close()
    # 技能包型技能：编辑页要看到真实内容与包内文件，而不是一条空指令
    slug = (row['harness'] or '').strip()
    if slug and skill_build.package_installed(user_id, slug):
        payload['files'] = skill_build.package_files(user_id, slug)
        payload['package'] = True
        if not payload.get('prompt'):
            payload['prompt'] = skill_build.read_package_text(user_id, slug)
    else:
        payload['files'] = []
        payload['package'] = False
    return payload

@app.post('/api/skills')
async def create_skill(payload: SkillForm, user_id: str = Depends(current_user)) -> dict:
    """新建「我的技能」：默认私有，只有自己能用，需要时才发布到广场。"""
    rate_limit(f"skill-write:{user_id}", 30, 3600, '新建技能过于频繁，请稍后再试')
    form = clean_skill_form(payload)
    db = await connect()
    # 技能配额按会员档位走：试用 1 个、Plus 5 个、Pro 10 个
    account = await fetchone(db, 'SELECT * FROM users WHERE id=?', (user_id,))
    limit = limits_for_user(account)['skills']
    if await owned_skill_count(db, user_id) >= limit:
        await db.close()
        raise HTTPException(400, f'当前会员可创建 {limit} 个技能，已用完。升级会员可以创建更多。')
    skill_id = f"sk-{uuid.uuid4().hex[:12]}"
    stamp = now()
    await db.execute(
        "INSERT INTO skills(id,user_id,name,summary,prompt,icon,developer_wechat,harness,visibility,source,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'','private','custom','active',?,?)",
        (skill_id, user_id, form['name'], form['summary'], form['prompt'], form['icon'], form['developer_wechat'], stamp, stamp),
    )
    await db.commit()
    row = await fetchone(db, 'SELECT * FROM skills WHERE id=?', (skill_id,))
    await db.close()
    return skill_payload(row, user_id)


@app.patch('/api/skills/{skill_id}')
async def update_skill(skill_id: str, payload: SkillForm, user_id: str = Depends(current_user)) -> dict:
    """编辑自己的技能：内置技能与他人技能一律不可改。"""
    form = clean_skill_form(payload)
    db = await connect()
    row = await fetchone(db, "SELECT * FROM skills WHERE id=? AND status='active'", (skill_id,))
    if not row or (row['user_id'] or '') != user_id:
        await db.close(); raise HTTPException(404, '技能不存在或不属于你')
    await db.execute(
        'UPDATE skills SET name=?, summary=?, prompt=?, icon=?, developer_wechat=?, updated_at=? WHERE id=?',
        (form['name'], form['summary'], form['prompt'], form['icon'], form['developer_wechat'], now(), skill_id),
    )
    await db.commit()
    updated = await fetchone(db, 'SELECT * FROM skills WHERE id=?', (skill_id,))
    await db.close()
    # 技能包型技能：编辑页改完要同步回 SKILL.md，否则文件与数据库会静默不一致
    slug = (row['harness'] or '').strip()
    if slug and skill_build.package_installed(user_id, slug):
        skill_build.write_package_text(user_id, slug, form['name'], form['summary'], form['prompt'])
    return skill_payload(updated, user_id)


@app.delete('/api/skills/{skill_id}')
async def delete_skill(skill_id: str, user_id: str = Depends(current_user)) -> dict:
    """删除自己的技能：连带清掉点赞 / 收藏记录，以及 harness 生成的技能包文件。

    已发布到技能广场的会一并下架——广场列表就是这张表，行删掉即不再公开；
    技能包（SKILL.md 与脚本）与构建残留同时删除，不在磁盘上留垃圾文件。
    """
    db = await connect()
    row = await fetchone(db, "SELECT * FROM skills WHERE id=? AND status='active'", (skill_id,))
    if not row or (row['user_id'] or '') != user_id:
        await db.close(); raise HTTPException(404, '技能不存在或不属于你')
    if (row['source'] or '') == 'builtin':
        await db.close(); raise HTTPException(400, '内置技能不可删除')
    slug = (row['harness'] or '').strip()
    await db.execute('DELETE FROM skill_likes WHERE skill_id=?', (skill_id,))
    await db.execute('DELETE FROM skill_favorites WHERE skill_id=?', (skill_id,))
    await db.execute('DELETE FROM skills WHERE id=? AND user_id=?', (skill_id, user_id))
    await db.commit(); await db.close()
    removed = skill_build.remove_package(user_id, slug) if slug else False
    return {'ok': True, 'files_removed': removed}


@app.post('/api/skills/{skill_id}/publish')
async def publish_skill(skill_id: str, payload: SkillPublishUpdate, user_id: str = Depends(current_user)) -> dict:
    """发布到技能广场 / 从广场下架：只有作者本人能操作。"""
    db = await connect()
    row = await fetchone(db, "SELECT * FROM skills WHERE id=? AND status='active'", (skill_id,))
    if not row or (row['user_id'] or '') != user_id:
        await db.close(); raise HTTPException(404, '技能不存在或不属于你')
    if (row['source'] or '') == 'builtin':
        await db.close(); raise HTTPException(400, '内置技能已在技能广场')
    stamp = now()
    await db.execute(
        'UPDATE skills SET visibility=?, published_at=?, updated_at=? WHERE id=? AND user_id=?',
        ('public' if payload.published else 'private', stamp if payload.published else '', stamp, skill_id, user_id),
    )
    await db.commit()
    updated = await fetchone(db, 'SELECT * FROM skills WHERE id=?', (skill_id,))
    await db.close()
    return skill_payload(updated, user_id)


async def toggle_skill_flag(table: str, skill_id: str, user_id: str, active: bool) -> dict:
    """点赞 / 收藏开关：技能广场与自己的技能都可以点，计数以明细表为准。"""
    counter = 'like_count' if table == 'skill_likes' else 'favorite_count'
    db = await connect()
    row = await fetchone(db, "SELECT * FROM skills WHERE id=? AND status='active'", (skill_id,))
    if not row:
        await db.close(); raise HTTPException(404, '技能不存在')
    owner = row['user_id'] or ''
    if owner and owner != user_id and (row['visibility'] or '') != 'public':
        await db.close(); raise HTTPException(404, '技能不存在')
    if active:
        await db.execute(f'INSERT OR IGNORE INTO {table}(skill_id,user_id,created_at) VALUES(?,?,?)', (skill_id, user_id, now()))
    else:
        await db.execute(f'DELETE FROM {table} WHERE skill_id=? AND user_id=?', (skill_id, user_id))
    total = await fetchone(db, f'SELECT COUNT(*) AS c FROM {table} WHERE skill_id=?', (skill_id,))
    count = int((total['c'] if total else 0) or 0)
    await db.execute(f'UPDATE skills SET {counter}=?, updated_at=? WHERE id=?', (count, now(), skill_id))
    await db.commit(); await db.close()
    return {'id': skill_id, 'active': bool(active), 'count': count}


@app.post('/api/skills/{skill_id}/like')
async def like_skill(skill_id: str, payload: SkillFlagUpdate, user_id: str = Depends(current_user)) -> dict:
    rate_limit(f"skill-like:{user_id}", 80, 60, '操作过于频繁，请稍后再试')
    result = await toggle_skill_flag('skill_likes', skill_id, user_id, payload.active)
    return {**result, 'field': 'like_count'}


@app.post('/api/skills/{skill_id}/favorite')
async def favorite_skill(skill_id: str, payload: SkillFlagUpdate, user_id: str = Depends(current_user)) -> dict:
    rate_limit(f"skill-favorite:{user_id}", 80, 60, '操作过于频繁，请稍后再试')
    result = await toggle_skill_flag('skill_favorites', skill_id, user_id, payload.active)
    return {**result, 'field': 'favorite_count'}


# ---- 制作技能：由 harness 加载 skill-creator 真的写出技能包 ----

# 增强提示词用的模板：和目标技能包的 SKILL.md 结构保持一致，两条路径产出同一种形状。
SKILL_PROMPT_TEMPLATE = (
    '你是技能说明书编辑。用户会给你一句技能想法，你要把它改写成一段可直接执行的技能指令。\n'
    '严格按下面四个小标题输出，不要开场白、不要解释你在做什么：\n'
    '## 目标\n用一句话说明这个技能交付什么。\n'
    '## 执行步骤\n3-6 步，动词开头，每步具体到能照做。\n'
    '## 输出格式\n写清输出分几节、每节放什么；有固定版式就直接给骨架。\n'
    '## 注意事项\n2-4 条边界，例如资料不足怎么写、不要编造什么。\n'
    '只使用简体中文；不要引入用户没提到的功能；用户写得含糊的地方按最合理的常见做法定下来，不要反问；全文控制在 400 字以内。'
)


async def enhance_skill_worker(job_id: str, text: str, name: str) -> dict:
    """后台跑一次改写：模型调用可能到几十秒，超出容器通道单次调用上限。

    走直连模型通道而不是 harness：这是一次纯文本改写，没必要为它付运行时冷启动的成本。
    """
    from .services.llm import provider_config
    base_url, api_key, model = provider_config('')
    root = base_url.rstrip('/')
    endpoint = f"{root}/chat/completions" if root.endswith('/v1') else f"{root}/v1/chat/completions"
    subject = f'技能名称：{name}\n' if name else ''
    await jobs_service.set_progress(job_id, '正在改写提示词…')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            response = await client.post(endpoint, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={
                'model': model, 'temperature': 0.4, 'max_tokens': 900,
                'messages': [
                    {'role': 'system', 'content': SKILL_PROMPT_TEMPLATE},
                    {'role': 'user', 'content': f'{subject}用户的想法：{text}'},
                ],
            })
            response.raise_for_status()
            enhanced = str(response.json()['choices'][0]['message']['content'] or '').strip()
    except Exception as exc:
        print(f'[skill] 增强提示词失败：{exc}', flush=True)
        raise RuntimeError('增强提示词暂时不可用，请稍后重试') from exc
    if not enhanced:
        raise RuntimeError('增强提示词暂时不可用，请稍后重试')
    return {'instruction': enhanced[:4000]}


@app.post('/api/skills/enhance')
async def enhance_skill_prompt(payload: SkillEnhanceRequest, user_id: str = Depends(current_user)) -> dict:
    """增强提示词：登记后台任务，客户端轮询 /api/jobs/{id} 取结果。

    模型改写经常要几十秒，远超云托管单次调用上限，不能同步等。
    """
    rate_limit(f"skill-enhance:{user_id}", 20, 600, '增强提示词过于频繁，请稍后再试')
    text = (payload.instruction or '').strip()
    if not text:
        raise HTTPException(400, '先写一句技能要求，再点增强提示词')
    from .services.llm import provider_config
    if not provider_config(''):
        raise HTTPException(503, '模型尚未配置，暂时无法增强提示词')
    name = (payload.name or '').strip()
    job_id = await jobs_service.create(user_id, 'skill_enhance', {'name': name[:30]})
    jobs_service.spawn(job_id, lambda jid: enhance_skill_worker(jid, text, name))
    return {'job_id': job_id}


async def build_skill_worker(job_id: str, *, payload: SkillBuildRequest, form: dict, slug: str, instruction: str, user_id: str) -> dict:
    """后台跑一轮 skill-creator：思考 → 写文件 → 自检 → 安装技能包。

    过程阶段写进任务进度，成品写进任务结果，客户端轮询 /api/jobs/{id}。
    """
    await jobs_service.set_progress(job_id, '正在准备制作技能…')
    try:
        async for event in stream_raw_prompt(
            skill_build.build_prompt(instruction, payload.name, payload.summary, slug),
            f'skillbuild-{slug}', settings.harness_model,
            thinking='deep', user_id=user_id,
            skills=[{
                'id': 'builtin-skill-creator',
                'harness': skill_build.BUILTIN_SKILL_SLUG,
                'prompt': '',
                'label': skill_build.BUILTIN_SKILL_LABEL,
            }],
        ):
            if event.get('kind') == 'trace':
                label = skill_build.stage_label(event.get('item') or {})
                if label:
                    await jobs_service.set_progress(job_id, label)
        await jobs_service.set_progress(job_id, '正在安装技能包…')
        source = skill_build.workspace_build_dir(user_id, slug)
        degraded = not (source / skill_build.SKILL_FILE).is_file()
        if degraded:
            # 模型没写出技能包（抖动/超时）：用用户填的内容合成一份结构合规的 SKILL.md，
            # 不让用户白填一遍表单。前端会拿到 degraded=true，可以提示重新生成。
            skill_build.synthesize_package(user_id, slug, payload.name, payload.summary, instruction)
        info = skill_build.install_package(user_id, slug, source)
        meta = info.get('meta') or {}
        # frontmatter 的 name 现在固定是技能 slug（kebab-case 英文，运行时靠它加载），
        # 展示名只能用用户填的 / 表单里的中文名，不能拿 frontmatter 的 name 兜底。
        name = (form['name'] or '').strip()[:SKILL_NAME_MAX] or '我的技能'
        summary = (form['summary'] or (meta.get('description') or '').strip())[:80]
        body = skill_build.read_package_text(user_id, slug)
        skill_id = f"sk-{uuid.uuid4().hex[:12]}"
        stamp = now()
        store = await connect()
        try:
            await store.execute(
                "INSERT INTO skills(id,user_id,name,summary,prompt,icon,developer_wechat,harness,visibility,source,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'private','custom','active',?,?)",
                (skill_id, user_id, name, summary, body, form['icon'], form['developer_wechat'], slug, stamp, stamp),
            )
            await store.commit()
            row = await fetchone(store, 'SELECT * FROM skills WHERE id=?', (skill_id,))
        finally:
            await store.close()
        # 安装完就把工作区里的构建残留删掉，避免同一份技能留两份文件
        skill_build.remove_build_dir(user_id, slug)
        card = skill_payload(row, user_id)
        card['files'] = skill_build.package_files(user_id, slug)
        card['package'] = True
        card['degraded'] = degraded
        card['note'] = '技能包已生成' if not degraded else '模型没写完技能包，已按你填写的内容生成基础技能'
        return {'skill': card, 'note': card['note'], 'degraded': degraded}
    except Exception:
        # 失败不落库：技能包与构建残留一起清掉，不留半成品文件
        skill_build.remove_package(user_id, slug)
        raise


@app.post('/api/skills/build')
async def build_skill(payload: SkillBuildRequest, user_id: str = Depends(current_user)) -> dict:
    """新建技能：让 harness 加载 skill-creator 生成并安装技能包。

    生成技能要跑完整一轮 agent（思考 → 写文件 → 自检），耗时以分钟计，远超
    云托管单次调用 15s 上限，所以只登记任务：进度与成品都用 /api/jobs/{id} 轮询。
    """
    rate_limit(f"skill-build:{user_id}", 6, 900, '制作技能过于频繁，请稍后再试')
    instruction = (payload.instruction or '').strip()
    if len(instruction) < 4:
        raise HTTPException(400, '请把技能要求写清楚一些')
    if not harness_configured():
        raise HTTPException(503, '技能制作依赖 Harness 运行时，当前未启用')
    db = await connect()
    try:
        account = await fetchone(db, 'SELECT * FROM users WHERE id=?', (user_id,))
        limits = limits_for_user(account)
        used = await owned_skill_count(db, user_id)
    finally:
        await db.close()
    if used >= limits['skills']:
        raise HTTPException(400, f"当前会员可创建 {limits['skills']} 个技能，已用完。升级会员可以创建更多。")

    form = clean_skill_form(SkillForm(
        name=(payload.name or '').strip() or '新技能',
        summary=(payload.summary or '').strip(),
        prompt=instruction,
        developer_wechat=(payload.developer_wechat or '').strip(),
        icon=payload.icon,
    ))
    slug = skill_build.new_slug()
    job_id = await jobs_service.create(user_id, 'skill_build', {'slug': slug, 'name': form['name']})
    jobs_service.spawn(job_id, lambda jid: build_skill_worker(jid, payload=payload, form=form, slug=slug, instruction=instruction, user_id=user_id))
    return {'job_id': job_id}
