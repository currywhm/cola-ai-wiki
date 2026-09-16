import asyncio
import hashlib
import json
import math
import mimetypes
import re
import secrets
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
from .config import settings
from .db import connect, decode_sources, fetchall, fetchone, init_db, row_dict
from .schemas import ArticleImportRequest, ChatRequest, ConversationPinUpdate, DocumentMove, DocumentTagUpdate, FolderCreate, KnowledgeCreate, LoginRequest, PayCreateRequest, PlanReviewRequest, PreferenceUpdate, ProfileUpdate, ShareCreate, SkillBuildRequest, SkillEnhanceRequest, SkillFlagUpdate, SkillForm, SkillPublishUpdate
from .security import create_token, current_user, rate_limit
from .services.documents import ALLOWED_SUFFIXES, extract_text, split_chunks
from .services import content as content_service
from .services import skill_build
from .services import artifacts as artifact_service
from .services.wechat_article import ArticleFetchError, build_document_html, fetch_wechat_article
from .services.llm import stream_answer
from .services.memory import load_history, maybe_compress, recent_context
from .services.harness import (
    SKILL_LABELS,
    configured as harness_configured,
    pending_plan_review,
    stream_raw_prompt,
    submit_plan_review,
)
from .services.organizer import organize_document
from .services.virtual_pay import calc_pay_sig, calc_user_signature, query_order, sign_data, virtual_configured, virtual_product
from .services.web_search import WebSearchError, search_web, web_context

# 免费试用：注册当天起 30 天倒计时，期间 300MB / 1 个资料库。
# 会员按租期开通（月/季/年）：Plus 10GB / Pro 30GB，另外区别在资料库数量、单文件大小与每月问答额度。
TRIAL_DAYS = 30
FREE_STORAGE_BYTES = 300 * 1024 * 1024
PLUS_STORAGE_BYTES = 10 * 1024 * 1024 * 1024
PRO_STORAGE_BYTES = 30 * 1024 * 1024 * 1024
# 试用结束后免费账号仍可提问，但额度收紧，避免注册即弃用。
FREE_QUESTIONS_AFTER_TRIAL = 50

