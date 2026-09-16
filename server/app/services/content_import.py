"""把运营文案源文件导入数据库（部署时执行一次，可重复执行）。

为什么要有这一步
----------------
内容（清单 / 正文 / 配图）在仓库里是源文件，方便改文案和评审；运行时统一从
数据库读，容器不挂 content 目录也能正常展示，小程序也不用跟着发版。
部署流程：``python scripts/import_content.py``，或者直接启动服务——首次启动
发现库里没有内容时会自动导入一次（见 ``ensure_content_imported``）。

两套内容共用这套流程：

===========  ==============  ==========================
slug         源目录          接口
===========  ==============  ==========================
``tips``     content/tips    使用技巧
``legal``    content/legal   合规文档
===========  ==============  ==========================

合规文案里的占位符
------------------
运营者名称、备案号、联系邮箱随部署环境变化，因此源文件里写成占位符，
导入时用环境变量替换（只在这里替换一次，之后库里存的就是最终文案）：

========================  ==============================  ============================
占位符                    来源                            留空时的行为
========================  ==============================  ============================
``{{operator}}``          ``LEGAL_OPERATOR_NAME``         退化成通用表述并打印告警
``{{appid}}``             ``WECHAT_APPID``                退回内置的正式 AppID
``{{email_line}}``        ``LEGAL_CONTACT_EMAIL``         改成「小程序内联系客服」表述
``{{icp_line}}``          ``LEGAL_ICP_NUMBER``            整行消失（不写备案号）
========================  ==============================  ============================

幂等性：按「清单 + 全部正文 + 全部配图」算一个指纹存进 ``content_meta``。
指纹没变就直接跳过（不动库、不重传图片）；``--force`` 可强制重导。

一致性：导入在同一个事务里完成——先删掉该 slug 的历史条目与配图，再整批写入，
因此清单里删掉的文章/图片不会残留在库里（不会被旧版本读到）。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import connect, fetchone
from .content import LEGAL_SLUG, SLUGS, TIPS_SLUG, ContentError, allowed_asset, parse_markdown

# 源文件里形如 {{operator}} 的占位符；导入时必须全部替换掉，写错会直接报错，
# 避免把「{{xxx}}」这种半成品文案发到线上。
_placeholder_pattern = re.compile(r"\{\{([a-z_]+)\}\}")


def source_dir(slug: str) -> Path:
    return settings.content_path / slug


def tips_source_dir() -> Path:
    """兼容旧调用：使用技巧的源目录。"""
    return source_dir(TIPS_SLUG)


def placeholder_values() -> dict[str, str]:
    """合规文案的替换值：只在 .env 维护，源文件里不出现真实身份信息。"""
    email = settings.legal_contact_email.strip()
    icp = settings.legal_icp_number.strip()
    return {
        "operator": settings.legal_operator_name.strip() or "cola 知识库小程序开发者（个人开发者）",
        "appid": settings.wechat_appid.strip() or "wx64ec0013d8af77c9",
        "email_line": (
            f"你也可以发送邮件至 {email}，我们会在收到后及时处理。"
            if email
            else "你也可以通过小程序内的「联系客服」会话与我们沟通，我们会在收到后及时处理。"
        ),
        "icp_line": f"本小程序已完成 ICP 备案，备案号：{icp}。" if icp else "",
    }


def apply_placeholders(text: str, values: dict[str, str] | None = None) -> str:
    """替换占位符；出现未知占位符直接报错，不静默留下 {{xxx}}。"""
    table = placeholder_values() if values is None else values

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in table:
            raise ContentError(f"正文里有未知占位符：{{{{{key}}}}}")
        return table[key]

    return _placeholder_pattern.sub(replace, text)


def _fingerprint(manifest_bytes: bytes, bodies: list[tuple[str, bytes]], assets: list[tuple[str, bytes]], mapping: dict[str, str]) -> str:
    digest = hashlib.sha1()
    digest.update(manifest_bytes)
    # 身份信息变了也要重导，否则库里的文案会停在旧运营者名称上
    for key, value in sorted(mapping.items()):
        digest.update(f"{key}={value}".encode("utf-8"))
    for name, payload in sorted(bodies):
        digest.update(name.encode("utf-8"))
        digest.update(payload)
    for name, payload in sorted(assets):
        digest.update(name.encode("utf-8"))
        digest.update(hashlib.sha1(payload).digest())
    return digest.hexdigest()


def _collect(root: Path) -> dict[str, Any]:
    """读取源目录：清单 + 正文 + 配图，顺带算出指纹。"""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ContentError(f"文案源文件缺失：{manifest_path}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContentError(f"文案清单格式错误：{exc}") from exc

    mapping = placeholder_values()
    entries: list[dict[str, Any]] = []
    bodies: list[tuple[str, bytes]] = []
    order = 0
    for group in manifest.get("groups", []):
        group_id = str(group.get("id") or "")
        group_title = str(group.get("title") or "")
        for entry in group.get("entries", []):
            entry_id = str(entry.get("id") or "").strip()
            relative = str(entry.get("body") or "").strip()
            if not entry_id or not relative:
                continue
            target = (root / relative).resolve()
            # 只允许读源目录内的正文，避免清单被写成相对路径穿越
            if root.resolve() not in target.parents or not target.is_file():
                raise ContentError(f"正文文件不存在或越权：{relative}")
            text = apply_placeholders(target.read_text(encoding="utf-8"), mapping)
            bodies.append((entry_id, text.encode("utf-8")))
            cover = str(entry.get("cover") or "").strip()
            order += 1
            entries.append({
                "entry_id": entry_id,
                "group_id": group_id,
                "group_title": group_title,
                "sort_order": order,
                "title": str(entry.get("title") or ""),
                "summary": str(entry.get("summary") or ""),
                "icon": str(entry.get("icon") or "dengpao"),
                "cover": cover if cover and allowed_asset(cover) else "",
                "read_minutes": int(entry.get("read_minutes") or 2),
                "updated_at": str(entry.get("updated_at") or ""),
                "effective_at": str(entry.get("effective_at") or ""),
                "body_md": text,
                # 解析一次存库：请求时不再解析 Markdown
                "blocks_json": json.dumps(parse_markdown(text), ensure_ascii=False),
            })

    assets: list[dict[str, Any]] = []
    assets_dir = root / "assets"
    if assets_dir.is_dir():
        for path in sorted(assets_dir.iterdir()):
            if not path.is_file() or not allowed_asset(path.name):
                continue
            payload = path.read_bytes()
            assets.append({
                "name": path.name,
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "size": len(payload),
                "bytes": payload,
            })

    available = {item["name"] for item in assets}
    for entry in entries:
        if entry["cover"] and entry["cover"] not in available:
            entry["cover"] = ""
    if not entries:
        raise ContentError("文案清单里没有任何条目")

    return {
        "meta": {
            "version": str(manifest.get("version") or ""),
            "fingerprint": _fingerprint(manifest_bytes, bodies, [(item["name"], item["bytes"]) for item in assets], mapping),
            "title": str(manifest.get("title") or ""),
            "subtitle": str(manifest.get("subtitle") or ""),
            "intro": str(manifest.get("intro") or ""),
            "source_updated_at": str(manifest.get("updated_at") or ""),
        },
        "entries": entries,
        "assets": assets,
    }


async def import_content(slug: str, *, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """把某个 slug 的源目录导入数据库；指纹一致且不强制时直接跳过。"""
    source = root or source_dir(slug)
    collected = _collect(source)
    meta = collected["meta"]
    entries = collected["entries"]
    assets = collected["assets"]

    db = await connect()
    try:
        row = await fetchone(db, "SELECT fingerprint FROM content_meta WHERE slug=?", (slug,))
        count = await fetchone(db, "SELECT COUNT(*) AS total FROM content_entries WHERE slug=?", (slug,))
        stored = int(count["total"] or 0) if count else 0
        if not force and row and stored and (row["fingerprint"] or "") == meta["fingerprint"]:
            return {
                "slug": slug, "skipped": True, "entries": stored,
                "assets": len(assets), "version": meta["version"], "fingerprint": meta["fingerprint"],
            }

        stamp = datetime.now(timezone.utc).isoformat()
        await db.execute("DELETE FROM content_entries WHERE slug=?", (slug,))
        await db.execute("DELETE FROM content_assets WHERE slug=?", (slug,))
        await db.execute(
            "INSERT INTO content_meta(slug,version,fingerprint,title,subtitle,intro,source_updated_at,imported_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET version=excluded.version,"
            "fingerprint=excluded.fingerprint,title=excluded.title,subtitle=excluded.subtitle,intro=excluded.intro,"
            "source_updated_at=excluded.source_updated_at,imported_at=excluded.imported_at",
            (slug, meta["version"], meta["fingerprint"], meta["title"], meta["subtitle"], meta["intro"], meta["source_updated_at"], stamp),
        )
        for entry in entries:
            await db.execute(
                "INSERT INTO content_entries(slug,entry_id,group_id,group_title,sort_order,title,summary,icon,cover,read_minutes,updated_at,effective_at,body_md,blocks_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    slug, entry["entry_id"], entry["group_id"], entry["group_title"], entry["sort_order"],
                    entry["title"], entry["summary"], entry["icon"], entry["cover"], entry["read_minutes"],
                    entry["updated_at"], entry["effective_at"], entry["body_md"], entry["blocks_json"],
                ),
            )
        for asset in assets:
            await db.execute(
                "INSERT INTO content_assets(slug,name,mime,size,bytes,updated_at) VALUES(?,?,?,?,?,?)",
                (slug, asset["name"], asset["mime"], asset["size"], asset["bytes"], stamp),
            )
        await db.commit()
    finally:
        await db.close()

    return {
        "slug": slug, "skipped": False, "entries": len(entries), "assets": len(assets),
        "version": meta["version"], "fingerprint": meta["fingerprint"],
    }


async def import_tips(*, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """兼容旧调用：导入使用技巧。"""
    return await import_content(TIPS_SLUG, root=root, force=force)


def import_warnings() -> list[str]:
    """上线前必须补齐的身份信息，导入时打印出来，避免「悄悄用通用表述」。"""
    warnings: list[str] = []
    if not settings.legal_operator_name.strip():
        warnings.append("LEGAL_OPERATOR_NAME 未设置：合规文档里的运营者会显示为通用表述，上线前请填真实运营者名称。")
    if not settings.legal_contact_email.strip():
        warnings.append("LEGAL_CONTACT_EMAIL 未设置：合规文档只提供「小程序内联系客服」渠道。")
    if not settings.legal_icp_number.strip():
        warnings.append("LEGAL_ICP_NUMBER 未设置：合规文档不展示 ICP 备案号。")
    return warnings


async def ensure_content_imported() -> list[dict[str, Any]]:
    """首次启动的兜底导入：某个 slug 库里没内容、而源目录在时导入一次。

    部署时既可以用 ``scripts/import_content.py`` 显式导入，也可以只启动服务；
    两条路径都会把「源文件 → 数据库」这件事做完，且可重复执行。
    """
    imported: list[dict[str, Any]] = []
    for slug in SLUGS:
        source = source_dir(slug)
        if not source.is_dir():
            continue
        db = await connect()
        try:
            count = await fetchone(db, "SELECT COUNT(*) AS total FROM content_entries WHERE slug=?", (slug,))
            stored = int(count["total"] or 0) if count else 0
        finally:
            await db.close()
        if stored:
            continue
        imported.append(await import_content(slug, root=source))
    return imported
