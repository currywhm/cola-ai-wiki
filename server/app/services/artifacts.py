"""工具产物回流：把 harness agent 本轮在工作区里生成的文件收成「对话产物」。

官方 ``dsh-attachment-local`` 只承接「用户带进对话的附件」（内容寻址在
``<DSH_HOME>/attachments/v1``），工具产出的文件不会自己出现在对话里。这里补齐这一段：

- 沙箱固定 ``workspace-write``、工作根由 ``DSH_WORKSPACE_ROOT`` 按租户注入，所以
  agent 用 write / edit / bash 写出来的文件一定落在该租户的工作区里；
  后端按 mtime 差量把它们收上来（见 ``harness._ArtifactWatch``）。
- 收回来的文件复制进 ``uploads/artifacts/<租户>/`` 做内容寻址保存（同一份字节只存一份），
  落 ``artifacts`` 表，随消息一起持久化（``messages.artifacts_json``），
  对话里可以预览、可以保存到微信。
- 知识库问答里生成的文件还会同步登记进知识库（``documents`` + 分块 + FTS），
  这样下一次检索就能引用到它；纯对话（执行规划通道）只在对话里给出文件。

权限与配额：产物只能被它的所有者读取（所有接口都按 ``user_id`` 收窄）；
登记进知识库时沿用与上传完全相同的空间校验，空间不够时只在对话里提供文件，不写知识库。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from ..db import connect, fetchone

# 单文件上限：够放报告 / 表格 / 演示稿，又不至于让一次问答把租户空间撑爆
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
# 一轮问答最多收几个产物：防止模型刷文件把对话刷屏
MAX_ARTIFACTS_PER_TURN = 8
# 在线预览的正文上限：超出只提示保存到微信再看
MAX_PREVIEW_TEXT = 240 * 1024

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
# 微信内置文档渲染器能 1:1 打开的类型（与前端 previewDocument 的白名单一致）
OFFICE_SUFFIXES = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
# 站内可直接看正文的类型（Markdown / 纯文本 / 代码 / 结构化文本）
TEXT_SUFFIXES = {
    '.md', '.markdown', '.txt', '.csv', '.tsv', '.json', '.html', '.htm', '.xml',
    '.yaml', '.yml', '.toml', '.ini', '.conf', '.sh', '.py', '.js', '.ts', '.sql',
    '.java', '.go', '.rs', '.c', '.h', '.cpp', '.css', '.vue',
}

# 文件名只保留安全字形：产物名会进 Content-Disposition 与磁盘路径，必须防路径穿越
_UNSAFE_NAME = re.compile(r'[^\w\u4e00-\u9fff.-]+')


def safe_name(name: str) -> str:
    cleaned = _UNSAFE_NAME.sub('_', Path(str(name or '')).name).strip('._')
    return (cleaned[:96] or 'artifact')


def now() -> str:
    """与全站一致的时间口径（UTC ISO 串），避免为一个小函数反向依赖 main。"""
    return datetime.now(timezone.utc).isoformat()


def suffix_of(name: str) -> str:
    return Path(str(name or '')).suffix.lower()


def artifact_kind(suffix: str) -> str:
    """产物类型：image=图片、document=可交给微信渲染的文档、text=站内可读正文、file=其它。"""
    value = str(suffix or '').lower()
    if value in IMAGE_SUFFIXES:
        return 'image'
    if value in OFFICE_SUFFIXES:
        return 'document'
    if value in TEXT_SUFFIXES:
        return 'text'
    return 'file'


def human_size(value: int) -> str:
    size = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


def _digest(path: Path) -> str:
    sha = hashlib.sha1()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def view(row) -> dict:
    """给前端的产物视图：只暴露展示与下载需要的东西，不暴露服务端路径。"""
    name = str(row['filename'])
    suffix = str(row['file_type'] or suffix_of(name))
    return {
        'id': str(row['id']),
        'name': name,
        'type': suffix.lstrip('.'),
        'suffix': suffix,
        'kind': artifact_kind(suffix),
        'size': int(row['file_size'] or 0),
        'size_label': human_size(int(row['file_size'] or 0)),
        'mime': str(row['mime'] or mimetypes.guess_type(name)[0] or 'application/octet-stream'),
        'document_id': str(row['document_id'] or ''),
        'saved': bool(str(row['document_id'] or '')),
        'note': str(row['note'] or ''),
        'created_at': str(row['created_at'] or ''),
        'url': f'/api/artifacts/{row["id"]}/download',
    }


def decode(value) -> list[dict]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or '[]'))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _read_text(data: bytes, limit: int = MAX_PREVIEW_TEXT) -> tuple[str, bool]:
    """读文本类产物的正文；解码异常时返回空串。"""
    text = data.decode('utf-8', errors='ignore')
    return (text[:limit], True) if len(text) <= limit else (text[:limit], False)


async def register(
    *,
    user_id: str,
    source: Path,
    conversation_id: str = '',
    knowledge_id: str = '',
    folder_id: str = '',
    save_to_knowledge: bool = False,
    storage_room: int | None = None,
) -> dict | None:
    """把一个工作区文件收成对话产物；不合适（空文件 / 过大 / 已消失）时返回 None。

    ``save_to_knowledge`` 为真时同步登记进知识库：产物字节只存一份（documents 与
    artifacts 指向同一个文件），并写入分块与 FTS，让后续检索能引用到它。
    """
    try:
        if not source.is_file():
            return None
        size = source.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        return None
    name = safe_name(source.name)
    suffix = suffix_of(name)
    try:
        digest = _digest(source)
    except OSError:
        return None
    tenant = re.sub(r'[^0-9a-zA-Z_-]', '', str(user_id or ''))[:48] or 'anonymous'
    key = f'artifacts/{tenant}/{digest[:12]}_{name}'
    storage_ref = storage.reference(key)
    try:
        if not await storage.exists(storage_ref):
            storage_ref = await storage.save_file(source, key)
    except storage.StorageError:
        return None
    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    artifact_id = uuid.uuid4().hex
    timestamp = now()
    db = await connect()
    try:
        document_id, note = await _save_to_knowledge(
            db, user_id=user_id, knowledge_id=knowledge_id, folder_id=folder_id,
            path=source, storage_ref=storage_ref, name=name, suffix=suffix, size=size,
            enabled=bool(save_to_knowledge), storage_room=storage_room,
        )
        await db.execute(
            "INSERT INTO artifacts(id,user_id,conversation_id,knowledge_id,folder_id,filename,file_type,file_size,mime,"
            "digest,storage_path,source_path,document_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact_id, user_id, conversation_id, knowledge_id, folder_id, name, suffix, size, mime,
                digest, storage_ref, str(source), document_id, note, timestamp,
            ),
        )
        await db.commit()
        row = await fetchone(db, "SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    finally:
        await db.close()
    return view(row) if row else None


async def _save_to_knowledge(
    db,
    *,
    user_id: str,
    knowledge_id: str,
    folder_id: str,
    path: Path,
    storage_ref: str,
    name: str,
    suffix: str,
    size: int,
    enabled: bool,
    storage_room: int | None,
) -> tuple[str, str]:
    """把产物登记成知识库文件（分块 + FTS + 计数）。返回 (document_id, 提示文案)。"""
    if not enabled or not knowledge_id:
        return '', ''
    if storage_room is not None and size > storage_room:
        return '', '知识库空间不足，本次只在对话里保留文件'
    from .documents import ALLOWED_SUFFIXES, extract_text, split_chunks

    document_id = uuid.uuid4().hex
    timestamp = now()
    await db.execute(
        "INSERT INTO documents(id,knowledge_id,user_id,filename,file_type,file_size,storage_path,status,progress,folder_id,"
        "organize_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (document_id, knowledge_id, user_id, name, suffix, size, storage_ref, 'processing', 15, folder_id, 'pending', timestamp, timestamp),
    )
    text = ''
    try:
        if suffix in ALLOWED_SUFFIXES:
            text, _pages = extract_text(path, suffix)
        elif artifact_kind(suffix) == 'text':
            # 生成文件常见 .md/.json/.py 之类的结构化文本：上传白名单管不到，但正文要能被检索
            text, _ok = _read_text(await storage.read_bytes(storage_ref))
    except Exception as exc:  # 解析失败只影响检索，不影响文件本身的预览与下载
        print(f'[artifact] 解析失败 {name}: {exc}', flush=True)
        text = ''
    try:
        chunks = split_chunks(text) if text.strip() else []
        for index, content in enumerate(chunks):
            chunk_id = uuid.uuid4().hex
            await db.execute(
                "INSERT INTO chunks(id,document_id,knowledge_id,content,page_number,chunk_index,created_at) VALUES(?,?,?,?,?,?,?)",
                (chunk_id, document_id, knowledge_id, content, 1, index, timestamp),
            )
            await db.execute(
                "INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) "
                "VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)",
                (content, chunk_id, knowledge_id, name, 1),
            )
        await db.execute(
            "UPDATE documents SET status='completed',progress=100,extracted_text=?,page_count=1,organize_status=?,updated_at=? WHERE id=?",
            (text, 'processing' if text.strip() else 'pending', now(), document_id),
        )
        await db.execute(
            "UPDATE knowledge_bases SET document_count=(SELECT COUNT(*) FROM documents WHERE knowledge_id=? AND status!='deleted'),updated_at=? WHERE id=?",
            (knowledge_id, now(), knowledge_id),
        )
    except Exception as exc:
        print(f'[artifact] 入库失败 {name}: {exc}', flush=True)
        await db.execute("UPDATE documents SET status='failed',error_message=?,organize_status='pending',updated_at=? WHERE id=?", (str(exc), now(), document_id))
        return '', '已生成文件，但登记到知识库失败'
    return document_id, '已存入知识库' if text.strip() else '已存入知识库（无可检索正文）'


async def owned(db, artifact_id: str, user_id: str):
    return await fetchone(
        db, "SELECT * FROM artifacts WHERE id=? AND user_id=?",
        (str(artifact_id or ''), user_id),
    )


def stored_ref(row) -> str:
    return str(row['storage_path'] or '')


def preview_text(data: bytes) -> tuple[str, bool]:
    """文本类产物的正文（截断标记一并返回），供站内预览页使用。"""
    return _read_text(data)
