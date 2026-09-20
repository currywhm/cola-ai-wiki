"""Database access for local SQLite and WeChat Cloud Run MySQL.

Business code keeps the existing ``connect()`` / ``execute()`` / ``fetchone()``
shape. SQLite uses ``aiosqlite`` directly; MySQL uses an ``aiomysql`` pool and
translates the small set of SQLite-specific statements used by this project.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiosqlite

from .config import settings

# 技能广场的内置技能：随包发行、所有人可用（owner 为空串即官方内置）。
# 只放「怎么做」的技能指令，具体要做什么仍由用户那一句话决定。
BUILTIN_SKILLS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ('builtin-organize-knowledge', '整理知识库', '把资料整理成结构化知识条目', 'skill-organize', 'organize-knowledge', '把当前知识库里的资料整理成结构化的知识条目：按主题归类，每条给出标题、要点和出处，合并重复内容，并在结尾列出仍然缺口的主题。'),
    ('builtin-write-report', '撰写报告', '基于资料输出一份调研报告', 'skill-report', 'write-report', '基于当前知识库的资料写一份调研报告：先给结论与关键数据，再展开背景、现状、对比与风险，最后给出建议，并保留资料出处标记。'),
    ('builtin-make-deck', '生成 PPT', '输出分页大纲与每页要点', 'skill-deck', 'make-deck', '基于当前知识库的资料输出一份 PPT 大纲：按页给出标题、每页要点与建议配图方向，整体 8-14 页，重点数据单独成页。'),
    ('builtin-knowledge-diagram', '知识图解', '把长文梳理成知识结构', 'skill-diagram', 'knowledge-diagram', '把当前知识库里的长文整理成一份知识图解：按主题分组，给出层级关系与关键节点，并标出节点之间的因果关系或先后顺序。'),
    ('builtin-contract-review', '合同审阅', '逐条找出合同风险并给出修改建议', 'skill-contract', 'contract-review', '审阅用户给的合同或协议：先做条款完整性核对，再按风险清单逐条标出风险等级，每条写清原文、可能后果与建议改法，最后给出能否签署的结论与需要律师复核的提示。'),
    ('builtin-meeting-notes', '会议纪要', '把会议记录整理成决议与待办', 'skill-meeting', 'meeting-notes', '把用户给的会议记录、转写稿或零散笔记整理成规范纪要：按议题分节，每个议题写清讨论要点、结论与待办，逐条标明责任人与截止时间。'),
    ('builtin-data-analysis', '数据表分析', '分析表格与经营数据并给出结论', 'skill-data', 'data-analysis', '先核口径再分析：写清数据概况与每个指标的定义，找出异常与结构变化，每个结论都跟具体数字和可能原因，最后列出需要核实的口径与数据缺口。'),
    ('builtin-doc-brief', '长文精读', '精读长文并输出可复用的精读笔记', 'skill-reading', 'doc-brief', '把长文读透：先给一句话主旨，再给结构地图，标出关键论据、隐含假设与可疑之处，最后列出可复用的结论和仍需确认的问题。'),
    ('builtin-industry-research', '行业调研', '联网调研行业并输出带来源的报告', 'skill-research', 'industry-research', '先联网多轮检索再动笔：市场规模、主要玩家、盈利模式、监管与趋势都要给出带来源与可信等级的事实，数据冲突时并列呈现，检索不到就写明未检索到。'),
    ('builtin-official-writing', '公文写作', '起草通知、请示、报告等公文', 'skill-writing', 'official-writing', '先定文种与行文方向，再按标准骨架起草：主送单位、事由、事项、要求、落款齐全，用语规范，缺失的信息用占位符并在末尾列出待补清单。'),
)

_MYSQL_POOL = None
_MYSQL_ACTIVE_CONNECTIONS: set['MySQLDatabase'] = set()
_REQUEST_MYSQL_CONNECTIONS: ContextVar[tuple['MySQLDatabase', ...]] = ContextVar(
    'request_mysql_connections', default=()
)
_MYSQL_POOL_LOCK = None


class MySQLCursor:
    """Small cursor wrapper that keeps the aiosqlite cursor surface used here."""

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount or 0)

    async def fetchone(self):
        return await self._cursor.fetchone()

    async def fetchall(self):
        return list(await self._cursor.fetchall())

    async def fetchmany(self, size: int | None = None):
        if size is None:
            return list(await self._cursor.fetchmany())
        return list(await self._cursor.fetchmany(size))

    async def close(self) -> None:
        await self._cursor.close()


class SQLiteDatabase:
    def __init__(self, connection: aiosqlite.Connection):
        self._connection = connection

    async def execute(self, query: str, params: tuple | list = ()):
        return await self._connection.execute(query, tuple(params or ()))

    async def executemany(self, query: str, params):
        return await self._connection.executemany(query, params)

    async def executescript(self, script: str) -> None:
        await self._connection.executescript(script)

    async def commit(self) -> None:
        await self._connection.commit()

    async def rollback(self) -> None:
        await self._connection.rollback()

    async def close(self) -> None:
        await self._connection.close()


def _translate_mysql(query: str) -> str:
    """Translate the SQLite syntax used by the application to MySQL 8."""
    translated = query

    # FTS5 is only a local search optimization in this project. The runtime
    # retrieval itself scans ``chunks`` and scores terms, so MySQL can store the
    # same metadata in a regular InnoDB table.
    translated = re.sub(
        r"INSERT\s+INTO\s+chunks_fts\s*\(\s*rowid\s*,\s*content\s*,\s*chunk_id\s*,\s*knowledge_id\s*,\s*filename\s*,\s*page_number\s*\)\s*"
        r"VALUES\s*\(\s*\(\s*SELECT\s+COALESCE\s*\(\s*MAX\s*\(\s*rowid\s*\)\s*,\s*0\s*\)\s*\+\s*1\s+FROM\s+chunks_fts\s*\)\s*,",
        "INSERT INTO chunks_fts(content,chunk_id,knowledge_id,filename,page_number) VALUES(",
        translated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    translated = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "REPLACE INTO", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\bON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\b",
        "ON DUPLICATE KEY UPDATE",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: f"VALUES({match.group(1)})",
        translated,
        flags=re.IGNORECASE,
    )
    # messages has no SQLite rowid in MySQL; created_at is already the stable
    # second sort key and msg ids are unique.
    translated = re.sub(r",\s*rowid\s+(?:ASC|DESC)\b", "", translated, flags=re.IGNORECASE)
    return translated.replace("?", "%s")


class MySQLDatabase:
    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._closed = False
        _MYSQL_ACTIVE_CONNECTIONS.add(self)

    async def execute(self, query: str, params: tuple | list = ()):
        if self._closed:
            raise RuntimeError('MySQL 连接已经归还连接池')
        cursor = await self._connection.cursor()
        await cursor.execute(_translate_mysql(query), tuple(params or ()))
        return MySQLCursor(cursor)

    async def executemany(self, query: str, params):
        if self._closed:
            raise RuntimeError('MySQL 连接已经归还连接池')
        cursor = await self._connection.cursor()
        await cursor.executemany(_translate_mysql(query), params)
        return MySQLCursor(cursor)

    async def executescript(self, script: str) -> None:
        statements = [item.strip() for item in script.split(';') if item.strip()]
        for statement in statements:
            await self.execute(statement)

    async def commit(self) -> None:
        if self._closed:
            raise RuntimeError('MySQL 连接已经归还连接池')
        await self._connection.commit()

    async def rollback(self) -> None:
        if self._closed:
            return
        await self._connection.rollback()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._connection.rollback()
        except Exception as exc:  # noqa: BLE001 - 退出/超时路径上连接可能正被别的协程占用
            print(f"[db] 关闭连接前回滚失败（忽略）：{exc}", flush=True)
        finally:
            self._pool.release(self._connection)
            _MYSQL_ACTIVE_CONNECTIONS.discard(self)
            current = _REQUEST_MYSQL_CONNECTIONS.get()
            if self in current:
                _REQUEST_MYSQL_CONNECTIONS.set(tuple(item for item in current if item is not self))


def _mysql_kwargs() -> dict[str, Any]:
    parsed = urlparse(settings.database_url.replace("mysql+aiomysql://", "mysql://", 1))
    if not parsed.hostname or not parsed.path.lstrip('/'):
        raise ValueError("DATABASE_URL 缺少 MySQL 主机名或数据库名")
    query = parse_qs(parsed.query)
    kwargs: dict[str, Any] = {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": unquote(parsed.path.lstrip('/')),
        "charset": query.get("charset", ["utf8mb4"])[0],
        "autocommit": False,
        "connect_timeout": 10,
        "minsize": 1,
        "maxsize": 10,
        "pool_recycle": 1800,
    }
    ssl_value = query.get("ssl", [""])[0].lower()
    if ssl_value in {"1", "true", "yes", "required"}:
        kwargs["ssl"] = {}
    ssl_ca = query.get("ssl_ca", [""])[0]
    if ssl_ca:
        kwargs["ssl"] = {"ca": ssl_ca}
    return kwargs


async def _get_mysql_pool():
    global _MYSQL_POOL, _MYSQL_POOL_LOCK
    if _MYSQL_POOL_LOCK is None:
        import asyncio

        _MYSQL_POOL_LOCK = asyncio.Lock()
    async with _MYSQL_POOL_LOCK:
        if _MYSQL_POOL is None:
            try:
                import aiomysql
            except ImportError as exc:
                raise RuntimeError("MySQL 部署需要安装 aiomysql") from exc
            _MYSQL_POOL = await aiomysql.create_pool(
                cursorclass=aiomysql.DictCursor,
                **_mysql_kwargs(),
            )
    return _MYSQL_POOL


async def close_db_pool() -> None:
    """Close the shared pool on application shutdown.

    退出时可能还有后台任务占着同一条连接（巡检、整理、任务轮询都会），逐条兜住异常：
    关连接失败不该让「应用关闭」以 traceback 收场——容器已经在退出了。
    """
    global _MYSQL_POOL
    for database in tuple(_MYSQL_ACTIVE_CONNECTIONS):
        try:
            await database.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[db] 退出时关闭连接失败（忽略）：{exc}", flush=True)
    if _MYSQL_POOL is not None:
        _MYSQL_POOL.close()
        await _MYSQL_POOL.wait_closed()
        _MYSQL_POOL = None


def take_request_connections() -> tuple['MySQLDatabase', ...]:
    """Detach this request's MySQL connections before a streamed response starts."""
    databases = tuple(_REQUEST_MYSQL_CONNECTIONS.get())
    if databases:
        _REQUEST_MYSQL_CONNECTIONS.set(())
    return databases


