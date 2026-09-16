"""部署/更新内容：把 content/ 下的运营文案源文件导入数据库。

用法::

    python scripts/import_content.py                 # 全部内容，指纹没变就跳过
    python scripts/import_content.py --slug legal    # 只导入合规文档
    python scripts/import_content.py --force         # 强制重新导入
    python scripts/import_content.py --check         # 只看库里现在有什么，不写入

生产环境部署流程（在服务器上、启动服务之前执行一次）::

    cd /opt/zhi-reader-api
    . .venv/bin/activate
    python scripts/import_content.py

不执行这一步也能用：服务首次启动发现某个 slug 库里没有内容、而源目录存在时
会自动导入一次（见 app/services/content_import.py 的 ensure_content_imported）。

合规文案（content/legal）里的运营者名称、备案号、联系邮箱来自环境变量，
导入时会替换进正文；没配置会打印告警（不是报错），上线前请按 README 补齐。
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import connect, fetchone, init_db  # noqa: E402
from app.services.content import SLUGS  # noqa: E402
from app.services.content_import import import_content, import_warnings, source_dir  # noqa: E402


async def describe(slug: str) -> dict:
    db = await connect()
    try:
        meta = await fetchone(db, "SELECT * FROM content_meta WHERE slug=?", (slug,))
        entries = await fetchone(db, "SELECT COUNT(*) AS total FROM content_entries WHERE slug=?", (slug,))
        assets = await fetchone(db, "SELECT COUNT(*) AS total, COALESCE(SUM(size),0) AS bytes FROM content_assets WHERE slug=?", (slug,))
    finally:
        await db.close()
    if not meta:
        return {"slug": slug, "imported": False}
    return {
        "slug": slug,
        "imported": True,
        "version": meta["version"],
        "title": meta["title"],
        "entries": int(entries["total"] or 0),
        "assets": int(assets["total"] or 0),
        "asset_bytes": int(assets["bytes"] or 0),
        "imported_at": meta["imported_at"],
    }


def print_state(state: dict) -> None:
    if not state["imported"]:
        print(f"[{state['slug']}] 数据库里还没有内容，执行 python scripts/import_content.py 导入")
        return
    print(
        f"[{state['slug']}] {state['title']}：{state['entries']} 篇正文、{state['assets']} 张配图，"
        f"版本 {state['version']}，导入于 {state['imported_at']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="把 content/ 源文件导入数据库")
    parser.add_argument("--slug", default="", choices=["", *SLUGS], help="只导入指定内容（默认全部）")
    parser.add_argument("--source", default="", help="源目录，默认 content/<slug>")
    parser.add_argument("--force", action="store_true", help="指纹一致时也重新导入")
    parser.add_argument("--check", action="store_true", help="只打印数据库现状，不导入")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list(SLUGS)

    async def run() -> dict:
        await init_db()
        if args.check:
            return {"states": [await describe(slug) for slug in slugs]}
        results = []
        for slug in slugs:
            source = Path(args.source).resolve() if args.source else source_dir(slug)
            if not source.is_dir():
                results.append({"slug": slug, "error": f"源目录不存在：{source}"})
                continue
            try:
                result = await import_content(slug, root=source, force=args.force)
            except Exception as exc:  # 源文件缺失或格式错误
                results.append({"slug": slug, "error": str(exc)})
                continue
            result["state"] = await describe(slug)
            results.append(result)
        return {"results": results, "warnings": import_warnings()}

    payload = asyncio.run(run())
    failed = False

    if "states" in payload:
        for state in payload["states"]:
            print_state(state)
        # --check 是上线前的自检命令，所以也要把待补齐的运营者信息一并列出来
        for warning in import_warnings():
            print(f"[合规待办] {warning}")
        return 0

    for result in payload["results"]:
        if result.get("error"):
            print(f"导入失败（{result['slug']}）：{result['error']}")
            failed = True
            continue
        if result.get("skipped"):
            print(f"[{result['slug']}] 源文件未变化，跳过导入（库里 {result['entries']} 篇正文，版本 {result['version']}）")
        else:
            print(f"[{result['slug']}] 导入完成：{result['entries']} 篇正文、{result['assets']} 张配图，版本 {result['version']}")
        print_state(result["state"])

    for warning in payload["warnings"]:
        print(f"[合规待办] {warning}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