MEMBERSHIP_LIMITS = {
    'free': {'label': '免费试用', 'knowledge_bases': 1, 'storage_bytes': FREE_STORAGE_BYTES, 'monthly_questions': 200, 'max_file_bytes': 50 * 1024 * 1024, 'skills': 1},
    'plus': {'label': 'Plus 会员', 'knowledge_bases': 10, 'storage_bytes': PLUS_STORAGE_BYTES, 'monthly_questions': 1000, 'max_file_bytes': 100 * 1024 * 1024, 'skills': 5},
    'pro': {'label': 'Pro 会员', 'knowledge_bases': 50, 'storage_bytes': PRO_STORAGE_BYTES, 'monthly_questions': 5000, 'max_file_bytes': 300 * 1024 * 1024, 'skills': 10},
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


def trial_state(row: Any) -> dict:
    """新用户免费试用倒计时：注册起 TRIAL_DAYS 天，会员不参与倒计时。"""
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


async def questions_this_month(db, user_id: str) -> int:
    """本月已提问题数（自然月，UTC），用于会员/试用的月度问答额度。"""
    period_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = await fetchone(db, "SELECT COUNT(*) AS used FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.role='user' AND m.created_at>=?", (user_id, period_start))
    return int(row['used'] or 0) if row else 0


async def owned_skill_count(db, user_id: str) -> int:
    """自己创建（不含内置）的技能数量，用于会员技能配额。"""
    row = await fetchone(db, "SELECT COUNT(*) AS used FROM skills WHERE user_id=? AND status='active' AND source!='builtin'", (user_id,))
    return int(row['used'] or 0) if row else 0


def account_state(row: Any, questions_used: int = 0, skills_used: int = 0) -> dict:
    """会员 + 试用倒计时 + 月度问答额度 + 技能配额合成一份状态，前后端共用同一套口径。"""
    tier = membership_for_user(row)
    limits = MEMBERSHIP_LIMITS[tier]
    trial = trial_state(row)
    trial_active = tier == 'free' and trial['active']
    quota = limits['monthly_questions'] if (tier != 'free' or trial_active) else FREE_QUESTIONS_AFTER_TRIAL
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
        'limits': {'knowledge_bases': limits['knowledge_bases'], 'storage_bytes': limits['storage_bytes'], 'max_file_bytes': limits['max_file_bytes'], 'monthly_questions': quota, 'skills': limits['skills']},
        'quota': {'questions_limit': quota, 'questions_used': questions_used, 'questions_left': max(0, quota - questions_used), 'skills_limit': limits['skills'], 'skills_used': skills_used, 'skills_left': max(0, limits['skills'] - skills_used)},
        'entitlements': {
            'can_upload': tier != 'free' or trial_active,
            'can_create_knowledge': tier != 'free' or trial_active,
            'can_ask': questions_used < quota,
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
    await init_db()
    # Backfill the default workspace for accounts created before this rule was
    # introduced. The helper is idempotent, so restarts never create duplicates.
    db = await connect()
    users = await fetchall(db, "SELECT id FROM users WHERE status='active'")
    for user in users:
        await ensure_default_knowledge(db, user["id"])
    await db.commit()
    await db.close()
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
        # 每轮问答新建 harness 会话（记忆由 DB 承载），启动时清扫 24h 前的过期会话目录
        from .services.harness import cleanup_stale_sessions
        removed = cleanup_stale_sessions()
        if removed:
            print(f'[harness] 已清理 {removed} 个过期会话目录', flush=True)
        # 预热 Harness 运行时：运行时是唯一单例（完整 sdk profile），后台热身一次即可，
        # 避免用户第一个问题承担冷启动成本。后台执行，不阻塞服务就绪。
        async def prewarm():
            from .services.harness import stream_answer as harness_warm
            try:
                async for _ in harness_warm('热身：请只回复「就绪」两个字。', '', f'prewarm-{uuid.uuid4().hex}', '', 'deep'):
                    pass
            except Exception as exc:
                print(f'[harness] prewarm failed: {exc}', flush=True)
        asyncio.create_task(prewarm())
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


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list or ["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


PAID_MARKERS = {'PAID', 'SUCCESS', 'PAY_SUCCESS', '2'}


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
        "FROM chunks c JOIN documents d ON d.id=c.document_id JOIN knowledge_bases k ON k.id=c.knowledge_id WHERE " + ' AND '.join(where) + ") WHERE hits>=? "
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
    return {"ok": True, "service": settings.app_name, "harness": {"enabled": settings.harness_enabled, "profile": settings.harness_profile if settings.harness_enabled else None, "provider": settings.harness_provider if settings.harness_enabled else None, "model": settings.harness_model if settings.harness_enabled else None, "runtime_mode": settings.harness_runtime_mode if settings.harness_enabled else None, "strict": settings.harness_strict if settings.harness_enabled else None}}


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
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get("https://api.weixin.qq.com/sns/jscode2session", params={"appid": settings.wechat_appid, "secret": settings.wechat_secret, "js_code": payload.code, "grant_type": "authorization_code"})
        data = response.json()
    if data.get("errcode") or not data.get("openid"):
        raise HTTPException(401, "微信登录校验失败")
    openid = data["openid"]
    db = await connect()
    row = await fetchone(db, "SELECT * FROM users WHERE openid = ?", (openid,))
    user_id = row["id"] if row else uuid.uuid4().hex
    timestamp = now()
    session_key = data.get("session_key", "")
    if row:
        await db.execute("UPDATE users SET session_key=?,updated_at=? WHERE id=?", (session_key, timestamp, user_id))
    else:
        await db.execute("INSERT INTO users(id,openid,nickname,avatar,session_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, openid, "微信用户", "", session_key, timestamp, timestamp))
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
    knowledge_usage = await fetchone(db, "SELECT COUNT(*) AS knowledge_bases FROM knowledge_bases WHERE user_id=? AND status='active'", (user_id,))
    document_usage = await fetchone(db, "SELECT COUNT(*) AS documents, COALESCE(SUM(file_size),0) AS storage_bytes FROM documents WHERE user_id=? AND status!='deleted'", (user_id,))
    questions_used = await questions_this_month(db, user_id)
    skills_used = await owned_skill_count(db, user_id)
    await db.close()
    if not row: raise HTTPException(404, "用户不存在")
    state = account_state(row, questions_used, skills_used)
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
    await db.execute("UPDATE users SET nickname=?, avatar=?, updated_at=? WHERE id=? AND status='active'", (payload.nickname.strip(), payload.avatar.strip(), now(), user_id))
    await db.commit()
    row = await fetchone(db, "SELECT id,nickname,avatar FROM users WHERE id=? AND status='active'", (user_id,))
    await db.close()
    if not row: raise HTTPException(404, "用户不存在")
    return row_dict(row)


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
    db = await connect()
    files = await fetchall(db, "SELECT storage_path FROM documents WHERE user_id=?", (user_id,))
    await db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.user_id=?)", (user_id,))
    await db.execute("DELETE FROM users WHERE id=?", (user_id,))
    await db.commit(); await db.close()
    for row in files:
        try: settings.resolve_path(row["storage_path"]).unlink(missing_ok=True)
        except OSError: pass
    return {"ok": True}


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
            'knowledge_bases': limits['knowledge_bases'], 'monthly_questions': limits['monthly_questions'],
            'max_file_label': human_size(limits['max_file_bytes']), 'plans': plans,
        })
    free = MEMBERSHIP_LIMITS['free']
    return {
        'trial_days': TRIAL_DAYS,
        'free': {
            'label': free['label'], 'storage_label': human_size(free['storage_bytes']), 'knowledge_bases': free['knowledge_bases'],
            'monthly_questions': free['monthly_questions'], 'monthly_questions_after_trial': FREE_QUESTIONS_AFTER_TRIAL,
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
    body = sign_data(offer_id=settings.wechat_virtual_offer_id, quantity=1, env=settings.wechat_virtual_env, product_id=product_id, goods_price=goods_price, out_trade_no=out_trade_no, attach=attach)
    pay_data = {'mode': 'short_series_goods', 'signData': body, 'paySig': calc_pay_sig('requestVirtualPayment', body), 'signature': calc_user_signature(body, user['session_key'])}
    try:
        db = await connect()
        await db.execute("INSERT INTO pay_orders(id,user_id,out_trade_no,plan,amount,status,offer_id,product_id,attach,quantity,deliver_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, user_id, out_trade_no, payload.plan, goods_price, "pending", settings.wechat_virtual_offer_id, product_id, attach, 1, "pending", now()))
        await db.commit(); await db.close()
    except Exception:
        try: await db.close()
        except Exception: pass
        raise HTTPException(500, "创建支付订单失败，请重试")
    return {"out_trade_no": out_trade_no, "plan": payload.plan, "amount": amount, "payData": pay_data}


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


@app.get("/api/knowledge")
async def list_knowledge(user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    await db.commit()
    rows = await fetchall(db, "SELECT * FROM knowledge_bases WHERE user_id=? AND status='active' ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, updated_at DESC", (user_id, DEFAULT_KNOWLEDGE_NAME))
    await db.close()
    return [row_dict(r) for r in rows]


@app.get("/api/market")
async def market_knowledge(query: str = Query(default="", max_length=80), category: str = Query(default="", max_length=40)) -> list[dict]:
    """Return only explicitly published knowledge bases for discovery."""
    db = await connect()
    clauses = ["k.visibility='public'", "k.status='active'"]
    params: list[str] = []
    if query.strip():
        clauses.append("(k.name LIKE ? OR k.description LIKE ?)")
        term = f"%{query.strip()}%"
        params.extend([term, term])
    if category.strip():
        clauses.append("k.category=?")
        params.append(category.strip())
    rows = await fetchall(db, f"SELECT k.id,k.name,k.description,k.icon,k.category,k.subscribers,k.document_count,k.updated_at FROM knowledge_bases k WHERE {' AND '.join(clauses)} ORDER BY k.subscribers DESC,k.updated_at DESC LIMIT 50", tuple(params))
    await db.close()
    return [{**(row_dict(row) or {}), 'documents': int(row['document_count'] or 0), 'subscribers': int(row['subscribers'] or 0)} for row in rows]


@app.post("/api/knowledge")
async def create_knowledge(payload: KnowledgeCreate, user_id: str = Depends(current_user)) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "资料库名称不能为空")
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    count = await fetchone(db, "SELECT COUNT(*) AS count FROM knowledge_bases WHERE user_id=? AND status='active'", (user_id,))
    limits = limits_for_user(user)
    if not account_state(user, 0)['entitlements']['can_create_knowledge']:
        await db.close(); raise HTTPException(403, "免费试用已结束，开通会员后可继续新建资料库")
    if int(count['count']) >= limits['knowledge_bases']:
        await db.close(); raise HTTPException(403, f"{limits['label']}最多创建 {limits['knowledge_bases']} 个资料库，请升级会员")
    if name == DEFAULT_KNOWLEDGE_NAME:
        await db.close()
        raise HTTPException(409, "默认资料库已存在")
    item = (uuid.uuid4().hex, user_id, name, payload.description.strip(), payload.icon, 0, "active", now(), now())
    await db.execute("INSERT INTO knowledge_bases(id,user_id,name,description,icon,document_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", item); await db.commit(); await db.close()
    return {"id": item[0], "user_id": user_id, "name": name, "description": item[3], "icon": payload.icon, "document_count": 0, "status": "active", "created_at": item[7], "updated_at": item[8]}


@app.delete("/api/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id)); user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    if not kb:
        await db.close(); raise HTTPException(404, "资料库不存在")
    files = await fetchall(db, "SELECT storage_path FROM documents WHERE knowledge_id=? AND user_id=?", (knowledge_id, user_id))
    await db.execute("DELETE FROM chunks_fts WHERE knowledge_id=?", (knowledge_id,))
    await db.execute('DELETE FROM conversations WHERE knowledge_id=? AND user_id=?', (knowledge_id, user_id))
    await db.execute("DELETE FROM knowledge_bases WHERE id=? AND user_id=?", (knowledge_id, user_id)); await db.commit(); await db.close()
    for row in files:
        try: settings.resolve_path(row["storage_path"]).unlink(missing_ok=True)
        except OSError: pass
    return {"ok": True}


@app.get("/api/knowledge/{knowledge_id}")
async def knowledge_detail(knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); kb = await fetchone(db, "SELECT * FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id));
    if not kb: await db.close(); raise HTTPException(404, "知识库不存在")
    docs = await fetchall(db, "SELECT id,filename,file_type,file_size,page_count,status,progress,error_message,organized_title,summary,tags_json,key_points_json,organize_status,organize_method,organized_at,folder_id,created_at,updated_at FROM documents WHERE knowledge_id=? AND status!='deleted' ORDER BY created_at DESC", (knowledge_id,)); await db.close()
    return {"knowledge": row_dict(kb), "documents": [document_view(x) for x in docs]}


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


@app.get("/api/knowledge/{knowledge_id}/suggestions")
async def knowledge_suggestions(knowledge_id: str, folder_id: str = "", user_id: str = Depends(current_user)) -> dict:
    """推荐问题：LLM 按文件清单生成一次，按 数量+最新更新时间 指纹缓存，资料变化自动失效。

    传 folder_id 时范围收窄到该文件夹（文件夹会话就该问这个文件夹里的资料）；
    没有任何已完成资料时直接返回空列表，前端不展示无意义的提问提示。
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
    questions = await _generate_suggestions(scope_name, [dict(r) for r in rows], '文件夹' if folder else '知识库')
    if not questions:
        questions = list(SUGGESTION_FALLBACK)
    payload = json.dumps(questions, ensure_ascii=False)
    db = await connect()
    if folder:
        await db.execute("INSERT INTO folder_suggestions(knowledge_id,folder_id,fingerprint,questions_json,created_at) VALUES(?,?,?,?,?) ON CONFLICT(knowledge_id,folder_id) DO UPDATE SET fingerprint=excluded.fingerprint,questions_json=excluded.questions_json,created_at=excluded.created_at", (knowledge_id, folder_id, fingerprint, payload, now()))
    else:
        await db.execute("INSERT INTO knowledge_suggestions(knowledge_id,fingerprint,questions_json,created_at) VALUES(?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET fingerprint=excluded.fingerprint,questions_json=excluded.questions_json,created_at=excluded.created_at", (knowledge_id, fingerprint, payload, now()))
    await db.commit(); await db.close()
    return {"questions": questions, "scope": "folder" if folder else "knowledge"}


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
    folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=?", (folder_id, user_id))
    if not folder: await db.close(); raise HTTPException(404, "文件夹不存在")
    await db.execute("UPDATE documents SET folder_id='',updated_at=? WHERE folder_id=?", (now(), folder_id))
    await db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    await db.commit(); await db.close()
    return {"ok": True}


@app.patch("/api/documents/{document_id}/move")
async def move_document(document_id: str, payload: DocumentMove, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    doc = await fetchone(db, "SELECT id,knowledge_id FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not doc: await db.close(); raise HTTPException(404, "文档不存在")
    if payload.folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (payload.folder_id, user_id, doc["knowledge_id"]))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
    await db.execute("UPDATE documents SET folder_id=?,updated_at=? WHERE id=?", (payload.folder_id, now(), document_id))
    await db.commit(); await db.close()
    return {"ok": True}


@app.post("/api/knowledge/{knowledge_id}/documents")
async def upload_document(background_tasks: BackgroundTasks, knowledge_id: str, file: UploadFile = File(...), folder_id: str = Form(default=""), x_upload_filename: str | None = Header(default=None), user_id: str = Depends(current_user)) -> dict:
    db = await connect(); kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id)); user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    if not kb: await db.close(); raise HTTPException(404, "知识库不存在")
    if folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (folder_id, user_id, knowledge_id))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
    limits = limits_for_user(user)
    if not account_state(user, 0)['entitlements']['can_upload']:
        await db.close(); raise HTTPException(403, "免费试用已结束，开通会员后可继续添加资料")
    client_name = unquote(x_upload_filename) if x_upload_filename else file.filename
    safe_name = Path(client_name or "upload").name; document_id = uuid.uuid4().hex; destination = settings.upload_path / f"{document_id}_{safe_name}"
    suffix = destination.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        await db.close(); raise HTTPException(415, "暂不支持该文件类型")
    data = await file.read()
    if len(data) > limits['max_file_bytes']:
        await db.close(); raise HTTPException(413, f"单文件不能超过 {human_size(limits['max_file_bytes'])}")
    used = await fetchone(db, "SELECT COALESCE(SUM(file_size),0) AS bytes FROM documents WHERE user_id=? AND status!='deleted'", (user_id,))
    if int(used['bytes']) + len(data) > limits['storage_bytes']:
        await db.close(); raise HTTPException(413, f"{limits['label']}可用空间 {human_size(limits['storage_bytes'])} 已满，升级会员可继续添加")
    destination.write_bytes(data); timestamp = now()
    await db.execute("INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, knowledge_id, user_id, safe_name, suffix, len(data), str(destination), "processing", 15, folder_id, timestamp, timestamp)); await db.commit(); await db.close()
    status = "completed"
    error_message = ""
    extracted = ""
    try:
        text, pages = extract_text(destination, suffix); chunks = split_chunks(text)
        extracted = text
        db = await connect(); await db.execute("UPDATE documents SET page_count=?,status='embedding',progress=70,extracted_text=?,updated_at=? WHERE id=?", (pages, text, now(), document_id))
        for index, content in enumerate(chunks):
            chunk_id = uuid.uuid4().hex; await db.execute("INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)", (chunk_id, document_id, knowledge_id, content, min(pages, index + 1), index, now())); await db.execute("INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)", (content, chunk_id, knowledge_id, safe_name, min(pages, index + 1)))
        await db.execute("UPDATE documents SET status='completed',progress=100,updated_at=? WHERE id=?", (now(), document_id)); await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (knowledge_id, now(), knowledge_id)); await db.commit(); await db.close()
    except Exception as exc:
        status = "failed"; error_message = str(exc)
        db = await connect(); await db.execute("UPDATE documents SET status='failed',error_message=?,updated_at=? WHERE id=?", (error_message, now(), document_id)); await db.commit(); await db.close()
    # 没有可抽取正文（旧版 Office 格式、无文字图片）时不排整理任务，避免落一条空摘要；
    # 这类文件仍可正常打开原文预览。
    if status == 'completed' and extracted.strip() and background_tasks is not None:
        background_tasks.add_task(organize_document, document_id)
    # organize_status 按实际有没有排整理任务回报：旧版 Office 格式正文为空，不会排整理，
    # 报 processing 会让调用方一直等一个永远不会到来的摘要。
    organized = status == 'completed' and bool(extracted.strip())
    return {"id": document_id, "filename": safe_name, "status": status, "progress": 100 if status == "completed" else 0, "error_message": error_message, "organize_status": 'processing' if organized else 'pending'}


@app.post("/api/knowledge/{knowledge_id}/import-article")
async def import_article(payload: ArticleImportRequest, background_tasks: BackgroundTasks, knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    """导入微信公众号文章：抓取正文（含图片），存为 HTML 文档并切片入库。"""
    rate_limit(f"import:{user_id}", 10, 60, "导入太频繁了，请稍后再试")
    db = await connect()
    kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    user = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    limits = limits_for_user(user)
    if not kb:
        await db.close(); raise HTTPException(404, "知识库不存在")
    if payload.folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (payload.folder_id, user_id, knowledge_id))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
    if not account_state(user, 0)['entitlements']['can_upload']:
        await db.close(); raise HTTPException(403, "免费试用已结束，开通会员后可继续添加资料")
    document_id = uuid.uuid4().hex
    assets_dir = settings.upload_path / "article_assets" / document_id
    try:
        article = await fetch_wechat_article(payload.url, assets_dir, f"/api/article-assets/{document_id}")
    except ArticleFetchError as exc:
        await db.close(); raise HTTPException(422, str(exc)) from exc
    full_html = build_document_html(article["title"], article["account"], article["html"], payload.url.strip())
    safe_name = f"{article['title'].replace('/', '_')[:60]}.html"
    destination = settings.upload_path / f"{document_id}_{safe_name}"
    destination.write_text(full_html, encoding="utf-8")
    file_size = destination.stat().st_size + sum(f.stat().st_size for f in assets_dir.glob("*") if f.is_file())
    used = await fetchone(db, "SELECT COALESCE(SUM(file_size),0) AS bytes FROM documents WHERE user_id=? AND status!='deleted'", (user_id,))
    if int(used['bytes']) + file_size > limits['storage_bytes']:
        await db.close(); raise HTTPException(413, f"{limits['label']}可用空间 {human_size(limits['storage_bytes'])} 已满，升级会员可继续添加")
    timestamp = now()
    await db.execute("INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, knowledge_id, user_id, safe_name, ".html", file_size, str(destination), "processing", 15, payload.folder_id, timestamp, timestamp))
    await db.commit(); await db.close()
    status = "completed"; error_message = ""
    try:
        chunks = split_chunks(article["text"])
        db = await connect()
        await db.execute("UPDATE documents SET page_count=1,status='embedding',progress=70,extracted_text=?,updated_at=? WHERE id=?", (article["text"], now(), document_id))
        for index, content in enumerate(chunks):
            chunk_id = uuid.uuid4().hex
            await db.execute("INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)", (chunk_id, document_id, knowledge_id, content, 1, index, now()))
            await db.execute("INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)", (content, chunk_id, knowledge_id, safe_name, 1))
        await db.execute("UPDATE documents SET status='completed',progress=100,updated_at=? WHERE id=?", (now(), document_id))
        await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (knowledge_id, now(), knowledge_id))
        await db.commit(); await db.close()
    except Exception as exc:
        status = "failed"; error_message = str(exc)
        db = await connect(); await db.execute("UPDATE documents SET status='failed',error_message=?,updated_at=? WHERE id=?", (error_message, now(), document_id)); await db.commit(); await db.close()
    if status == 'completed' and background_tasks is not None:
        background_tasks.add_task(organize_document, document_id)
    return {"id": document_id, "filename": safe_name, "title": article["title"], "account": article["account"], "image_count": article["image_count"], "status": status, "progress": 100 if status == "completed" else 0, "error_message": error_message, "organize_status": 'processing' if status == 'completed' else 'pending'}


@app.get("/api/article-assets/{document_id}/{filename}")
async def article_asset(document_id: str, filename: str) -> FileResponse:
    """文章图片资源。文档 ID 为随机不可枚举串，资源不含私密信息，无需登录态。"""
    safe = Path(filename).name
    if not document_id.isalnum() or safe != filename:
        raise HTTPException(404, "资源不存在")
    path = settings.upload_path / "article_assets" / document_id / safe
    if not path.exists():
        raise HTTPException(404, "资源不存在")
    media_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


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
    db = await connect(); row = await fetchone(db, "SELECT * FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not row:
        await db.close(); raise HTTPException(404, "文档不存在")
    path = settings.resolve_path(row["storage_path"]); suffix = row["file_type"]
    if not path.exists():
        await db.close(); raise HTTPException(404, "原文文件不存在，无法重新解析")
    await db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)", (document_id,))
    await db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
    await db.execute("UPDATE documents SET status='processing',progress=15,error_message='',updated_at=? WHERE id=?", (now(), document_id)); await db.commit(); await db.close()
    try:
        text, pages = extract_text(path, suffix); chunks = split_chunks(text)
        db = await connect(); await db.execute("UPDATE documents SET page_count=?,status='embedding',progress=70,extracted_text=?,updated_at=? WHERE id=?", (pages, text, now(), document_id))
        for index, content in enumerate(chunks):
            chunk_id = uuid.uuid4().hex; page = min(pages, index + 1)
            await db.execute("INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)", (chunk_id, document_id, row["knowledge_id"], content, page, index, now()))
            await db.execute("INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)", (content, chunk_id, row["knowledge_id"], row["filename"], page))
        await db.execute("UPDATE documents SET status='completed',progress=100,updated_at=? WHERE id=?", (now(), document_id)); await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (row["knowledge_id"], now(), row["knowledge_id"])); await db.commit(); await db.close()
        if text.strip():
            background_tasks.add_task(organize_document, document_id)
        return {"id": document_id, "status": "completed", "progress": 100, "organize_status": "processing"}
    except Exception as exc:
        db = await connect(); await db.execute("UPDATE documents SET status='failed',progress=0,error_message=?,updated_at=? WHERE id=?", (str(exc), now(), document_id)); await db.commit(); await db.close()
        return {"id": document_id, "status": "failed", "progress": 0, "error_message": str(exc)}


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); row = await fetchone(db, "SELECT storage_path,knowledge_id FROM documents WHERE id=? AND user_id=?", (document_id, user_id));
    if row:
        await db.execute('DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)', (document_id,))
        await db.execute('DELETE FROM chunks WHERE document_id=?', (document_id,))
        await db.execute("UPDATE documents SET status='deleted',updated_at=? WHERE id=?", (now(), document_id)); await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (row["knowledge_id"], now(), row["knowledge_id"])); await db.commit()
    await db.close()
    if row:
        try: settings.resolve_path(row["storage_path"]).unlink(missing_ok=True)
        except OSError: pass
    return {"ok": True}


@app.get("/api/documents/{document_id}/download")
async def download_document(document_id: str, user_id: str = Depends(current_user)) -> FileResponse:
    db = await connect(); row = await fetchone(db, "SELECT storage_path,filename FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id)); await db.close()
    if not row or not settings.resolve_path(row["storage_path"]).exists(): raise HTTPException(404, "文档不存在")
    return FileResponse(settings.resolve_path(row["storage_path"]), filename=row["filename"], media_type=mimetypes.guess_type(row["filename"])[0] or "application/octet-stream")


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, user_id: str = Depends(current_user)) -> FileResponse:
    """下载 / 转发工具产物：产物按用户收窄，别人的 id 拿不到文件。"""
    db = await connect(); row = await artifact_service.owned(db, artifact_id, user_id); await db.close()
    if not row:
        raise HTTPException(404, "文件不存在")
    path = artifact_service.stored_path(row)
    if not path.is_file():
        raise HTTPException(404, "文件已不在服务器上")
    return FileResponse(path, filename=str(row["filename"]), media_type=str(row["mime"] or "application/octet-stream"))


@app.get("/api/artifacts/{artifact_id}/preview")
async def preview_artifact(artifact_id: str, user_id: str = Depends(current_user)) -> dict:
    """站内预览：文本类产物直接给正文（Markdown / 纯文本 / 代码）；
    图片与文档类交前端走微信原生预览（图片预览 / 内置文档渲染器）。"""
    db = await connect(); row = await artifact_service.owned(db, artifact_id, user_id); await db.close()
    if not row:
        raise HTTPException(404, "文件不存在")
    view = artifact_service.view(row)
    if not artifact_service.stored_path(row).is_file():
        raise HTTPException(404, "文件已不在服务器上")
    if view["kind"] == "text":
        text, complete = artifact_service.preview_text(artifact_service.stored_path(row))
        view["text"] = text
        view["truncated"] = not complete
    return view


@app.get("/api/documents/{document_id}")
async def document_detail(document_id: str, user_id: str = Depends(current_user)) -> dict:
    db = await connect(); row = await fetchone(db, "SELECT id,filename,file_type,file_size,page_count,status,progress,error_message,extracted_text,organized_title,summary,tags_json,key_points_json,organize_status,organize_method,organize_error,organized_at,created_at,updated_at,storage_path FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id)); await db.close()
    if not row: raise HTTPException(404, "文档不存在")
    view = document_view(row)
    if row["file_type"] == ".html":
        try:
            view["content_html"] = settings.resolve_path(row["storage_path"]).read_text(encoding="utf-8", errors="ignore")
        except OSError:
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
    row = await fetchone(db, "SELECT id FROM documents WHERE id=? AND user_id=? AND status!='deleted'", (document_id, user_id))
    if not row:
        await db.close()
        raise HTTPException(404, "文档不存在")
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
async def recent_overview(limit: int = Query(default=30, ge=1, le=100), user_id: str = Depends(current_user)) -> dict:
    """「最近」页数据源：最近更新的资料库 + 最近动过的文档/文件夹。"""
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    await db.commit()
    kbs = await fetchall(db, "SELECT id,name,description,icon,document_count,updated_at,created_at FROM knowledge_bases WHERE user_id=? AND status='active' ORDER BY updated_at DESC LIMIT 8", (user_id,))
    docs = await fetchall(db, "SELECT id,knowledge_id,filename,file_type,file_size,status,created_at,updated_at FROM documents WHERE user_id=? AND status!='deleted' ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    folders = await fetchall(db, "SELECT id,knowledge_id,name,created_at,updated_at FROM folders WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
    await db.close()
    items = [
        {
            'id': row['id'], 'kind': 'document', 'name': row['filename'], 'file_type': row['file_type'] or '',
            'file_size': int(row['file_size'] or 0), 'status': row['status'] or 'uploaded',
            'knowledge_id': row['knowledge_id'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
        }
        for row in docs
    ] + [
        {
            'id': row['id'], 'kind': 'folder', 'name': row['name'], 'file_type': '',
            'file_size': 0, 'status': 'folder',
            'knowledge_id': row['knowledge_id'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
        }
        for row in folders
    ]
    # 文档与文件夹混合排序，按最近更新时间统一呈现
    items.sort(key=lambda item: item['updated_at'] or '', reverse=True)
    return {'knowledge': [row_dict(row) for row in kbs], 'items': items[:limit]}


async def make_sources(db, knowledge_id: str, query: str, user_id: str, folder_id: str = '') -> list[dict]:
    # 范围语义（与产品入口一致）：
    #   从知识库首页进入的对话（folder_id 为空）= 整个知识库，根目录和各文件夹里的资料都算数；
    #   从某个文件夹进入的对话 = 只检索该文件夹，不外溢到根目录或其它文件夹。
    rows = await retrieve_chunks(db, query, user_id, knowledge_id, folder_id or None, limit=5)
    return [{"id": r["id"], "document_id": r["document_id"], "filename": r["filename"], "page_number": r["page_number"], "quote": r["content"][:180], "score": r["score"]} for r in rows]


async def remaining_storage(db, user_id: str, limits: dict) -> int:
    """该用户当前还剩多少可用空间（每次登记产物都重算，一轮内多个产物不会超配额）。"""
    row = await fetchone(db, "SELECT COALESCE(SUM(file_size),0) AS bytes FROM documents WHERE user_id=? AND status!='deleted'", (user_id,))
    return max(0, int(limits['storage_bytes']) - int((row['bytes'] if row else 0) or 0))


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(current_user)) -> StreamingResponse:
    rate_limit(f"chat:{user_id}", 15, 60, "提问太频繁啦，喝口水休息一下再试")
    db = await connect()
    account = await fetchone(db, "SELECT * FROM users WHERE id=?", (user_id,))
    state = account_state(account, await questions_this_month(db, user_id))
    limits = limits_for_user(account)
    if not state['entitlements']['can_ask']:
        limit = state['quota']['questions_limit']
        await db.close()
        raise HTTPException(429, f"本月 {limit} 次问答额度已用完，开通会员可继续提问")
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
    # 执行规划模式：走 harness agent 自带的 web_search / web_fetch 工具链（DeepSeek 原生
    # 搜索，复用 DEEPSEEK_API_KEY，不需要额外的 EXA_API_KEY）——真·联网检索 + 任务规划。
    # harness 未启用时才回退到直连搜索/纯模型通道。
    web_agent = payload.mode == 'web' and harness_configured()
    if web_agent:
        # Harness 对齐：问全网由 agent 自带的 web_search/web_fetch 工具完成，绕过不可用的 duckduckgo 直连
        sources = []
        web_results = []
    elif payload.mode == "web":
        if settings.web_search_api_key:
            try:
                web_results = await search_web(payload.content)
            except WebSearchError:
                # 直连搜索不可用时不再 503，降级为模型通用回答（提示词中明确说明，不编造来源）
                web_results = []
        else:
            web_results = []
        sources = [
            {
                "id": item["id"],
                "filename": item["title"],
                "url": item["url"],
                "page_number": 0,
                "quote": item["snippet"],
                "score": round(max(0.78, 0.98 - index * 0.05), 2),
            }
            for index, item in enumerate(web_results)
        ]
    else:
        sources = await make_sources(db, knowledge_id, payload.content, user_id, folder_id)
    conversation_id = payload.conversation_id or uuid.uuid4().hex
    memory_summary = ''
    if payload.conversation_id:
        # 归属校验即隔离边界：会话必须同时属于当前用户、当前知识库、当前文件夹（根目录 ''），
        # 记忆摘要/历史都挂在会话上，用户之间、知识库之间、文件夹之间互不可见
        owner = await fetchone(db, 'SELECT id,memory_summary,folder_id FROM conversations WHERE id=? AND user_id=? AND knowledge_id=?', (conversation_id, user_id, knowledge_id))
        if not owner or (owner['folder_id'] or '') != folder_id:
            await db.close(); raise HTTPException(404, '对话不存在')
        memory_summary = owner['memory_summary'] or ''
    if not payload.conversation_id:
        await db.execute("INSERT INTO conversations(id,user_id,knowledge_id,folder_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (conversation_id, user_id, knowledge_id, folder_id, payload.content[:32], now(), now()))
    await db.execute("INSERT INTO messages(id,conversation_id,role,content,sources_json,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, conversation_id, "user", payload.content, "[]", now())); await db.commit()
    if web_agent:
        system_content = (
            "你是 cola 的全网检索助手，不是泛用闲聊机器人。请使用 web_search 工具检索公开网页、"
            "必要时用 web_fetch 读取页面后再回答；优先给出结论，再说明依据；不得编造未检索到的事实；"
            "使用中文，并保留 [1] 这样的来源标记。对时效性、医疗、法律、金融内容要明确提示用户核验原始页面。"
        )
    elif payload.mode == "web":
        if web_results:
            context = web_context(web_results)
            system_content = (
                "你是 cola 的全网检索助手，不是泛用闲聊机器人。只能依据后端提供的公开网页摘要回答，"
                "不得假设自己访问了网页全文，不得编造未出现在摘要中的事实。优先给出结论，再说明依据；"
                "使用中文，并保留 [1] 这样的来源标记。对时效性、医疗、法律、金融内容要明确提示用户核验原始页面。\n"
                f"本次全网检索结果：\n{context}"
            )
        else:
            system_content = (
                "你是 cola 的智能助手。请基于模型已有知识直接回答用户问题；"
                "优先给出结论，再作必要说明；使用中文，回答简洁清楚。"
                "不要编造网页来源，不要使用 [1] 这类引用标记。"
                "对话中用户主动提供的信息（称呼、偏好、需求）可以直接记住，"
                "并在后续回答中自然使用，无需声明无法保存。"
            )
    else:
        # 已整理知识按同一套范围语义取：整库范围不看文件夹，文件夹范围只看该文件夹。
        organized_sql = ("SELECT filename,organized_title,summary,tags_json,key_points_json FROM documents WHERE knowledge_id=? AND status='completed' AND organize_status='completed'"
                         + (" AND folder_id=?" if folder_id else "") + " ORDER BY organized_at DESC LIMIT 20")
        organized = await fetchall(db, organized_sql, (knowledge_id, folder_id) if folder_id else (knowledge_id,))
        wiki_context = "\n\n".join(f"《{r['organized_title'] or r['filename']}》\n摘要：{r['summary']}\n要点：{'；'.join(json_list(r['key_points_json']))}" for r in organized)
        context = "\n\n".join(f"[{i+1}] {s['filename']} 第{s['page_number']}页\n{s['quote']}" for i, s in enumerate(sources))
        scope_note = (f"当前问答范围限定在文件夹「{folder_name}」内：只能依据该文件夹中的文件回答，不得引用本知识库其它文件夹或根目录的文件。" if folder_id else "当前问答范围是整个知识库：根目录和各文件夹中的文件都可以作为依据。")
        scope_label = f"文件夹「{folder_name}」" if folder_id else "资料库"
        if not wiki_context and not context:
            system_content = (
                f"你是 cola 知识库的资料助手。当前{scope_label}暂无可用文件内容，"
                "因此本轮可以直接根据通用模型能力回答用户问题。不要提到“全网问答”、"
                "“切换模式”或“没有文件所以切换”，也不要编造资料库引用。使用中文，回答要简洁、清楚。"
                "对话中用户主动提供的信息（称呼、偏好、需求）可以直接记住，"
                "并在后续回答中自然使用，无需声明无法保存。"
                f"\n资料库：{kb['name']}\n{scope_note}"
            )
        else:
            system_content = f"你是 cola 知识库的资料助手，不是泛用聊天机器人。回答必须围绕当前资料库，先参考已整理的知识条目，再核对原文片段；资料不足时明确说明，不得编造。优先给出直接结论，再给出依据和必要的补充，并保留 [1] 这样的引用标记。使用中文，排版清晰。对话中用户主动提供的信息（称呼、偏好、需求）可以直接记住，并在后续回答中自然使用，无需声明无法保存。\n资料库：{kb['name']}\n{scope_note}\n已整理知识：{wiki_context or '暂无整理条目'}\n原文片段：{context or '暂无匹配原文'}"
    # 上下文记忆（参考 harness compaction）：记忆摘要（压缩态）+ 最近 N 条原文。
    # 历史在本轮用户消息落库后读取，因此末条即当前问题
    history = await load_history(db, conversation_id)
    messages = [{"role": "system", "content": system_content}]
    if memory_summary:
        messages.append({"role": "system", "content": f"以下是本会话此前的记忆摘要（供保持上下文连贯，不要在回答中复述它）：\n{memory_summary}"})
    messages += recent_context(history)
    # 本轮技能：只有用户明确选择的技能才会注入——内置技能走技能包，我的技能走技能指令。
    # 别人的私有技能在这里解析为空，保证「我的技能」严格隔离。
    # 本轮技能（可多选）：payload.skills 优先，为空时兼容旧的单选 skill 字段。
    # 只有用户明确选择的技能才会注入；别人的私有技能在这里解析为空，保证「我的技能」严格隔离。
    requested_skills = [item for item in (payload.skills or []) if item] or ([payload.skill] if payload.skill else [])
    turn_skills = await resolve_turn_skills(db, user_id, requested_skills)
    for item in turn_skills:
        await db.execute('UPDATE skills SET use_count=COALESCE(use_count,0)+1 WHERE id=?', (item['id'],))
    if turn_skills:
        await db.commit()
    async def events():
        answer = ""
        # 过程区随正文一起落库：思考文字单独累积（增量太多，不入过程节点列表），
        # 步骤 / 工具 / 技能 / 子智能体 / 提示 按原顺序留存，重进对话时才能复原。
        started_at = time.monotonic()
        trace_items: list[dict] = []
        reason_parts: list[str] = []
        # 工具产物：agent 本轮生成的文件（报告 / 表格 / 演示稿 / 图 …）随消息落库。
        # 知识库问答里的产物同时登记进知识库，纯对话（执行规划）只在对话里给出文件。
        produced_artifacts: list[dict] = []
        save_artifacts_to_kb = bool(state['entitlements']['can_upload']) and payload.mode != 'web'
        yield f"data: {json.dumps({'type':'meta','conversation_id':conversation_id,'sources':sources}, ensure_ascii=False)}\n\n"
        try:
            # harness 模型统一取后端配置（深度思考即 deepseek-flash，见 HARNESS_MODEL）
            turn_model = settings.harness_model if harness_configured() else payload.model
            # 技能已在进入流之前解析好：内置技能 = 技能包 slug，我的技能 = 技能指令文本
            # 计划模式：执行规划通道打开官方 plan mode（计划先评审、批准后再执行）。
            # 请求显式带 plan 时以请求为准，便于前端按入口切换。
            turn_plan = (
                payload.plan
                if payload.plan is not None
                else (settings.harness_plan_mode and payload.mode == 'web')
            )
            async for event in stream_answer(
                messages,
                turn_model,
                conversation_id,
                thinking=payload.thinking,
                skills=turn_skills,
                mode='planner' if payload.mode == 'web' else 'knowledge',
                user_id=user_id,
                plan=bool(turn_plan),
            ):
                kind = event.get('kind')
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
                        yield f"data: {json.dumps({'type':'trace', **item}, ensure_ascii=False)}\n\n"
                    continue
                if kind == 'artifact':
                    # 工具产物回流：把运行时工作区里新生成的文件收成可预览/下载的产物。
                    # 收不动（空文件 / 过大 / 已消失 / 超出一轮上限）时静默跳过，不影响回答正文。
                    discovery = event.get('artifact') or {}
                    record = await artifact_service.register(
                        user_id=user_id,
                        source=Path(str(discovery.get('path') or '')),
                        conversation_id=conversation_id,
                        knowledge_id=knowledge_id,
                        folder_id=folder_id,
                        save_to_knowledge=save_artifacts_to_kb,
                        storage_room=await remaining_storage(db, user_id, limits),
                    )
                    if not record:
                        continue
                    produced_artifacts.append(record)
                    # 生成物入库后照旧做一次整理（与上传同一条链路）：摘要 / 标签 / 要点
                    if record.get('document_id') and background_tasks is not None:
                        background_tasks.add_task(organize_document, record['document_id'])
                    yield f"data: {json.dumps({'type':'artifact','artifact':record}, ensure_ascii=False)}\n\n"
                    continue
                piece = event['text']
                answer += piece; yield f"data: {json.dumps({'type':'delta','content':piece}, ensure_ascii=False)}\n\n"
            if not answer.strip():
                # 网关断流等情况导致零产出：不发 done（否则前端落一个空气泡），
                # 发 error 让前端提示重试
                yield f"data: {json.dumps({'type':'error','message':'网络波动，本次回答未完成，请重新发送。'}, ensure_ascii=False)}\n\n"
                return
            await db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,sources_json,trace_json,reason,duration_ms,artifacts_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex, conversation_id, "assistant", answer,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(trace_items, ensure_ascii=False),
                    "".join(reason_parts).strip(),
                    int((time.monotonic() - started_at) * 1000),
                    json.dumps(produced_artifacts, ensure_ascii=False),
                    now(),
                ),
            ); await db.commit()
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conversation_id)); await db.commit()
            yield f"data: {json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
            # 滚动压缩旧轮次（done 已发出，压缩不阻塞正文流；失败下轮重试）
            try:
                await maybe_compress(db, conversation_id, payload.model)
            except Exception as exc:
                print(f'[memory] 压缩失败：{exc}', flush=True)
        except Exception as exc:
            yield f"data: {json.dumps({'type':'error','message':str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            await db.close()
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


@app.post("/api/chat/plan-review")
async def plan_review(payload: PlanReviewRequest, user_id: str = Depends(current_user)) -> dict:
    """计划评审：把用户在计划卡片上的结论回写给运行时。

    计划卡片出现时，运行时的 ``exit_plan_mode`` 正阻塞等待评审通道；这里把结论写入
    该租户的计划桥目录，运行时读到即继续（批准=退出计划模式并开始执行；
    未批准=带上反馈重新规划）。评审已超时/不存在时返回 accepted=false，
    前端提示用户重新提问，绝不假装已批准。
    """
    review_id = payload.review_id.strip()
    if pending_plan_review(user_id, review_id) is None:
        return {"accepted": False, "reason": "expired"}
    accepted = submit_plan_review(user_id, review_id, payload.approved, payload.feedback)
    return {"accepted": accepted, "reason": "" if accepted else "expired"}


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


@app.get("/api/conversations/{conversation_id}")
async def conversation_detail(conversation_id: str, user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect(); rows = await fetchall(db, "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.conversation_id=? AND c.user_id=? ORDER BY m.created_at", (conversation_id, user_id)); await db.close()
    # 一并回放过程区（思考 / 步骤 / 工具 / 技能 / 子智能体）与耗时：不返回这些字段，
    # 前端重新进入对话时就只剩正文了。
    messages = []
    for row in rows:
        view = row_dict(row)
        try:
            trace = json.loads(view.get("trace_json") or "[]")
        except json.JSONDecodeError:
            trace = []
        messages.append({
            **view,
            "sources": decode_sources(row["sources_json"]),
            "trace": trace if isinstance(trace, list) else [],
            "reason": view.get("reason") or "",
            # 工具产物（生成的文件）与过程区一样回放：重进对话时文件卡还在，可以继续预览/下载
            "artifacts": [item for item in artifact_service.decode(view.get("artifacts_json")) if isinstance(item, dict)],
            "duration_ms": int(view.get("duration_ms") or 0),
        })
    return messages


@app.post('/api/shares')
async def create_share(payload: ShareCreate, user_id: str = Depends(current_user)) -> dict:
    """把一条回答落成分享卡片，供用户转发给微信好友。

    只保存问答正文与出处文件名，不保存分享者的昵称 / 头像 / openid：
    好友点开卡片看到的是内容本身，而不是分享者的账号信息。
    """
    rate_limit(f"share:{user_id}", 30, 60, '分享过于频繁，请稍后再试')
    share_id = secrets.token_urlsafe(9)
    sources = [{'filename': item.filename, 'page_number': item.page_number, 'url': item.url} for item in payload.sources if item.filename or item.url]
    db = await connect()
    await db.execute(
        'INSERT INTO share_cards(id,user_id,knowledge_name,question,answer,sources_json,views,created_at) VALUES(?,?,?,?,?,?,0,?)',
        (share_id, user_id, payload.knowledge_name, payload.question, payload.answer, json.dumps(sources, ensure_ascii=False), now()),
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


@app.get('/api/shares/{share_id}')
async def read_share(share_id: str, request: Request) -> dict:
    """公开只读：好友点开分享卡片时读取内容，不需要登录。

    返回体里没有任何用户身份字段，只回内容与出处。
    """
    if not valid_share_id(share_id):
        raise HTTPException(404, '分享内容不存在或已失效')
    rate_limit(f"share-read:{request.client.host if request.client else 'unknown'}", 120, 60, '访问过于频繁，请稍后再试')
    db = await connect()
    row = await fetchone(db, 'SELECT id,knowledge_name,question,answer,sources_json,views,created_at FROM share_cards WHERE id=?', (share_id,))
    if not row:
        await db.close(); raise HTTPException(404, '分享内容不存在或已失效')
    await db.execute('UPDATE share_cards SET views=COALESCE(views,0)+1 WHERE id=?', (share_id,))
    await db.commit(); await db.close()
    return {'id': row['id'], 'question': row['question'] or '', 'answer': row['answer'], 'knowledge_name': row['knowledge_name'] or '', 'sources': decode_sources(row['sources_json']), 'views': int(row['views'] or 0) + 1, 'created_at': row['created_at']}


# ---- 技能：技能广场（所有人可用）与我的技能（用户级隔离）----

# 与前端技能编辑页的可选图标一致；保留早期用过的图标名，避免旧技能在编辑时被改写
SKILL_ICONS = {'skill-node', 'knowledge-pick', 'book', 'ppt', 'image', 'sousuo', 'sliders', 'wangluo', 'atom', 'robot', 'liebiao', 'shuju', 'history', 'dui', 'dengpao', 'tag'}
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


@app.post('/api/skills/enhance')
async def enhance_skill_prompt(payload: SkillEnhanceRequest, user_id: str = Depends(current_user)) -> dict:
    """增强提示词：把随手写的一句话按技能模板改写成可执行的技能指令。

    走直连模型通道而不是 harness：这是一次纯文本改写，秒级返回即可，
    没必要为它付运行时冷启动的成本。
    """
    rate_limit(f"skill-enhance:{user_id}", 20, 600, '增强提示词过于频繁，请稍后再试')
    text = (payload.instruction or '').strip()
    if not text:
        raise HTTPException(400, '先写一句技能要求，再点增强提示词')
    from .services.llm import provider_config
    config = provider_config('')
    if not config:
        raise HTTPException(503, '模型尚未配置，暂时无法增强提示词')
    base_url, api_key, model = config
    root = base_url.rstrip('/')
    endpoint = f"{root}/chat/completions" if root.endswith('/v1') else f"{root}/v1/chat/completions"
    subject = f'技能名称：{payload.name.strip()}\n' if (payload.name or '').strip() else ''
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10)) as client:
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
        raise HTTPException(503, '增强提示词暂时不可用，请稍后重试') from exc
    if not enhanced:
        raise HTTPException(503, '增强提示词暂时不可用，请稍后重试')
    return {'instruction': enhanced[:4000]}


@app.post('/api/skills/build')
async def build_skill(payload: SkillBuildRequest, user_id: str = Depends(current_user)) -> StreamingResponse:
    """新建技能：让 harness 加载 skill-creator，按用户要求生成并安装技能包。

    生成技能要跑完整一轮 agent（思考 → 写文件 → 自检），耗时以十秒计，所以过程用
    SSE 推给前端：``stage``（当前在做什么）、``skill``（成品）、``error``。
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
    prompt = skill_build.build_prompt(instruction, payload.name, payload.summary, slug)
    session_id = f'skillbuild-{slug}'

    async def events():
        yield f"data: {json.dumps({'type':'stage','label':'正在准备制作技能…'}, ensure_ascii=False)}\n\n"
        try:
            async for event in stream_raw_prompt(
                prompt, session_id, settings.harness_model,
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
                        yield f"data: {json.dumps({'type':'stage','label':label}, ensure_ascii=False)}\n\n"
                    continue
            yield f"data: {json.dumps({'type':'stage','label':'正在安装技能包…'}, ensure_ascii=False)}\n\n"
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
            yield f"data: {json.dumps({'type':'skill','skill':card,'note':card['note'],'degraded':degraded}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            # 失败不落库：技能包与构建残留一起清掉，不留半成品文件
            skill_build.remove_package(user_id, slug)
            message = str(exc).strip() or '技能制作失败，请重试'
            yield f"data: {json.dumps({'type':'error','message':message}, ensure_ascii=False)}\n\n"
        return

    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
