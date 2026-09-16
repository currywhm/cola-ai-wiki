import asyncio
import hashlib
import json
import mimetypes
import secrets
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
from .schemas import ArticleImportRequest, ChatRequest, DocumentMove, DocumentTagUpdate, FolderCreate, KnowledgeCreate, LoginRequest, PayCreateRequest, ProfileUpdate
from .security import create_token, current_user, rate_limit
from .services.documents import extract_text, split_chunks
from .services.wechat_article import ArticleFetchError, build_document_html, fetch_wechat_article
from .services.llm import stream_answer
from .services.memory import load_history, maybe_compress, recent_context
from .services.harness import configured as harness_configured
from .services.organizer import organize_document
from .services.virtual_pay import calc_pay_sig, calc_user_signature, query_order, sign_data, virtual_configured, virtual_product
from .services.web_search import WebSearchError, search_web, web_context

MEMBERSHIP_LIMITS = {
    'free': {'label': '免费版', 'knowledge_bases': 1, 'storage_bytes': 300 * 1024 * 1024},
    'plus': {'label': 'Plus 会员', 'knowledge_bases': 5, 'storage_bytes': 3 * 1024 * 1024 * 1024},
    'pro': {'label': 'Pro 会员', 'knowledge_bases': 20, 'storage_bytes': 10 * 1024 * 1024 * 1024},
}

PLAN_CATALOG = {
    'plus_monthly': (690, 'Plus 会员月度', 'plus', 31), 'plus_quarterly': (1800, 'Plus 会员季度', 'plus', 92), 'plus_yearly': (6900, 'Plus 会员年度', 'plus', 365),
    'pro_monthly': (990, 'Pro 会员月度', 'pro', 31), 'pro_quarterly': (2500, 'Pro 会员季度', 'pro', 92), 'pro_yearly': (9900, 'Pro 会员年度', 'pro', 365),
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
    if harness_configured():
        # 每轮问答新建 harness 会话（记忆由 DB 承载），启动时清扫 24h 前的过期会话目录
        from .services.harness import cleanup_stale_sessions
        removed = cleanup_stale_sessions()
        if removed:
            print(f'[harness] 已清理 {removed} 个过期会话目录', flush=True)
        # 预热 Harness 运行时：快速（sdk-minimal）与深度（sdk）两个 profile 都后台热身，
        # 避免用户第一个问题承担冷启动成本。后台执行，不阻塞服务就绪。
        async def prewarm():
            from .services.harness import stream_answer as harness_warm
            for thinking in ('quick', 'deep'):
                try:
                    async for _ in harness_warm('热身：请只回复「就绪」两个字。', '', f'prewarm-{thinking}-{uuid.uuid4().hex}', '', thinking):
                        pass
                except Exception as exc:
                    print(f'[harness] prewarm({thinking}) failed: {exc}', flush=True)
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
    user = await fetchone(db, "SELECT membership,membership_expires_at FROM users WHERE id=?", (order['user_id'],))
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
    await db.close()
    if not row: raise HTTPException(404, "用户不存在")
    limits = limits_for_user(row)
    return {"id": row["id"], "nickname": row["nickname"], "avatar": row["avatar"], "membership": limits['tier'], "membership_label": limits['label'], "membership_expires_at": row['membership_expires_at'] or '', "limits": {"knowledge_bases": limits['knowledge_bases'], "storage_bytes": limits['storage_bytes']}, "usage": {"knowledge_bases": int(knowledge_usage['knowledge_bases'] or 0), "documents": int(document_usage['documents'] or 0), "storage_bytes": int(document_usage['storage_bytes'] or 0)}}


@app.patch("/api/me")
async def update_me(payload: ProfileUpdate, user_id: str = Depends(current_user)) -> dict:
    db = await connect()
    await db.execute("UPDATE users SET nickname=?, avatar=?, updated_at=? WHERE id=? AND status='active'", (payload.nickname.strip(), payload.avatar.strip(), now(), user_id))
    await db.commit()
    row = await fetchone(db, "SELECT id,nickname,avatar FROM users WHERE id=? AND status='active'", (user_id,))
    await db.close()
    if not row: raise HTTPException(404, "用户不存在")
    return row_dict(row)


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


@app.get("/api/recent")
async def recent(user_id: str = Depends(current_user)) -> dict:
    """最近使用：知识库按更新时间倒序，文件和文件夹合并后按时间倒序。"""
    db = await connect()
    await ensure_default_knowledge(db, user_id)
    await db.commit()
    kbs = await fetchall(db, "SELECT id,name,updated_at FROM knowledge_bases WHERE user_id=? AND status='active' ORDER BY updated_at DESC LIMIT 10", (user_id,))
    docs = await fetchall(db, "SELECT d.id,d.filename,d.file_type,d.folder_id,d.updated_at,d.created_at,d.knowledge_id,k.name AS knowledge_name FROM documents d JOIN knowledge_bases k ON k.id=d.knowledge_id WHERE d.user_id=? AND d.status!='deleted' AND k.status='active' ORDER BY d.updated_at DESC LIMIT 40", (user_id,))
    folders = await fetchall(db, "SELECT f.id,f.name,f.updated_at,f.knowledge_id,k.name AS knowledge_name FROM folders f JOIN knowledge_bases k ON k.id=f.knowledge_id WHERE f.user_id=? AND k.status='active' ORDER BY f.updated_at DESC LIMIT 20", (user_id,))
    await db.close()
    items = [{'id': str(r['id']), 'kind': 'document', 'name': str(r['filename'] or ''), 'file_type': str(r['file_type'] or ''), 'knowledge_id': str(r['knowledge_id']), 'knowledge_name': str(r['knowledge_name'] or ''), 'folder_id': str(r['folder_id'] or ''), 'updated_at': str(r['updated_at'] or r['created_at'] or '')} for r in docs]
    items += [{'id': str(r['id']), 'kind': 'folder', 'name': str(r['name'] or ''), 'file_type': 'folder', 'knowledge_id': str(r['knowledge_id']), 'knowledge_name': str(r['knowledge_name'] or ''), 'folder_id': '', 'updated_at': str(r['updated_at'] or '')} for r in folders]
    items.sort(key=lambda item: item['updated_at'], reverse=True)
    return {'knowledge': [row_dict(r) for r in kbs], 'items': items}


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
    user = await fetchone(db, "SELECT membership,membership_expires_at FROM users WHERE id=?", (user_id,))
    count = await fetchone(db, "SELECT COUNT(*) AS count FROM knowledge_bases WHERE user_id=? AND status='active'", (user_id,))
    limits = limits_for_user(user)
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
    db = await connect(); kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id)); user = await fetchone(db, "SELECT membership,membership_expires_at FROM users WHERE id=?", (user_id,))
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


