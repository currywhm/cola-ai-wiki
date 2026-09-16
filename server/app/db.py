import json
from datetime import datetime, timezone
from typing import Any
import aiosqlite
from .config import settings

# 技能广场的内置技能：随包发行、所有人可用（owner 为空串即官方内置）。
# 只放「怎么做」的技能指令，具体要做什么仍由用户那一句话决定。
BUILTIN_SKILLS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ('builtin-organize-knowledge', '整理知识库', '把资料整理成结构化知识条目', 'knowledge-pick', 'organize-knowledge', '把当前知识库里的资料整理成结构化的知识条目：按主题归类，每条给出标题、要点和出处，合并重复内容，并在结尾列出仍然缺口的主题。'),
    ('builtin-write-report', '撰写报告', '基于资料输出一份调研报告', 'book', 'write-report', '基于当前知识库的资料写一份调研报告：先给结论与关键数据，再展开背景、现状、对比与风险，最后给出建议，并保留资料出处标记。'),
    ('builtin-make-deck', '生成 PPT', '输出分页大纲与每页要点', 'ppt', 'make-deck', '基于当前知识库的资料输出一份 PPT 大纲：按页给出标题、每页要点与建议配图方向，整体 8-14 页，重点数据单独成页。'),
    ('builtin-knowledge-diagram', '知识图解', '把长文梳理成知识结构', 'image', 'knowledge-diagram', '把当前知识库里的长文整理成一份知识图解：按主题分组，给出层级关系与关键节点，并标出节点之间的因果关系或先后顺序。'),
    ('builtin-contract-review', '合同审阅', '逐条找出合同风险并给出修改建议', 'dui', 'contract-review', '审阅用户给的合同或协议：先做条款完整性核对，再按风险清单逐条标出风险等级，每条写清原文、可能后果与建议改法，最后给出能否签署的结论与需要律师复核的提示。'),
    ('builtin-meeting-notes', '会议纪要', '把会议记录整理成决议与待办', 'history', 'meeting-notes', '把用户给的会议记录、转写稿或零散笔记整理成规范纪要：按议题分节，每个议题写清讨论要点、结论与待办，逐条标明责任人与截止时间。'),
    ('builtin-data-analysis', '数据表分析', '分析表格与经营数据并给出结论', 'shuju', 'data-analysis', '先核口径再分析：写清数据概况与每个指标的定义，找出异常与结构变化，每个结论都跟具体数字和可能原因，最后列出需要核实的口径与数据缺口。'),
    ('builtin-doc-brief', '长文精读', '精读长文并输出可复用的精读笔记', 'dengpao', 'doc-brief', '把长文读透：先给一句话主旨，再给结构地图，标出关键论据、隐含假设与可疑之处，最后列出可复用的结论和仍需确认的问题。'),
    ('builtin-industry-research', '行业调研', '联网调研行业并输出带来源的报告', 'sousuo', 'industry-research', '先联网多轮检索再动笔：市场规模、主要玩家、盈利模式、监管与趋势都要给出带来源与可信等级的事实，数据冲突时并列呈现，检索不到就写明未检索到。'),
    ('builtin-official-writing', '公文写作', '起草通知、请示、报告等公文', 'jieshao', 'official-writing', '先定文种与行文方向，再按标准骨架起草：主送单位、事由、事项、要求、落款齐全，用语规范，缺失的信息用占位符并在末尾列出待补清单。'),
)

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
    CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, openid TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL, avatar TEXT, session_key TEXT DEFAULT '', membership TEXT NOT NULL DEFAULT 'free', membership_expires_at TEXT DEFAULT '', preferences TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS knowledge_bases (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '', icon TEXT DEFAULT 'library_books', document_count INTEGER DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'private', category TEXT DEFAULT '', subscribers INTEGER DEFAULT 0, status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, user_id TEXT NOT NULL, filename TEXT NOT NULL, file_type TEXT NOT NULL, file_size INTEGER DEFAULT 0, storage_path TEXT NOT NULL, page_count INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'uploaded', progress INTEGER DEFAULT 0, error_message TEXT DEFAULT '', extracted_text TEXT DEFAULT '', organized_title TEXT DEFAULT '', summary TEXT DEFAULT '', tags_json TEXT DEFAULT '[]', key_points_json TEXT DEFAULT '[]', organize_status TEXT NOT NULL DEFAULT 'pending', organize_method TEXT DEFAULT 'local', organize_error TEXT DEFAULT '', organized_at TEXT DEFAULT '', folder_id TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, content TEXT NOT NULL, page_number INTEGER DEFAULT 1, chunk_index INTEGER DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, chunk_id UNINDEXED, knowledge_id UNINDEXED, filename UNINDEXED, page_number UNINDEXED);
    CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_id TEXT NOT NULL, folder_id TEXT DEFAULT '', title TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, pinned_at TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, sources_json TEXT DEFAULT '[]', created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, sources_json TEXT DEFAULT '[]', trace_json TEXT DEFAULT '[]', reason TEXT DEFAULT '', duration_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS knowledge_suggestions (knowledge_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, questions_json TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(knowledge_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS folder_suggestions (knowledge_id TEXT NOT NULL, folder_id TEXT NOT NULL, fingerprint TEXT NOT NULL, questions_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(knowledge_id, folder_id));
    CREATE TABLE IF NOT EXISTS pay_orders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, out_trade_no TEXT UNIQUE NOT NULL, plan TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', transaction_id TEXT DEFAULT '', prepay_id TEXT DEFAULT '', created_at TEXT NOT NULL, paid_at TEXT DEFAULT '', offer_id TEXT DEFAULT '', product_id TEXT DEFAULT '', wx_order_id TEXT DEFAULT '', attach TEXT DEFAULT '', quantity INTEGER DEFAULT 1, deliver_status TEXT DEFAULT 'pending', delivered_at TEXT DEFAULT '', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS share_cards (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, knowledge_name TEXT DEFAULT '', question TEXT DEFAULT '', answer TEXT NOT NULL, sources_json TEXT DEFAULT '[]', views INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS skills (id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, summary TEXT DEFAULT '', prompt TEXT NOT NULL DEFAULT '', icon TEXT DEFAULT 'skill-node', developer_wechat TEXT DEFAULT '', harness TEXT DEFAULT '', visibility TEXT NOT NULL DEFAULT 'private', source TEXT NOT NULL DEFAULT 'custom', use_count INTEGER NOT NULL DEFAULT 0, like_count INTEGER NOT NULL DEFAULT 0, favorite_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', published_at TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS skill_likes (skill_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(skill_id, user_id), FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS skill_favorites (skill_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(skill_id, user_id), FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE);
    -- 工具产物：harness 在本轮工作区里生成的文件，收进来随消息一起回放
    -- （内容寻址存盘，同一份字节只存一份；document_id 非空表示已同步登记进知识库）
    CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL DEFAULT '', knowledge_id TEXT NOT NULL DEFAULT '', folder_id TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, file_type TEXT NOT NULL DEFAULT '', file_size INTEGER NOT NULL DEFAULT 0, mime TEXT NOT NULL DEFAULT 'application/octet-stream', digest TEXT NOT NULL DEFAULT '', storage_path TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '', document_id TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    -- 运营文案（使用技巧等）：源文件在 content/ 目录，部署时导入这里，运行时只读数据库。
    CREATE TABLE IF NOT EXISTS content_meta (slug TEXT PRIMARY KEY, version TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', subtitle TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '', source_updated_at TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS content_entries (slug TEXT NOT NULL, entry_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', group_title TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '', read_minutes INTEGER NOT NULL DEFAULT 2, body_md TEXT NOT NULL DEFAULT '', blocks_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(slug, entry_id));
    CREATE TABLE IF NOT EXISTS content_assets (slug TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL DEFAULT 'application/octet-stream', size INTEGER NOT NULL DEFAULT 0, bytes BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(slug, name));
    """)
    # Keep existing local databases compatible with the deployable schema.
    for table, columns in {
        'users': {'session_key': "TEXT DEFAULT ''", 'membership': "TEXT NOT NULL DEFAULT 'free'", 'membership_expires_at': "TEXT DEFAULT ''", 'token_version': "INTEGER NOT NULL DEFAULT 0", 'preferences': "TEXT NOT NULL DEFAULT '{}'"},
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
            'pinned': "INTEGER NOT NULL DEFAULT 0",
            'pinned_at': "TEXT DEFAULT ''",
        },
        'content_entries': {
            # 合规文档要在页内显示「更新日期 / 生效日期」，随条目存库；
            # 新建库也靠这段补列（建表之后紧接着执行）。
            'updated_at': "TEXT NOT NULL DEFAULT ''",
            'effective_at': "TEXT NOT NULL DEFAULT ''",
        },
        'messages': {
            # 过程区（思考 / 步骤 / 工具 / 技能 / 子智能体）与耗时随消息落库，
            # 否则重新进入历史对话时只剩正文，思考链路全部丢失。
            'trace_json': "TEXT DEFAULT '[]'",
            'reason': "TEXT DEFAULT ''",
            'duration_ms': "INTEGER NOT NULL DEFAULT 0",
            # 工具产物（生成的文件）也随消息落库：重进对话时文件卡照样在
            'artifacts_json': "TEXT DEFAULT '[]'",
        },
    }.items():
        existing = {row[1] for row in await (await db.execute(f'PRAGMA table_info({table})')).fetchall()}
        for name, declaration in columns.items():
            if name not in existing:
                await db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    await db.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_conversation ON artifacts(conversation_id)")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_orders_wx_order_id ON pay_orders(wx_order_id) WHERE wx_order_id != ''")
    # 内置技能随版本写回技能表：id 固定，老库也不会重复插入
    stamp = datetime.now(timezone.utc).isoformat()
    for skill_id, name, summary, icon, harness, prompt in BUILTIN_SKILLS:
        await db.execute(
            "INSERT OR IGNORE INTO skills(id,user_id,name,summary,prompt,icon,harness,visibility,source,status,published_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (skill_id, '', name, summary, prompt, icon, harness, 'public', 'builtin', 'active', stamp, stamp, stamp),
        )
        await db.execute("UPDATE skills SET name=?, summary=?, prompt=?, icon=?, harness=?, status='active' WHERE id=? AND source='builtin'", (name, summary, prompt, icon, harness, skill_id))
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
