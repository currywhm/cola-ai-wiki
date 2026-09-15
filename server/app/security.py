from collections import deque
from datetime import datetime, timedelta, timezone
import time

import jwt
from fastapi import Header, HTTPException
from .config import settings
from .db import connect, fetchone


def create_token(user_id: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "tv": token_version, "iat": now, "exp": now + timedelta(days=30)}, settings.jwt_secret, algorithm="HS256")


async def current_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        payload = jwt.decode(authorization[7:], settings.jwt_secret, algorithms=["HS256"])
        user_id = str(payload['sub'])
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(status_code=401, detail="登录已过期")
    db = await connect()
    try:
        user = await fetchone(db, "SELECT id,token_version FROM users WHERE id=? AND status='active'", (user_id,))
    finally:
        await db.close()
    if not user:
        raise HTTPException(status_code=401, detail='账号不存在或已注销，请重新登录')
    if int(payload.get('tv', 0)) != int(user['token_version'] or 0):
        raise HTTPException(status_code=401, detail='登录状态已失效，请重新登录')
    return user_id


# ---- 进程内滑动窗口限流 ----

_rate_buckets: dict[str, deque] = {}


def rate_limit(key: str, limit: int, window_seconds: int, message: str) -> None:
    """固定 key 在 window_seconds 内最多 limit 次，超限抛 429。"""
    now = time.monotonic()
    bucket = _rate_buckets.setdefault(key, deque())
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail=message)
    bucket.append(now)
