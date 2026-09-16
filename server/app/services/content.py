"""运营文案服务：使用技巧、合规文档（数据库版）。

存储与部署（2026-09 起）
------------------------
* **源文件**：``content/<slug>/`` 目录里维护清单（manifest.json）、正文
  （entries/*.md）与配图（assets/*.png）。这是内容作者的唯一入口，改文案 = 改文件。
* **运行数据**：部署时由 ``scripts/import_content.py``（或首次启动时的自动导入）
  把源文件写进数据库的 ``content_meta`` / ``content_entries`` / ``content_assets``
  三张表。运行时**只读数据库**，容器里没有 content 目录也能正常展示，
  小程序也不需要跟着发版。
* **可缓存**：``manifest.version`` 是前端缓存失效依据；服务端按
  ``(version, imported_at, 条目数)`` 缓存解析结果，重新导入后自动失效。

目前有两套内容，共用同一套解析与存储：

===========  ==============  ==============================================
slug         业务            接口
===========  ==============  ==============================================
``tips``     使用技巧        ``/api/content/tips``
``legal``    合规文档        ``/api/content/legal``
===========  ==============  ==============================================

合规文档（隐私保护指引 / 用户服务协议 / 软件许可 / 数据管理 / 隐私安全 /
会员服务条款 / 关于）要逐字可追溯，因此：

* 正文同样以 Markdown 源文件维护，导入时一次性解析成 blocks 存库；
* ``body_md``（替换占位符后的原文）也存库，便于日后核对与再解析；
* 运营者身份、备案号、联系邮箱这类「随部署环境变化」的信息写成占位符
  （``{{operator}}`` / ``{{appid}}`` / ``{{email_line}}`` / ``{{icp_line}}``），
  由 ``content_import`` 在导入时用环境变量替换，**不在小程序端拼字符串**。

前端零负担：小程序里没有可靠的富文本组件，因此受限 Markdown 由后端解析成
结构化 blocks（heading / paragraph / step / bullet / note / figure），
前端按 block 类型渲染原生组件，样式完全可控。正文的 blocks 在导入时就解析好
存库，请求时不需要再解析。

支持的 Markdown 子集（故意保持最小）::

    ## 二级标题          -> heading
    普通段落              -> paragraph
    - 项目                -> bullet
    1. 步骤               -> step（序号由后端重排，写错也不会错位）
    > 提示                -> note
    ![说明](图片.png)     -> figure，说明即图注
    **加粗** / `等宽`      -> 段落内的行内样式
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..db import connect, fetchall, fetchone

TIPS_SLUG = "tips"
LEGAL_SLUG = "legal"
SLUGS = (TIPS_SLUG, LEGAL_SLUG)

_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}

_inline_pattern = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
_bullet_pattern = re.compile(r"^[-*]\s+(.*)$")
_step_pattern = re.compile(r"^\d+[.、)]\s+(.*)$")
_figure_pattern = re.compile(r"^!\[(?P<caption>[^\]]*)\]\((?P<src>[^)\s]+)\)$")

# 解析结果缓存：按 slug 各存一份，key 由数据库里的版本信息算出，重新导入即失效。
_caches: dict[str, dict[str, Any]] = {}


class ContentError(RuntimeError):
    """内容缺失或格式不合法。"""


def allowed_asset(name: str) -> bool:
    """资源名必须是「目录内的单个文件名 + 允许的后缀」，避免路径穿越。"""
    safe = (name or "").strip()
    if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
        return False
    return safe.rsplit(".", 1)[-1].lower() in {item.lstrip(".") for item in _ALLOWED_SUFFIXES}


def asset_url(name: str) -> str:
    return f"/api/content/assets/{name}"


def _cache(slug: str) -> dict[str, Any]:
    """取某个 slug 的解析缓存槽：两套内容互不影响。"""
    return _caches.setdefault(slug, {"key": None, "index": None, "bodies": {}})


# ---------------------------------------------------------------------------
# 行内样式
# ---------------------------------------------------------------------------

def _runs(text: str) -> list[dict[str, str]]:
    """把 ``**加粗**`` 与 ``` `等宽` ``` 拆成前端可直接渲染的片段。"""
    runs: list[dict[str, str]] = []
    for piece in _inline_pattern.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            runs.append({"text": piece[2:-2], "style": "strong"})
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            runs.append({"text": piece[1:-1], "style": "code"})
        else:
            runs.append({"text": piece, "style": "plain"})
    return runs or [{"text": text, "style": "plain"}]


# ---------------------------------------------------------------------------
# 正文解析（导入时执行一次，结果存库）
# ---------------------------------------------------------------------------

def parse_markdown(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []
    step_index = 0

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append({"type": "paragraph", "runs": _runs(" ".join(paragraph))})
            paragraph = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue

        figure = _figure_pattern.match(line)
        if figure:
            flush()
            blocks.append(
                {
                    "type": "figure",
                    "src": asset_url(figure.group("src").strip().rsplit("/", 1)[-1]),
                    "caption": figure.group("caption").strip(),
                }
            )
            continue

        if line.startswith("### "):
            flush()
            blocks.append({"type": "heading", "level": 3, "text": line[4:].strip()})
            continue
        if line.startswith("## "):
            flush()
            # 小标题即新的一节：有序步骤重新从 1 开始，避免跨节连号
            step_index = 0
            blocks.append({"type": "heading", "level": 2, "text": line[3:].strip()})
            continue
        if line.startswith("# "):
            flush()
            step_index = 0
            blocks.append({"type": "heading", "level": 2, "text": line[2:].strip()})
            continue
        if line.startswith("> "):
            flush()
            blocks.append({"type": "note", "runs": _runs(line[2:].strip())})
            continue

        bullet = _bullet_pattern.match(line)
        if bullet:
            flush()
            blocks.append({"type": "bullet", "runs": _runs(bullet.group(1).strip())})
            continue

        step = _step_pattern.match(line)
        if step:
            flush()
            step_index += 1
            blocks.append({"type": "step", "index": step_index, "runs": _runs(step.group(1).strip())})
            continue

        paragraph.append(line)

    flush()
    return blocks


# ---------------------------------------------------------------------------
# 读取（全部走数据库）
# ---------------------------------------------------------------------------

def _decode(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default


async def _meta(slug: str) -> dict[str, Any] | None:
    db = await connect()
    try:
        row = await fetchone(db, "SELECT * FROM content_meta WHERE slug=?", (slug,))
        count = await fetchone(db, "SELECT COUNT(*) AS total FROM content_entries WHERE slug=?", (slug,))
    finally:
        await db.close()
    if not row:
        return None
    value = dict(row)
    value["entries"] = int(count["total"] or 0) if count else 0
    return value


def _cache_key(meta: dict[str, Any], slug: str) -> tuple:
    return (slug, meta.get("version") or "", meta.get("imported_at") or "", meta.get("entries") or 0)


async def _load_index(meta: dict[str, Any], slug: str) -> dict[str, Any]:
    db = await connect()
    try:
        rows = await fetchall(
            db,
            "SELECT entry_id,group_id,group_title,title,summary,icon,cover,read_minutes,updated_at,effective_at "
            "FROM content_entries WHERE slug=? ORDER BY sort_order, entry_id",
            (slug,),
        )
    finally:
        await db.close()
    groups: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = row["group_id"] or ""
        group = by_id.get(group_id)
        if group is None:
            group = {"id": group_id, "title": row["group_title"] or "", "entries": []}
            by_id[group_id] = group
            groups.append(group)
        cover = (row["cover"] or "").strip()
        group["entries"].append(
            {
                "id": row["entry_id"],
                "title": row["title"] or "",
                "summary": row["summary"] or "",
                "icon": row["icon"] or "dengpao",
                "cover": asset_url(cover) if cover and allowed_asset(cover) else "",
                "read_minutes": int(row["read_minutes"] or 2),
                "updated_at": row["updated_at"] or "",
                "effective_at": row["effective_at"] or "",
            }
        )
    return {
        "schema": 1,
        "version": meta.get("version") or "",
        "title": meta.get("title") or "",
        "subtitle": meta.get("subtitle") or "",
        "intro": meta.get("intro") or "",
        "updated_at": meta.get("source_updated_at") or "",
        "groups": [group for group in groups if group["entries"]],
    }


async def _cache_state(slug: str) -> dict[str, Any]:
    meta = await _meta(slug)
    if not meta or not meta["entries"]:
        raise ContentError(f"{slug} 内容尚未导入数据库（部署时执行 scripts/import_content.py）")
    cache = _cache(slug)
    key = _cache_key(meta, slug)
    if cache["key"] != key or cache["index"] is None:
        index = await _load_index(meta, slug)
        cache.update({"key": key, "index": index, "bodies": {}})
    return cache["index"]


async def content_index(slug: str) -> dict[str, Any]:
    return await _cache_state(slug)


async def content_entry(slug: str, entry_id: str) -> dict[str, Any] | None:
    index = await _cache_state(slug)
    match = None
    for group in index["groups"]:
        for entry in group["entries"]:
            if entry["id"] == entry_id:
                match = (group, entry)
                break
        if match:
            break
    if not match:
        return None
    group, entry = match
    bodies: dict[str, Any] = _cache(slug)["bodies"]
    if entry_id not in bodies:
        db = await connect()
        try:
            row = await fetchone(db, "SELECT blocks_json FROM content_entries WHERE slug=? AND entry_id=?", (slug, entry_id))
        finally:
            await db.close()
        bodies[entry_id] = _decode(row["blocks_json"], []) if row else []
    return {**entry, "group": group["title"], "blocks": bodies[entry_id]}


async def tips_index() -> dict[str, Any]:
    return await content_index(TIPS_SLUG)


async def tip_entry(entry_id: str) -> dict[str, Any] | None:
    return await content_entry(TIPS_SLUG, entry_id)


async def legal_index() -> dict[str, Any]:
    """合规文档清单：扁平数组，顺序即展示顺序（关于、数据管理、隐私、协议…）。"""
    index = await content_index(LEGAL_SLUG)
    docs = [entry for group in index["groups"] for entry in group["entries"]]
    return {
        "schema": 1,
        "version": index["version"],
        "title": index["title"] or "服务说明",
        "subtitle": index["subtitle"],
        "updated_at": index["updated_at"],
        "docs": [
            {key: doc[key] for key in ("id", "title", "summary", "updated_at", "effective_at")}
            for doc in docs
        ],
    }


async def legal_doc(doc_id: str) -> dict[str, Any] | None:
    return await content_entry(LEGAL_SLUG, doc_id)


async def asset(name: str, slug: str = TIPS_SLUG) -> dict[str, Any] | None:
    """按文件名取配图（二进制存库，匿名可访问）。"""
    if not allowed_asset(name):
        return None
    db = await connect()
    try:
        row = await fetchone(db, "SELECT name,mime,size,bytes,updated_at FROM content_assets WHERE slug=? AND name=?", (slug, name))
    finally:
        await db.close()
    if not row:
        return None
    return {"name": row["name"], "mime": row["mime"] or "application/octet-stream", "size": int(row["size"] or 0), "bytes": bytes(row["bytes"]), "updated_at": row["updated_at"] or ""}
