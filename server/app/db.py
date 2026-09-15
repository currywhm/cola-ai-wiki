import json
from typing import Any
import aiosqlite
from .config import settings

DB_PATH = settings.database_path
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


async def connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db() -> None:
    db = await connect()
    await db.execute('PRAGMA journal_mode = WAL')
    await db.executescript("""
    CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, openid TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL, avatar TEXT, session_key TEXT DEFAULT '', membership TEXT NOT NULL DEFAULT 'free', membership_expires_at TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS knowledge_bases (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '', icon TEXT DEFAULT 'library_books', document_count INTEGER DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'private', category TEXT DEFAULT '', subscribers INTEGER DEFAULT 0, status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, user_id TEXT NOT NULL, filename TEXT NOT NULL, file_type TEXT NOT NULL, file_size INTEGER DEFAULT 0, storage_path TEXT NOT NULL, page_count INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'uploaded', progress INTEGER DEFAULT 0, error_message TEXT DEFAULT '', extracted_text TEXT DEFAULT '', organized_title TEXT DEFAULT '', summary TEXT DEFAULT '', tags_json TEXT DEFAULT '[]', key_points_json TEXT DEFAULT '[]', organize_status TEXT NOT NULL DEFAULT 'pending', organize_method TEXT DEFAULT 'local', organize_error TEXT DEFAULT '', organized_at TEXT DEFAULT '', folder_id TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, content TEXT NOT NULL, page_number INTEGER DEFAULT 1, chunk_index INTEGER DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED, knowledge_id UNINDEXED, filename UNINDEXED, page_number UNINDEXED);
    CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, folder_id TEXT DEFAULT '', title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, sources_json TEXT DEFAULT '[]', created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS knowledge_suggestions (knowledge_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, questions_json TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS pay_orders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, out_trade_no TEXT UNIQUE NOT NULL, plan TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', transaction_id TEXT DEFAULT '', prepay_id TEXT DEFAULT '', created_at TEXT NOT NULL, paid_at TEXT DEFAULT '', offer_id TEXT DEFAULT '', product_id TEXT DEFAULT '', wx_order_id TEXT DEFAULT '', attach TEXT DEFAULT '', quantity INTEGER DEFAULT 1, deliver_status TEXT DEFAULT 'pending', delivered_at TEXT DEFAULT '', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    """)
    # Keep existing local databases compatible with the deployable schema.
    for table, columns in {
        'users': {'session_key': "TEXT DEFAULT ''", 'membership': "TEXT NOT NULL DEFAULT 'free'", 'membership_expires_at': "TEXT DEFAULT ''", 'token_version': "INTEGER NOT NULL DEFAULT 0"},
        'pay_orders': {
            'offer_id': "TEXT DEFAULT ''", 'product_id': "TEXT DEFAULT ''", 'wx_order_id': "TEXT DEFAULT ''",
            'attach': "TEXT DEFAULT ''", 'quantity': "INTEGER DEFAULT 1", 'deliver_status': "TEXT DEFAULT 'pending'",
            'delivered_at': "TEXT DEFAULT ''",
        },
        'documents': {
            'organized_title': "TEXT DEFAULT ''", 'summary': "TEXT DEFAULT ''", 'tags_json': "TEXT DEFAULT '[]'",
            'key_points_json': "TEXT DEFAULT '[]'", 'organize_status': "TEXT NOT NULL DEFAULT 'pending'",
            'organize_method': "TEXT DEFAULT 'local'", 'organize_error': "TEXT DEFAULT ''", 'organized_at': "TEXT DEFAULT ''",
            'folder_id': "TEXT DEFAULT ''",
        },
        'knowledge_bases': {
            'visibility': "TEXT NOT NULL DEFAULT 'private'", 'category': "TEXT DEFAULT ''", 'subscribers': "INTEGER DEFAULT 0",
        },
        'conversations': {
            'memory_summary': "TEXT NOT NULL DEFAULT ''",
            'folder_id': "TEXT DEFAULT ''",
        },
    }.items():
        existing = {row[1] for row in await (await db.execute(f'PRAGMA table_info({table})')).fetchall()}
        for name, declaration in columns.items():
            if name not in existing:
                await db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_orders_wx_order_id ON pay_orders(wx_order_id) WHERE wx_order_id != ''")
    await db.commit()
    await db.close()


async def fetchone(db: aiosqlite.Connection, query: str, params: tuple = ()) -> aiosqlite.Row | None:
    cursor = await db.execute(query, params)
    return await cursor.fetchone()


async def fetchall(db: aiosqlite.Connection, query: str, params: tuple = ()) -> list[aiosqlite.Row]:
    cursor = await db.execute(query, params)
    return await cursor.fetchall()


def row_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def decode_sources(value: str) -> list[dict[str, Any]]:
    try:
        return json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