async def close_connections(databases: tuple['MySQLDatabase', ...]) -> None:
    for database in reversed(databases):
        await database.close()


async def close_request_connections() -> None:
    """Return any MySQL connections left open when a request exits."""
    await close_connections(take_request_connections())


async def connect():
    if settings.database_backend == 'sqlite':
        path = settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path, timeout=30)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        return SQLiteDatabase(connection)

    pool = await _get_mysql_pool()
    connection = await pool.acquire()
    database = MySQLDatabase(pool, connection)
    _REQUEST_MYSQL_CONNECTIONS.set((*_REQUEST_MYSQL_CONNECTIONS.get(), database))
    return database


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, openid TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL, avatar TEXT, session_key TEXT DEFAULT '', membership TEXT NOT NULL DEFAULT 'free', membership_expires_at TEXT DEFAULT '', trial_used INTEGER NOT NULL DEFAULT 0, preferences TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- 注销过的微信号指纹（加盐哈希，不含可识别信息）：只用于防止反复注销刷试用与额度
CREATE TABLE IF NOT EXISTS deleted_accounts (openid_hash TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_bases (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '', icon TEXT DEFAULT 'library_books', avatar TEXT DEFAULT '', document_count INTEGER DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'private', category TEXT DEFAULT '', subscribers INTEGER DEFAULT 0, published_at TEXT DEFAULT '', status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, user_id TEXT NOT NULL, filename TEXT NOT NULL, file_type TEXT NOT NULL, file_size INTEGER DEFAULT 0, storage_path TEXT NOT NULL, page_count INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'uploaded', progress INTEGER DEFAULT 0, error_message TEXT DEFAULT '', extracted_text TEXT DEFAULT '', organized_title TEXT DEFAULT '', summary TEXT DEFAULT '', tags_json TEXT DEFAULT '[]', key_points_json TEXT DEFAULT '[]', organize_status TEXT NOT NULL DEFAULT 'pending', organize_method TEXT DEFAULT 'local', organize_error TEXT DEFAULT '', organized_at TEXT DEFAULT '', folder_id TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, content TEXT NOT NULL, page_number INTEGER DEFAULT 1, chunk_index INTEGER DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED, knowledge_id UNINDEXED, filename UNINDEXED, page_number UNINDEXED);
CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, folder_id TEXT DEFAULT '', title TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, pinned_at TEXT DEFAULT '', harness_session_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS chat_runs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, user_message_id TEXT NOT NULL DEFAULT '', knowledge_id TEXT NOT NULL, folder_id TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'running', model TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'knowledge', thinking TEXT NOT NULL DEFAULT 'quick', answer TEXT NOT NULL DEFAULT '', sources_json TEXT NOT NULL DEFAULT '[]', trace_json TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT '', artifacts_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT DEFAULT '', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, sources_json TEXT DEFAULT '[]', trace_json TEXT DEFAULT '[]', reason TEXT DEFAULT '', duration_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS knowledge_suggestions (knowledge_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, questions_json TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS folder_suggestions (knowledge_id TEXT NOT NULL, folder_id TEXT NOT NULL, fingerprint TEXT NOT NULL, questions_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(knowledge_id, folder_id));
CREATE TABLE IF NOT EXISTS pay_orders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, out_trade_no TEXT UNIQUE NOT NULL, plan TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', transaction_id TEXT DEFAULT '', prepay_id TEXT DEFAULT '', created_at TEXT NOT NULL, paid_at TEXT DEFAULT '', offer_id TEXT DEFAULT '', product_id TEXT DEFAULT '', wx_order_id TEXT DEFAULT '', attach TEXT DEFAULT '', quantity INTEGER DEFAULT 1, deliver_status TEXT DEFAULT 'pending', delivered_at TEXT DEFAULT '', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS share_cards (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_name TEXT DEFAULT '', question TEXT DEFAULT '', answer TEXT NOT NULL, sources_json TEXT DEFAULT '[]', views INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS share_claims (share_id TEXT NOT NULL, user_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, folder_id TEXT DEFAULT '', documents_json TEXT DEFAULT '[]', created_at TEXT NOT NULL, PRIMARY KEY(share_id, user_id));
CREATE TABLE IF NOT EXISTS skills (id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, summary TEXT DEFAULT '', prompt TEXT NOT NULL DEFAULT '', icon TEXT DEFAULT 'skill-node', developer_wechat TEXT DEFAULT '', harness TEXT DEFAULT '', visibility TEXT NOT NULL DEFAULT 'private', source TEXT NOT NULL DEFAULT 'custom', use_count INTEGER NOT NULL DEFAULT 0, like_count INTEGER NOT NULL DEFAULT 0, favorite_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', published_at TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_likes (skill_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(skill_id, user_id), FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS skill_favorites (skill_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(skill_id, user_id), FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS knowledge_shares (token TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, accepted_by TEXT DEFAULT '', accepted_at TEXT DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS knowledge_subscriptions (user_id TEXT NOT NULL, source_knowledge_id TEXT NOT NULL, mirror_knowledge_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(user_id, source_knowledge_id), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(source_knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE, FOREIGN KEY(mirror_knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL DEFAULT '', knowledge_id TEXT NOT NULL DEFAULT '', folder_id TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, file_type TEXT NOT NULL DEFAULT '', file_size INTEGER NOT NULL DEFAULT 0, mime TEXT NOT NULL DEFAULT 'application/octet-stream', digest TEXT NOT NULL DEFAULT '', storage_path TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '', document_id TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS content_meta (slug TEXT PRIMARY KEY, version TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', subtitle TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '', source_updated_at TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS content_entries (slug TEXT NOT NULL, entry_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', group_title TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '', read_minutes INTEGER NOT NULL DEFAULT 2, body_md TEXT NOT NULL DEFAULT '', blocks_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(slug, entry_id));
CREATE TABLE IF NOT EXISTS content_assets (slug TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL DEFAULT 'application/octet-stream', size INTEGER NOT NULL DEFAULT 0, bytes BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(slug, name));
CREATE TABLE IF NOT EXISTS usage_logs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cost_yuan REAL NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wx_tokens (name TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS kf_sessions (openid TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT '', nickname TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0, unread_count INTEGER NOT NULL DEFAULT 0, last_message_at TEXT NOT NULL DEFAULT '', last_autoreply_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS kf_messages (id TEXT PRIMARY KEY, openid TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'user', msg_type TEXT NOT NULL DEFAULT 'text', content TEXT NOT NULL DEFAULT '', media_id TEXT NOT NULL DEFAULT '', kf_account TEXT NOT NULL DEFAULT '', raw_json TEXT NOT NULL DEFAULT '{}', media_path TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'push', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kf_outbox (id TEXT PRIMARY KEY, openid TEXT NOT NULL, msg_type TEXT NOT NULL DEFAULT 'text', payload_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', kf_account TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', progress TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '');
"""


_SQLITE_MIGRATIONS: dict[str, dict[str, str]] = {
    'users': {'session_key': "TEXT DEFAULT ''", 'membership': "TEXT NOT NULL DEFAULT 'free'", 'membership_expires_at': "TEXT DEFAULT ''", 'token_version': "INTEGER NOT NULL DEFAULT 0", 'trial_used': "INTEGER NOT NULL DEFAULT 0", 'preferences': "TEXT NOT NULL DEFAULT '{}'"},
    'pay_orders': {'offer_id': "TEXT DEFAULT ''", 'product_id': "TEXT DEFAULT ''", 'wx_order_id': "TEXT DEFAULT ''", 'attach': "TEXT DEFAULT ''", 'quantity': "INTEGER DEFAULT 1", 'deliver_status': "TEXT DEFAULT 'pending'", 'delivered_at': "TEXT DEFAULT ''"},
    'documents': {'organized_title': "TEXT DEFAULT ''", 'summary': "TEXT DEFAULT ''", 'tags_json': "TEXT DEFAULT '[]'", 'key_points_json': "TEXT DEFAULT '[]'", 'organize_status': "TEXT NOT NULL DEFAULT 'pending'", 'organize_method': "TEXT DEFAULT 'local'", 'organize_error': "TEXT DEFAULT ''", 'organized_at': "TEXT DEFAULT ''", 'folder_id': "TEXT DEFAULT ''", 'origin_document_id': "TEXT DEFAULT ''", 'last_viewed_at': "TEXT DEFAULT ''"},
    'knowledge_bases': {'last_used_at': "TEXT DEFAULT ''", 'avatar': "TEXT DEFAULT ''", 'visibility': "TEXT NOT NULL DEFAULT 'private'", 'category': "TEXT DEFAULT ''", 'subscribers': "INTEGER DEFAULT 0", 'published_at': "TEXT DEFAULT ''", 'mirror_of': "TEXT DEFAULT ''", 'mirror_owner': "TEXT DEFAULT ''", 'mirror_state': "TEXT DEFAULT ''", 'mirror_token': "TEXT DEFAULT ''", 'mirror_at': "TEXT DEFAULT ''"},
    'conversations': {'memory_summary': "TEXT NOT NULL DEFAULT ''", 'folder_id': "TEXT DEFAULT ''", 'pinned': "INTEGER NOT NULL DEFAULT 0", 'pinned_at': "TEXT DEFAULT ''", 'harness_session_id': "TEXT NOT NULL DEFAULT ''"},
    'content_entries': {'updated_at': "TEXT NOT NULL DEFAULT ''", 'effective_at': "TEXT NOT NULL DEFAULT ''"},
    'folders': {'origin_folder_id': "TEXT DEFAULT ''"},
    'messages': {'trace_json': "TEXT DEFAULT '[]'", 'reason': "TEXT DEFAULT ''", 'duration_ms': "INTEGER NOT NULL DEFAULT 0", 'artifacts_json': "TEXT DEFAULT '[]'"},
    'share_cards': {'title': "TEXT NOT NULL DEFAULT ''", 'files_json': "TEXT DEFAULT '[]'"},
    'kf_messages': {'media_path': "TEXT NOT NULL DEFAULT ''", 'source': "TEXT NOT NULL DEFAULT 'push'"},
}


async def _init_sqlite() -> None:
    db = await connect()
    await db.execute('PRAGMA journal_mode = WAL')
    await db.executescript(_SQLITE_SCHEMA)
    for table, columns in _SQLITE_MIGRATIONS.items():
        existing = {row[1] for row in await (await db.execute(f'PRAGMA table_info({table})')).fetchall()}
        for name, declaration in columns.items():
            if name not in existing:
                await db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    await db.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_conversation ON artifacts(conversation_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_kf_messages_openid ON kf_messages(openid, created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_kf_outbox_state ON kf_outbox(state, created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_user_time ON usage_logs(user_id, created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_shares_knowledge ON knowledge_shares(knowledge_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_subscriptions_source ON knowledge_subscriptions(source_knowledge_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_subscriptions_mirror ON knowledge_subscriptions(mirror_knowledge_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_documents_origin ON documents(origin_document_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_runs_conversation ON chat_runs(conversation_id, status, updated_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_runs_user_status ON chat_runs(user_id, status, updated_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_status ON jobs(user_id, status, updated_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_bases_mirror ON knowledge_bases(mirror_of)")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_orders_wx_order_id ON pay_orders(wx_order_id) WHERE wx_order_id != ''")
    await _seed_builtin_skills(db)
    await db.commit()
    await db.close()


_MYSQL_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
      id VARCHAR(32) PRIMARY KEY,
      openid VARCHAR(128) NOT NULL UNIQUE,
      nickname VARCHAR(128) NOT NULL DEFAULT '',
      avatar VARCHAR(2048) NULL,
      session_key VARCHAR(512) NOT NULL DEFAULT '',
      membership VARCHAR(16) NOT NULL DEFAULT 'free',
      membership_expires_at VARCHAR(64) NOT NULL DEFAULT '',
      token_version INT NOT NULL DEFAULT 0,
      trial_used INT NOT NULL DEFAULT 0,
      preferences VARCHAR(2048) NOT NULL DEFAULT '{}',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS deleted_accounts (
      openid_hash VARCHAR(64) PRIMARY KEY,
      deleted_at VARCHAR(64) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_bases (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      name VARCHAR(255) NOT NULL,
      description VARCHAR(1024) NULL,
      icon VARCHAR(64) NULL,
      avatar TEXT NULL,
      document_count INT NOT NULL DEFAULT 0,
      visibility VARCHAR(16) NOT NULL DEFAULT 'private',
      category VARCHAR(128) NOT NULL DEFAULT '',
      subscribers INT NOT NULL DEFAULT 0,
      published_at VARCHAR(64) NOT NULL DEFAULT '',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      last_used_at VARCHAR(64) NOT NULL DEFAULT '',
      mirror_of VARCHAR(32) NOT NULL DEFAULT '',
      mirror_owner VARCHAR(32) NOT NULL DEFAULT '',
      mirror_state VARCHAR(32) NOT NULL DEFAULT '',
      mirror_token VARCHAR(64) NOT NULL DEFAULT '',
      mirror_at VARCHAR(64) NOT NULL DEFAULT '',
      CONSTRAINT fk_kb_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      INDEX idx_knowledge_user (user_id, status),
      INDEX idx_knowledge_bases_mirror (mirror_of)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
      id VARCHAR(32) PRIMARY KEY,
      knowledge_id VARCHAR(32) NOT NULL,
      user_id VARCHAR(32) NOT NULL,
      filename VARCHAR(512) NOT NULL,
      file_type VARCHAR(32) NOT NULL,
      file_size BIGINT NOT NULL DEFAULT 0,
      storage_path TEXT NOT NULL,
      page_count INT NOT NULL DEFAULT 0,
      status VARCHAR(24) NOT NULL DEFAULT 'uploaded',
      progress INT NOT NULL DEFAULT 0,
      error_message TEXT NULL,
      extracted_text LONGTEXT NULL,
      organized_title VARCHAR(255) NOT NULL DEFAULT '',
      summary TEXT NULL,
      tags_json LONGTEXT NULL,
      key_points_json LONGTEXT NULL,
      organize_status VARCHAR(24) NOT NULL DEFAULT 'pending',
      organize_method VARCHAR(24) NOT NULL DEFAULT 'local',
      organize_error TEXT NULL,
      organized_at VARCHAR(64) NOT NULL DEFAULT '',
      folder_id VARCHAR(32) NOT NULL DEFAULT '',
      origin_document_id VARCHAR(32) NOT NULL DEFAULT '',
      last_viewed_at VARCHAR(64) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_documents_kb FOREIGN KEY (knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
      INDEX idx_documents_user_status (user_id, status),
      INDEX idx_documents_knowledge_status (knowledge_id, status),
      INDEX idx_documents_origin (origin_document_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
      id VARCHAR(32) PRIMARY KEY,
      document_id VARCHAR(32) NOT NULL,
      knowledge_id VARCHAR(32) NOT NULL,
      content LONGTEXT NOT NULL,
      page_number INT NOT NULL DEFAULT 1,
      chunk_index INT NOT NULL DEFAULT 0,
      created_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_chunks_document FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
      INDEX idx_chunks_document (document_id, chunk_index),
      INDEX idx_chunks_knowledge (knowledge_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks_fts (
      rowid BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      content LONGTEXT NOT NULL,
      chunk_id VARCHAR(32) NOT NULL,
      knowledge_id VARCHAR(32) NOT NULL,
      filename VARCHAR(512) NOT NULL,
      page_number INT NOT NULL DEFAULT 1,
      INDEX idx_chunks_fts_chunk (chunk_id),
      INDEX idx_chunks_fts_knowledge (knowledge_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS folders (
      id VARCHAR(32) PRIMARY KEY,
      knowledge_id VARCHAR(32) NOT NULL,
      user_id VARCHAR(32) NOT NULL,
      name VARCHAR(255) NOT NULL,
      origin_folder_id VARCHAR(32) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_folders_kb FOREIGN KEY (knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
      INDEX idx_folders_knowledge (knowledge_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      knowledge_id VARCHAR(32) NOT NULL,
      folder_id VARCHAR(32) NOT NULL DEFAULT '',
      title VARCHAR(255) NOT NULL,
      pinned INT NOT NULL DEFAULT 0,
      pinned_at VARCHAR(64) NOT NULL DEFAULT '',
      harness_session_id VARCHAR(80) NOT NULL DEFAULT '',
      memory_summary TEXT NULL,
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      INDEX idx_conversations_user (user_id, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_runs (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      conversation_id VARCHAR(32) NOT NULL,
      user_message_id VARCHAR(32) NOT NULL DEFAULT '',
      knowledge_id VARCHAR(32) NOT NULL,
      folder_id VARCHAR(32) NOT NULL DEFAULT '',
      status VARCHAR(24) NOT NULL DEFAULT 'running',
      model VARCHAR(128) NOT NULL DEFAULT '',
      mode VARCHAR(24) NOT NULL DEFAULT 'knowledge',
      thinking VARCHAR(24) NOT NULL DEFAULT 'quick',
      answer LONGTEXT NULL,
      sources_json LONGTEXT NULL,
      trace_json LONGTEXT NULL,
      reason LONGTEXT NULL,
      artifacts_json LONGTEXT NULL,
      error TEXT NULL,
      revision INT NOT NULL DEFAULT 0,
      cancel_requested INT NOT NULL DEFAULT 0,
      duration_ms INT NOT NULL DEFAULT 0,
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      finished_at VARCHAR(64) NOT NULL DEFAULT '',
      CONSTRAINT fk_chat_runs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      CONSTRAINT fk_chat_runs_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
      INDEX idx_chat_runs_conversation (conversation_id, status, updated_at),
      INDEX idx_chat_runs_user_status (user_id, status, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      kind VARCHAR(32) NOT NULL,
      status VARCHAR(24) NOT NULL DEFAULT 'queued',
      progress VARCHAR(255) NOT NULL DEFAULT '',
      payload_json LONGTEXT NULL,
      result_json LONGTEXT NULL,
      error TEXT NULL,
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      finished_at VARCHAR(64) NOT NULL DEFAULT '',
      CONSTRAINT fk_jobs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      INDEX idx_jobs_user_status (user_id, status, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
      id VARCHAR(32) PRIMARY KEY,
      conversation_id VARCHAR(32) NOT NULL,
      role VARCHAR(16) NOT NULL,
      content LONGTEXT NOT NULL,
      sources_json LONGTEXT NULL,
      trace_json LONGTEXT NULL,
      reason TEXT NULL,
      duration_ms INT NOT NULL DEFAULT 0,
      artifacts_json LONGTEXT NULL,
      created_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
      INDEX idx_messages_conversation (conversation_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_suggestions (
      knowledge_id VARCHAR(32) PRIMARY KEY,
      fingerprint VARCHAR(128) NOT NULL,
      questions_json LONGTEXT NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_suggestions_kb FOREIGN KEY (knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS folder_suggestions (
      knowledge_id VARCHAR(32) NOT NULL,
      folder_id VARCHAR(32) NOT NULL,
      fingerprint VARCHAR(128) NOT NULL,
      questions_json LONGTEXT NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      PRIMARY KEY (knowledge_id, folder_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS pay_orders (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      out_trade_no VARCHAR(96) NOT NULL UNIQUE,
      plan VARCHAR(64) NOT NULL,
      amount INT NOT NULL,
      status VARCHAR(24) NOT NULL DEFAULT 'pending',
      transaction_id VARCHAR(128) NOT NULL DEFAULT '',
      prepay_id VARCHAR(128) NOT NULL DEFAULT '',
      offer_id VARCHAR(128) NOT NULL DEFAULT '',
      product_id VARCHAR(128) NOT NULL DEFAULT '',
      wx_order_id VARCHAR(128) NOT NULL DEFAULT '',
      attach VARCHAR(512) NOT NULL DEFAULT '',
      quantity INT NOT NULL DEFAULT 1,
      deliver_status VARCHAR(24) NOT NULL DEFAULT 'pending',
      delivered_at VARCHAR(64) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL,
      paid_at VARCHAR(64) NOT NULL DEFAULT '',
      CONSTRAINT fk_pay_orders_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      INDEX idx_pay_orders_user (user_id, created_at),
      INDEX idx_pay_orders_wx_order_id (wx_order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS share_cards (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      knowledge_name VARCHAR(255) NOT NULL DEFAULT '',
      question TEXT NULL,
      answer LONGTEXT NOT NULL,
      sources_json LONGTEXT NULL,
      files_json LONGTEXT NULL,
      title VARCHAR(255) NOT NULL DEFAULT '',
      views INT NOT NULL DEFAULT 0,
      created_at VARCHAR(64) NOT NULL,
      CONSTRAINT fk_share_cards_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS share_claims (
      share_id VARCHAR(32) NOT NULL,
      user_id VARCHAR(32) NOT NULL,
      knowledge_id VARCHAR(32) NOT NULL,
      folder_id VARCHAR(32) NOT NULL DEFAULT '',
      documents_json LONGTEXT NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      PRIMARY KEY (share_id, user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
      id VARCHAR(64) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL DEFAULT '',
      name VARCHAR(128) NOT NULL,
      summary VARCHAR(512) NOT NULL DEFAULT '',
      prompt LONGTEXT NOT NULL,
      icon VARCHAR(64) NOT NULL DEFAULT 'skill-node',
      developer_wechat VARCHAR(128) NOT NULL DEFAULT '',
      harness VARCHAR(128) NOT NULL DEFAULT '',
      visibility VARCHAR(16) NOT NULL DEFAULT 'private',
      source VARCHAR(16) NOT NULL DEFAULT 'custom',
      use_count INT NOT NULL DEFAULT 0,
      like_count INT NOT NULL DEFAULT 0,
      favorite_count INT NOT NULL DEFAULT 0,
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      published_at VARCHAR(64) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL,
      updated_at VARCHAR(64) NOT NULL,
      INDEX idx_skills_user_status (user_id, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_likes (
      skill_id VARCHAR(64) NOT NULL,
      user_id VARCHAR(32) NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      PRIMARY KEY (skill_id, user_id),
      CONSTRAINT fk_skill_likes_skill FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_favorites (
      skill_id VARCHAR(64) NOT NULL,
      user_id VARCHAR(32) NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      PRIMARY KEY (skill_id, user_id),
      CONSTRAINT fk_skill_favorites_skill FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_subscriptions (
      user_id VARCHAR(32) NOT NULL,
      source_knowledge_id VARCHAR(32) NOT NULL,
      mirror_knowledge_id VARCHAR(32) NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      PRIMARY KEY (user_id, source_knowledge_id),
      CONSTRAINT fk_knowledge_subscriptions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
      CONSTRAINT fk_knowledge_subscriptions_source FOREIGN KEY (source_knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
      CONSTRAINT fk_knowledge_subscriptions_mirror FOREIGN KEY (mirror_knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
      INDEX idx_knowledge_subscriptions_source (source_knowledge_id),
      INDEX idx_knowledge_subscriptions_mirror (mirror_knowledge_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_shares (
      token VARCHAR(128) PRIMARY KEY,
      knowledge_id VARCHAR(32) NOT NULL,
      owner_user_id VARCHAR(32) NOT NULL,
      created_at VARCHAR(64) NOT NULL,
      expires_at VARCHAR(64) NOT NULL,
      accepted_by VARCHAR(32) NOT NULL DEFAULT '',
      accepted_at VARCHAR(64) NOT NULL DEFAULT '',
      revoked INT NOT NULL DEFAULT 0,
      CONSTRAINT fk_knowledge_shares_kb FOREIGN KEY (knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
      INDEX idx_knowledge_shares_knowledge (knowledge_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      conversation_id VARCHAR(32) NOT NULL DEFAULT '',
      knowledge_id VARCHAR(32) NOT NULL DEFAULT '',
      folder_id VARCHAR(32) NOT NULL DEFAULT '',
      filename VARCHAR(512) NOT NULL,
      file_type VARCHAR(32) NOT NULL DEFAULT '',
      file_size BIGINT NOT NULL DEFAULT 0,
      mime VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
      digest VARCHAR(64) NOT NULL DEFAULT '',
      storage_path TEXT NOT NULL,
      source_path TEXT NULL,
      document_id VARCHAR(32) NOT NULL DEFAULT '',
      note VARCHAR(255) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL,
      INDEX idx_artifacts_conversation (conversation_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS content_meta (
      slug VARCHAR(32) PRIMARY KEY,
      version VARCHAR(64) NOT NULL DEFAULT '',
      fingerprint VARCHAR(128) NOT NULL DEFAULT '',
      title VARCHAR(512) NOT NULL DEFAULT '',
      subtitle VARCHAR(1024) NOT NULL DEFAULT '',
      intro TEXT NULL,
      source_updated_at VARCHAR(64) NOT NULL DEFAULT '',
      imported_at VARCHAR(64) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS content_entries (
      slug VARCHAR(32) NOT NULL,
      entry_id VARCHAR(128) NOT NULL,
      group_id VARCHAR(128) NOT NULL DEFAULT '',
      group_title VARCHAR(255) NOT NULL DEFAULT '',
      sort_order INT NOT NULL DEFAULT 0,
      title VARCHAR(512) NOT NULL DEFAULT '',
      summary TEXT NULL,
      icon VARCHAR(64) NOT NULL DEFAULT '',
      cover VARCHAR(512) NOT NULL DEFAULT '',
      read_minutes INT NOT NULL DEFAULT 2,
      body_md LONGTEXT NOT NULL,
      blocks_json LONGTEXT NOT NULL,
      updated_at VARCHAR(64) NOT NULL DEFAULT '',
      effective_at VARCHAR(64) NOT NULL DEFAULT '',
      PRIMARY KEY (slug, entry_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS content_assets (
      slug VARCHAR(32) NOT NULL,
      name VARCHAR(255) NOT NULL,
      mime VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
      size BIGINT NOT NULL DEFAULT 0,
      bytes LONGBLOB NOT NULL,
      updated_at VARCHAR(64) NOT NULL DEFAULT '',
      PRIMARY KEY (slug, name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_logs (
      id VARCHAR(32) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL,
      conversation_id VARCHAR(32) NOT NULL DEFAULT '',
      message_id VARCHAR(32) NOT NULL DEFAULT '',
      model VARCHAR(128) NOT NULL DEFAULT '',
      input_tokens INT NOT NULL DEFAULT 0,
      cache_read_tokens INT NOT NULL DEFAULT 0,
      cache_write_tokens INT NOT NULL DEFAULT 0,
      output_tokens INT NOT NULL DEFAULT 0,
      cost_yuan DOUBLE NOT NULL DEFAULT 0,
      credits INT NOT NULL DEFAULT 0,
      created_at VARCHAR(64) NOT NULL,
      INDEX idx_usage_logs_user_time (user_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wx_tokens (
      name VARCHAR(64) PRIMARY KEY,
      value LONGTEXT NOT NULL,
      expires_at VARCHAR(64) NOT NULL DEFAULT '',
      updated_at VARCHAR(64) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS kf_sessions (
      openid VARCHAR(128) PRIMARY KEY,
      user_id VARCHAR(32) NOT NULL DEFAULT '',
      nickname VARCHAR(128) NOT NULL DEFAULT '',
      message_count INT NOT NULL DEFAULT 0,
      unread_count INT NOT NULL DEFAULT 0,
      last_message_at VARCHAR(64) NOT NULL DEFAULT '',
      last_autoreply_at VARCHAR(64) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL DEFAULT '',
      updated_at VARCHAR(64) NOT NULL DEFAULT '',
      INDEX idx_kf_sessions_updated (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS kf_messages (
      id VARCHAR(32) PRIMARY KEY,
      openid VARCHAR(128) NOT NULL,
      user_id VARCHAR(32) NOT NULL DEFAULT '',
      role VARCHAR(16) NOT NULL DEFAULT 'user',
      msg_type VARCHAR(32) NOT NULL DEFAULT 'text',
      content LONGTEXT NULL,
      media_id VARCHAR(255) NOT NULL DEFAULT '',
      media_path TEXT NULL,
      kf_account VARCHAR(255) NOT NULL DEFAULT '',
      raw_json LONGTEXT NULL,
      source VARCHAR(16) NOT NULL DEFAULT 'push',
      created_at VARCHAR(64) NOT NULL,
      INDEX idx_kf_messages_openid (openid, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS kf_outbox (
      id VARCHAR(32) PRIMARY KEY,
      openid VARCHAR(128) NOT NULL,
      msg_type VARCHAR(32) NOT NULL DEFAULT 'text',
      payload_json LONGTEXT NOT NULL,
      state VARCHAR(16) NOT NULL DEFAULT 'pending',
      error TEXT NULL,
      kf_account VARCHAR(255) NOT NULL DEFAULT '',
      created_at VARCHAR(64) NOT NULL,
      sent_at VARCHAR(64) NOT NULL DEFAULT '',
      INDEX idx_kf_outbox_state (state, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


_MYSQL_MIGRATIONS: dict[str, dict[str, str]] = {
    'users': {'session_key': "VARCHAR(512) NOT NULL DEFAULT ''", 'membership': "VARCHAR(16) NOT NULL DEFAULT 'free'", 'membership_expires_at': "VARCHAR(64) NOT NULL DEFAULT ''", 'token_version': "INT NOT NULL DEFAULT 0", 'trial_used': "INT NOT NULL DEFAULT 0", 'preferences': "VARCHAR(2048) NOT NULL DEFAULT '{}'"},
    'pay_orders': {'offer_id': "VARCHAR(128) NOT NULL DEFAULT ''", 'product_id': "VARCHAR(128) NOT NULL DEFAULT ''", 'wx_order_id': "VARCHAR(128) NOT NULL DEFAULT ''", 'attach': "VARCHAR(512) NOT NULL DEFAULT ''", 'quantity': "INT NOT NULL DEFAULT 1", 'deliver_status': "VARCHAR(24) NOT NULL DEFAULT 'pending'", 'delivered_at': "VARCHAR(64) NOT NULL DEFAULT ''"},
    'documents': {'organized_title': "VARCHAR(255) NOT NULL DEFAULT ''", 'summary': "TEXT NULL", 'tags_json': "LONGTEXT NULL", 'key_points_json': "LONGTEXT NULL", 'organize_status': "VARCHAR(24) NOT NULL DEFAULT 'pending'", 'organize_method': "VARCHAR(24) NOT NULL DEFAULT 'local'", 'organize_error': "TEXT NULL", 'organized_at': "VARCHAR(64) NOT NULL DEFAULT ''", 'folder_id': "VARCHAR(32) NOT NULL DEFAULT ''", 'origin_document_id': "VARCHAR(32) NOT NULL DEFAULT ''", 'last_viewed_at': "VARCHAR(64) NOT NULL DEFAULT ''"},
    'knowledge_bases': {'last_used_at': "VARCHAR(64) NOT NULL DEFAULT ''", 'avatar': "TEXT NULL", 'visibility': "VARCHAR(16) NOT NULL DEFAULT 'private'", 'category': "VARCHAR(128) NOT NULL DEFAULT ''", 'subscribers': "INT NOT NULL DEFAULT 0", 'published_at': "VARCHAR(64) NOT NULL DEFAULT ''", 'mirror_of': "VARCHAR(32) NOT NULL DEFAULT ''", 'mirror_owner': "VARCHAR(32) NOT NULL DEFAULT ''", 'mirror_state': "VARCHAR(32) NOT NULL DEFAULT ''", 'mirror_token': "VARCHAR(64) NOT NULL DEFAULT ''", 'mirror_at': "VARCHAR(64) NOT NULL DEFAULT ''"},
    'conversations': {'memory_summary': "TEXT NULL", 'folder_id': "VARCHAR(32) NOT NULL DEFAULT ''", 'pinned': "INT NOT NULL DEFAULT 0", 'pinned_at': "VARCHAR(64) NOT NULL DEFAULT ''", 'harness_session_id': "VARCHAR(80) NOT NULL DEFAULT ''"},
    'content_entries': {'updated_at': "VARCHAR(64) NOT NULL DEFAULT ''", 'effective_at': "VARCHAR(64) NOT NULL DEFAULT ''"},
    'folders': {'origin_folder_id': "VARCHAR(32) NOT NULL DEFAULT ''"},
    'messages': {'trace_json': "LONGTEXT NULL", 'reason': "TEXT NULL", 'duration_ms': "INT NOT NULL DEFAULT 0", 'artifacts_json': "LONGTEXT NULL"},
    'share_cards': {'title': "VARCHAR(255) NOT NULL DEFAULT ''", 'files_json': "LONGTEXT NULL"},
    'kf_messages': {'media_path': "TEXT NULL", 'source': "VARCHAR(16) NOT NULL DEFAULT 'push'"},
}


async def _init_mysql() -> None:
    db = await connect()
    try:
        for statement in _MYSQL_SCHEMA:
            await db.execute(statement)
        for table, columns in _MYSQL_MIGRATIONS.items():
            rows = await (await db.execute(f"SHOW COLUMNS FROM `{table}`")).fetchall()
            existing = {str(row['Field']) for row in rows}
            for name, declaration in columns.items():
                if name not in existing:
                    await db.execute(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {declaration}")
        await _seed_builtin_skills(db)
        await db.commit()
    finally:
        await db.close()


async def _seed_builtin_skills(db) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    for skill_id, name, summary, icon, harness, prompt in BUILTIN_SKILLS:
        await db.execute(
            "INSERT OR IGNORE INTO skills(id,user_id,name,summary,prompt,icon,harness,visibility,source,status,published_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (skill_id, '', name, summary, prompt, icon, harness, 'public', 'builtin', 'active', stamp, stamp, stamp),
        )
        await db.execute(
            "UPDATE skills SET name=?, summary=?, prompt=?, icon=?, harness=?, status='active' WHERE id=? AND source='builtin'",
            (name, summary, prompt, icon, harness, skill_id),
        )


async def init_db() -> None:
    if settings.database_backend == 'sqlite':
        await _init_sqlite()
    else:
        await _init_mysql()


async def fetchone(db, query: str, params: tuple = ()):
    cursor = await db.execute(query, params)
    return await cursor.fetchone()


async def fetchall(db, query: str, params: tuple = ()) -> list:
    cursor = await db.execute(query, params)
    return await cursor.fetchall()


def row_dict(row: Any | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def decode_sources(value: str) -> list[dict[str, Any]]:
    try:
        return json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
