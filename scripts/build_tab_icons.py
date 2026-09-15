"""Generate WeChat tab PNG assets from iconfont collection 19238.

The source collection is:
https://www.iconfont.cn/collections/detail?cid=19238

This keeps the bottom tab bar aligned with the in-page icon system.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "miniprogram" / "assets"
COLLECTION_API = "https://www.iconfont.cn/api/collection/detail.json?id=19238"
REFERER = "https://www.iconfont.cn/collections/detail?cid=19238"

TABS = [
    ("tab-0", "home"),
    ("tab-1", "folder"),
    ("tab-2", "message-comments"),
    ("tab-3", "customer"),
]

COLORS = {
    "normal": "#7B8094",
    "active": "#C45129",
}


def load_iconfont_collection() -> dict[str, str]:
    request = urllib.request.Request(
        COLLECTION_API,
        headers={"Referer": REFERER, "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return {item["name"]: item["show_svg"] for item in payload["data"]["icons"]}


def clean_svg(raw: str, color: str, size: int = 72) -> str:
    viewbox = re.search(r'viewBox="([^"]+)"', raw).group(1)
    body = re.sub(r"^.*?<svg[^>]*>", "", raw, flags=re.S)
    body = re.sub(r"</svg>.*$", "", body, flags=re.S)
    body = re.sub(r'\s*(?:p-id|class|style|version)="[^"]*"', "", body)
    body = re.sub(r'\s*fill="[^"]*"', "", body)
    body = re.sub(r"<(path|circle|rect|polygon)\b", rf'<\1 fill="{color}"', body)
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="{viewbox}">{body}</svg>\n'


def main() -> None:
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    icons = load_iconfont_collection()
    with tempfile.TemporaryDirectory() as workdir:
        temp_root = Path(workdir)
        for basename, icon_name in TABS:
            for state, color in COLORS.items():
                svg_path = temp_root / f"{basename}-{state}.svg"
                png_path = temp_root / f"{basename}-{state}.png"
                svg_path.write_text(clean_svg(icons[icon_name], color), encoding="utf-8")
                subprocess.run(
                    ["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                dest = ASSET_ROOT / f"{basename}{'-active' if state == 'active' else ''}.png"
                shutil.copyfile(png_path, dest)


if __name__ == "__main__":
    main()