async def _generate_suggestions(kb_name: str, samples: list[dict]) -> list[str]:
    """根据知识库名 + 文件清单用 LLM 猜用户最想问的问题（返回空列表表示失败）。"""
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
                    {"role": "system", "content": "你是知识库助手。根据给出的知识库名称与文件清单，猜测用户最想问的 4 个问题。只输出 JSON 数组（字符串元素），不要输出任何其他内容。每个问题不超过 30 个字，用中文。"},
                    {"role": "user", "content": f"知识库名称：{kb_name}\n文件清单：\n{listing}"},
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
async def knowledge_suggestions(knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    """提问页推荐问题：LLM 按文件清单生成一次，按 数量+最新更新时间 指纹缓存，文档变化自动失效。"""
    db = await connect()
    kb = await fetchone(db, "SELECT id,name FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    if not kb:
        await db.close(); raise HTTPException(404, "知识库不存在")
    stats = await fetchone(db, "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at),'') AS latest FROM documents WHERE knowledge_id=? AND status!='deleted'", (knowledge_id,))
    fingerprint = f"{stats['n']}:{stats['latest']}"
    cached = await fetchone(db, "SELECT questions_json FROM knowledge_suggestions WHERE knowledge_id=? AND fingerprint=?", (knowledge_id, fingerprint))
    if cached:
        try:
            questions = json.loads(cached['questions_json'])
        except json.JSONDecodeError:
            questions = []
        await db.close()
        return {"questions": questions}
    rows = await fetchall(db, "SELECT filename,summary FROM documents WHERE knowledge_id=? AND status='completed' ORDER BY updated_at DESC LIMIT 20", (knowledge_id,))
    await db.close()
    questions = await _generate_suggestions(kb['name'], [dict(r) for r in rows])
    if not questions:
        questions = list(SUGGESTION_FALLBACK)
    db = await connect()
    await db.execute("INSERT INTO knowledge_suggestions(knowledge_id,fingerprint,questions_json,created_at) VALUES(?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET fingerprint=excluded.fingerprint,questions_json=excluded.questions_json,created_at=excluded.created_at", (knowledge_id, fingerprint, json.dumps(questions, ensure_ascii=False), now()))
    await db.commit(); await db.close()
    return {"questions": questions}


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
    db = await connect(); kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id)); user = await fetchone(db, "SELECT membership,membership_expires_at FROM users WHERE id=?", (user_id,))
    if not kb: await db.close(); raise HTTPException(404, "知识库不存在")
    if folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (folder_id, user_id, knowledge_id))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
    client_name = unquote(x_upload_filename) if x_upload_filename else file.filename
    safe_name = Path(client_name or "upload").name; document_id = uuid.uuid4().hex; destination = settings.upload_path / f"{document_id}_{safe_name}"
    suffix = destination.suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md", ".markdown", ".csv", ".jpg", ".jpeg", ".png", ".webp"}:
        await db.close(); raise HTTPException(415, "暂不支持该文件类型")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        await db.close(); raise HTTPException(413, "单文件不能超过 50MB")
    used = await fetchone(db, "SELECT COALESCE(SUM(file_size),0) AS bytes FROM documents WHERE user_id=? AND status!='deleted'", (user_id,))
    limits = limits_for_user(user)
    if int(used['bytes']) + len(data) > limits['storage_bytes']:
        await db.close(); raise HTTPException(413, f"{limits['label']}存储空间为 {limits['storage_bytes'] // (1024 * 1024)}MB，当前空间不足，请升级会员")
    destination.write_bytes(data); timestamp = now()
    await db.execute("INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, knowledge_id, user_id, safe_name, suffix, len(data), str(destination), "processing", 15, folder_id, timestamp, timestamp)); await db.commit(); await db.close()
    status = "completed"
    error_message = ""
    try:
        text, pages = extract_text(destination, suffix); chunks = split_chunks(text)
        db = await connect(); await db.execute("UPDATE documents SET page_count=?,status='embedding',progress=70,extracted_text=?,updated_at=? WHERE id=?", (pages, text, now(), document_id))
        for index, content in enumerate(chunks):
            chunk_id = uuid.uuid4().hex; await db.execute("INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)", (chunk_id, document_id, knowledge_id, content, min(pages, index + 1), index, now())); await db.execute("INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)", (content, chunk_id, knowledge_id, safe_name, min(pages, index + 1)))
        await db.execute("UPDATE documents SET status='completed',progress=100,updated_at=? WHERE id=?", (now(), document_id)); await db.execute("UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?", (knowledge_id, now(), knowledge_id)); await db.commit(); await db.close()
    except Exception as exc:
        status = "failed"; error_message = str(exc)
        db = await connect(); await db.execute("UPDATE documents SET status='failed',error_message=?,updated_at=? WHERE id=?", (error_message, now(), document_id)); await db.commit(); await db.close()
    if status == 'completed' and background_tasks is not None:
        background_tasks.add_task(organize_document, document_id)
    return {"id": document_id, "filename": safe_name, "status": status, "progress": 100 if status == "completed" else 0, "error_message": error_message, "organize_status": 'processing' if status == 'completed' else 'pending'}


@app.post("/api/knowledge/{knowledge_id}/import-article")
async def import_article(payload: ArticleImportRequest, background_tasks: BackgroundTasks, knowledge_id: str, user_id: str = Depends(current_user)) -> dict:
    """导入微信公众号文章：抓取正文（含图片），存为 HTML 文档并切片入库。"""
    rate_limit(f"import:{user_id}", 10, 60, "导入太频繁了，请稍后再试")
    db = await connect()
    kb = await fetchone(db, "SELECT id FROM knowledge_bases WHERE id=? AND user_id=? AND status='active'", (knowledge_id, user_id))
    user = await fetchone(db, "SELECT membership,membership_expires_at FROM users WHERE id=?", (user_id,))
    if not kb:
        await db.close(); raise HTTPException(404, "知识库不存在")
    if payload.folder_id:
        folder = await fetchone(db, "SELECT id FROM folders WHERE id=? AND user_id=? AND knowledge_id=?", (payload.folder_id, user_id, knowledge_id))
        if not folder: await db.close(); raise HTTPException(404, "目标文件夹不存在")
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
    limits = limits_for_user(user)
    if int(used['bytes']) + file_size > limits['storage_bytes']:
        await db.close(); raise HTTPException(413, f"{limits['label']}存储空间为 {limits['storage_bytes'] // (1024 * 1024)}MB，当前空间不足，请升级会员")
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
    base_sql = "SELECT f.content,f.filename,f.page_number,f.chunk_id,c.document_id,k.name AS knowledge_name FROM chunks_fts f JOIN chunks c ON c.id=f.chunk_id JOIN documents d ON d.id=c.document_id JOIN knowledge_bases k ON k.id=f.knowledge_id WHERE k.user_id=? AND d.status!='deleted'"
    params: tuple = (user_id,)
    if knowledge_id:
        base_sql += " AND f.knowledge_id=?"
        params = (user_id, knowledge_id)
    db = await connect(); rows = await fetchall(db, base_sql + " AND f.content MATCH ? LIMIT 30", params + (fts_literal(q),)); await db.close()
    if not rows:
        db = await connect(); rows = await fetchall(db, base_sql + " AND f.content LIKE ? LIMIT 30", params + (f"%{q}%",)); await db.close()
    return [{"id": r["chunk_id"], "document_id": r["document_id"], "content": r["content"], "filename": r["filename"], "page_number": r["page_number"], "knowledge_name": r["knowledge_name"], "score": 0.9} for r in rows]


async def make_sources(db, knowledge_id: str, query: str, user_id: str, folder_id: str = '') -> list[dict]:
    # 文件夹隔离：传入 folder_id 时只检索该文件夹内文档；根目录问答只检索未入夹文档
    scope = "f.knowledge_id=? AND k.user_id=? AND d.status!='deleted' AND d.folder_id=?"
    params: tuple = (knowledge_id, user_id, folder_id)
    base = "SELECT f.content,f.filename,f.page_number,f.chunk_id,c.document_id FROM chunks_fts f JOIN chunks c ON c.id=f.chunk_id JOIN documents d ON d.id=c.document_id JOIN knowledge_bases k ON k.id=f.knowledge_id WHERE "
    rows = await fetchall(db, base + scope + " AND f.content MATCH ? LIMIT 5", params + (fts_literal(query),))
    if not rows: rows = await fetchall(db, base + scope + " AND f.content LIKE ? LIMIT 5", params + (f"%{query}%",))
    if not rows: rows = await fetchall(db, base + scope + " ORDER BY f.rowid DESC LIMIT 3", params)
    return [{"id": r["chunk_id"], "document_id": r["document_id"], "filename": r["filename"], "page_number": r["page_number"], "quote": r["content"][:180], "score": round(max(0.78, 0.98 - i * 0.05), 2)} for i, r in enumerate(rows)]


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest, user_id: str = Depends(current_user)) -> StreamingResponse:
    rate_limit(f"chat:{user_id}", 15, 60, "提问太频繁啦，喝口水休息一下再试")
    db = await connect()
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
    # 问全网：配置了 EXA_API_KEY 才走 harness agent 的 web_search 工具链（真·联网检索）；
    # 未配置时直接用模型回答——本机 duckduckgo 不可达，不再浪费 12s 超时等待，
    # 仅当显式配置了 tavily/brave 密钥（web_search_api_key）时才尝试直连搜索
    web_agent = payload.mode == 'web' and harness_configured() and bool(settings.exa_api_key)
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
        organized = await fetchall(db, "SELECT filename,organized_title,summary,tags_json,key_points_json FROM documents WHERE knowledge_id=? AND status='completed' AND organize_status='completed' AND folder_id=? ORDER BY organized_at DESC LIMIT 20", (knowledge_id, folder_id))
        wiki_context = "\n\n".join(f"《{r['organized_title'] or r['filename']}》\n摘要：{r['summary']}\n要点：{'；'.join(json_list(r['key_points_json']))}" for r in organized)
        context = "\n\n".join(f"[{i+1}] {s['filename']} 第{s['page_number']}页\n{s['quote']}" for i, s in enumerate(sources))
        scope_note = f"当前问答范围限定在文件夹「{folder_name}」内：只能依据该文件夹中的文件回答，不得引用本知识库其它文件夹或根目录的文件。" if folder_id else "当前问答范围限定在知识库根目录：只能依据未放入文件夹的文件回答，不得引用各文件夹内的文件。"
        if not wiki_context and not context:
            system_content = (
                "你是 cola 知识库的资料助手。当前资料库暂无可用文件内容，"
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
    async def events():
        answer = ""
        yield f"data: {json.dumps({'type':'meta','conversation_id':conversation_id,'sources':sources}, ensure_ascii=False)}\n\n"
        try:
            async for event in stream_answer(messages, payload.model, conversation_id, thinking='deep' if web_agent else payload.thinking):
                if event.get('kind') == 'progress':
                    yield f"data: {json.dumps({'type':'progress','label':event['text']}, ensure_ascii=False)}\n\n"
                    continue
                piece = event['text']
                answer += piece; yield f"data: {json.dumps({'type':'delta','content':piece}, ensure_ascii=False)}\n\n"
            if not answer.strip():
                # 网关断流等情况导致零产出：不发 done（否则前端落一个空气泡），
                # 发 error 让前端提示重试
                yield f"data: {json.dumps({'type':'error','message':'网络波动，本次回答未完成，请重新发送。'}, ensure_ascii=False)}\n\n"
                return
            await db.execute("INSERT INTO messages(id,conversation_id,role,content,sources_json,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, conversation_id, "assistant", answer, json.dumps(sources, ensure_ascii=False), now())); await db.commit()
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


@app.get("/api/conversations")
async def conversations(user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect(); rows = await fetchall(db, "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC", (user_id,)); await db.close(); return [row_dict(r) for r in rows]


@app.get("/api/conversations/{conversation_id}")
async def conversation_detail(conversation_id: str, user_id: str = Depends(current_user)) -> list[dict]:
    db = await connect(); rows = await fetchall(db, "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.conversation_id=? AND c.user_id=? ORDER BY m.created_at", (conversation_id, user_id)); await db.close(); return [{**row_dict(r), "sources": decode_sources(r["sources_json"])} for r in rows]
