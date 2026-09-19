"""后台任务台账。

小程序走微信云托管私有协议后，单次调用有 15s 上限，凡是可能更久的动作
（LLM 长调用、大批量合规扫描、多文件正文抽取、harness 造技能）都必须挪到
请求之外执行，客户端拿 job_id 轮询结果。

约定与 chat_runs 一致：状态落库、进程重启把 running 标成 interrupted，
客户端据此提示重试，而不是无限转圈。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..db import connect, fetchone

QUEUED = 'queued'
RUNNING = 'running'
DONE = 'done'
ERROR = 'error'
INTERRUPTED = 'interrupted'
TERMINAL = {DONE, ERROR, INTERRUPTED}

_logger = logging.getLogger('app.jobs')
# 保住任务引用：事件循环只持弱引用，丢了会被中途回收
_running: set[asyncio.Task] = set()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}


def view(row: Any) -> dict:
    """给前端的任务视图：只暴露进度与结果，不暴露内部 payload。"""
    if not row:
        return {}
    return {
        'id': str(row['id']),
        'kind': str(row['kind']),
        'status': str(row['status']),
        'progress': str(row['progress'] or ''),
        'result': _loads(row['result_json']),
        'error': str(row['error'] or ''),
        'created_at': str(row['created_at'] or ''),
        'updated_at': str(row['updated_at'] or ''),
    }


async def create(user_id: str, kind: str, payload: dict | None = None) -> str:
    db = await connect()
    try:
        job_id = uuid.uuid4().hex
        stamp = now()
        await db.execute(
            "INSERT INTO jobs(id,user_id,kind,status,progress,payload_json,result_json,error,created_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, user_id, kind, QUEUED, '', json.dumps(payload or {}, ensure_ascii=False), '{}', '', stamp, stamp, ''),
        )
        await db.commit()
    finally:
        await db.close()
    return job_id


async def load(job_id: str) -> Any:
    db = await connect()
    try:
        return await fetchone(db, "SELECT * FROM jobs WHERE id=?", (job_id,))
    finally:
        await db.close()


async def update(job_id: str, *, status: str = '', progress: str = '', result: dict | None = None, error: str = '', finished: bool = False) -> None:
    columns = ['updated_at=?']
    values: list[Any] = [now()]
    if status:
        columns.append('status=?')
        values.append(status)
    if progress:
        columns.append('progress=?')
        values.append(progress[:240])
    if result is not None:
        columns.append('result_json=?')
        values.append(json.dumps(result, ensure_ascii=False))
    if error:
        columns.append('error=?')
        values.append(str(error)[:600])
    if finished:
        columns.append('finished_at=?')
        values.append(now())
    values.append(job_id)
    db = await connect()
    try:
        await db.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE id=?", tuple(values))
        await db.commit()
    finally:
        await db.close()


async def set_progress(job_id: str, label: str) -> None:
    """过程中的一句话进度：前端轮询时直接展示，不需要另开流。"""
    await update(job_id, status=RUNNING, progress=label)


def spawn(job_id: str, worker: Callable[[str], Awaitable[dict]]) -> None:
    """把任务挂到当前事件循环上执行；worker 用 set_progress 汇报、返回结果字典。"""
    async def runner() -> None:
        await update(job_id, status=RUNNING)
        try:
            result = await worker(job_id)
            await update(job_id, status=DONE, result=result or {}, finished=True)
        except asyncio.CancelledError:
            await update(job_id, status=INTERRUPTED, error='任务已中断', finished=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不能拖垮进程
            _logger.exception('job %s failed', job_id)
            await update(job_id, status=ERROR, error=str(exc), finished=True)

    task = asyncio.get_event_loop().create_task(runner())
    _running.add(task)
    task.add_done_callback(_running.discard)


async def mark_interrupted() -> None:
    """进程重启：上一轮还在跑的任务不可能继续写回，标成中断让前端提示重试。"""
    stamp = now()
    db = await connect()
    try:
        await db.execute(
            "UPDATE jobs SET status=?,error='服务重启，任务已中断',updated_at=?,finished_at=? WHERE status IN (?,?)",
            (INTERRUPTED, stamp, stamp, QUEUED, RUNNING),
        )
        await db.commit()
    finally:
        await db.close()
