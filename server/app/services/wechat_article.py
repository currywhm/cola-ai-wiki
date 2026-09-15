"""微信公众号文章抓取与清洗。

抓取 mp.weixin.qq.com 文章页，保留正文的结构（标题/段落/列表/引用/代码块）与图片：
- 图片下载到本地资源目录，src 重写为后端资源地址，保证原文图片不丢失；
- 输出两个产物：供小程序 rich-text 渲染的精简 HTML、供检索切片的纯文本。
"""
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from lxml import html as lxml_html
from lxml.html import HtmlElement

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47"
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg", "image/x-icon": ".ico"}

# rich-text 支持的标签集合（section 等容器统一转 div）
TAG_REWRITE = {"section": "div", "figure": "div", "figcaption": "p", "article": "div", "main": "div", "aside": "div", "header": "div", "footer": "div", "b": "strong", "i": "em"}
ALLOWED_TAGS = {"div", "p", "span", "strong", "em", "u", "s", "del", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "img", "br", "hr", "code", "table", "thead", "tbody", "tr", "th", "td", "a", "sup", "sub"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "form", "input", "button", "select", "textarea", "audio", "video", "canvas", "svg", "link", "meta"}
KEEP_ATTRS = {"style"}


class ArticleFetchError(RuntimeError):
    pass


def _safe_style(style: str) -> str:
    """过滤危险 CSS，并剥离公众号的懒加载隐藏样式（visibility/opacity）。"""
    if not style:
        return ""
    if re.search(r"expression|javascript:|url\s*\(", style, re.I):
        return ""
    declarations = []
    for part in style.split(";"):
        prop = part.split(":", 1)[0].strip().lower()
        if prop in {"visibility", "opacity"}:
            continue  # 公众号用 JS 控制显示，去掉后 rich-text 才能正常渲染
        if part.strip():
            declarations.append(part.strip())
    return ";".join(declarations)


def _text_with_code(element: HtmlElement) -> str:
    """提取节点文本，pre/code 内容原样保留换行。"""
    parts: list[str] = []

    def walk(node: HtmlElement) -> None:
        tag = node.tag if isinstance(node.tag, str) else ""
        if tag in DROP_WITH_CONTENT:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "pre", "section"}:
            parts.append("\n")
        if tag == "br":
            parts.append("\n")

    walk(element)
    text = "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _Sanitizer:
    def __init__(self, assets_dir: Path, asset_url_prefix: str, client: httpx.AsyncClient):
        self.assets_dir = assets_dir
        self.asset_url_prefix = asset_url_prefix
        self.client = client
        self.image_count = 0

    async def _download_image(self, url: str) -> str | None:
        """下载图片到本地，成功返回资源 URL，失败返回 None（保留原链接占位）。"""
        if not url.startswith(("http://", "https://")):
            return None
        try:
            resp = await self.client.get(url, timeout=20, follow_redirects=True)
            if resp.status_code != 200 or len(resp.content) > MAX_IMAGE_BYTES:
                return None
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            ext = ALLOWED_IMAGE_TYPES.get(content_type) or Path(urlparse(url).path).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"}:
                ext = ".jpg"
            self.image_count += 1
            name = f"img_{self.image_count}{ext}"
            (self.assets_dir / name).write_bytes(resp.content)
            return f"{self.asset_url_prefix}/{name}"
        except (httpx.HTTPError, OSError):
            return None

    async def sanitize(self, node: HtmlElement) -> HtmlElement | None:
        """递归清洗节点，返回安全的新节点（lxml 元素）。"""
        tag = node.tag if isinstance(node.tag, str) else ""
        if not tag:
            return None
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            return None
        tag = TAG_REWRITE.get(tag, tag)
        if tag == "pre":
            return await self._code_block(node)
        if tag not in ALLOWED_TAGS:
            # 不认识的容器：只保留其子节点（由调用方处理），这里返回 div 包裹
            tag = "div"
        clean = lxml_html.Element(tag)
        if tag == "img":
            src = node.get("data-src") or node.get("src") or ""
            local = await self._download_image(src)
            clean.set("src", local or src)
            clean.set("style", "max-width:100%;height:auto;display:block;margin:16rpx auto;")
            return clean
        if tag == "a":
            href = node.get("href", "")
            if href.startswith(("http://", "https://")):
                clean.set("href", href)
        style = _safe_style(node.get("style", ""))
        if style and tag != "img":
            clean.set("style", style)
        if node.text:
            clean.text = node.text
        for child in node:
            child_clean = await self.sanitize(child)
            if child_clean is not None:
                clean.append(child_clean)
            if child.tail:
                if len(clean):
                    last = clean[-1]
                    last.tail = (last.tail or "") + child.tail
                else:
                    clean.text = (clean.text or "") + child.tail
        return clean

    async def _code_block(self, node: HtmlElement) -> HtmlElement:
        """pre 不在 rich-text 白名单内，转为等宽字体的 div，换行用 br 保留代码格式。"""
        block = lxml_html.Element("div")
        block.set("style", "background:#f5f6f7;border-radius:12rpx;padding:20rpx 24rpx;margin:20rpx 0;font-family:Menlo,Consolas,monospace;font-size:24rpx;line-height:1.6;white-space:pre-wrap;word-break:break-all;")
        code_text = node.text_content()
        block.text = code_text.rstrip("\n")
        return block


async def fetch_wechat_article(url: str, assets_dir: Path, asset_url_prefix: str) -> dict:
    """抓取公众号文章，返回 {title, account, html, text, image_count}。"""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc.endswith("mp.weixin.qq.com"):
        raise ArticleFetchError("仅支持微信公众号文章链接（mp.weixin.qq.com）")
    assets_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Referer": "https://mp.weixin.qq.com/"}, follow_redirects=True, timeout=20) as client:
        try:
            resp = await client.get(url.strip())
        except httpx.HTTPError as exc:
            raise ArticleFetchError(f"文章抓取失败：{exc}") from exc
        if resp.status_code != 200:
            raise ArticleFetchError(f"文章抓取失败（HTTP {resp.status_code}），链接可能已失效")
        doc = lxml_html.fromstring(resp.content)
        content = doc.find('.//*[@id="js_content"]')
        if content is None:
            raise ArticleFetchError("未找到文章正文，可能已被删除或需要微信环境访问")
        title_el = doc.find('.//*[@id="activity-name"]')
        title = (title_el.text_content().strip() if title_el is not None else "") or "公众号文章"
        account_el = doc.find('.//*[@id="js_name"]')
        account = account_el.text_content().strip() if account_el is not None else ""
        sanitizer = _Sanitizer(assets_dir, asset_url_prefix, client)
        clean_root = await sanitizer.sanitize(content)
        if clean_root is None:
            raise ArticleFetchError("正文解析失败")
        body_html = lxml_html.tostring(clean_root, encoding="unicode", method="html")
        text = _text_with_code(content)
        if len(text) < 20:
            raise ArticleFetchError("正文内容过少，可能不是有效的公众号文章")
    return {"title": title[:120], "account": account, "html": body_html, "text": text, "image_count": sanitizer.image_count}


def build_document_html(title: str, account: str, body_html: str, source_url: str) -> str:
    """组装落盘的完整 HTML 文档（带标题与来源行）。"""
    head = (
        f'<h1 style="font-size:40rpx;font-weight:700;line-height:1.4;margin:0 0 16rpx;">{title}</h1>'
        f'<p style="color:#8a8f99;font-size:24rpx;margin:0 0 32rpx;">{account} · 来源：{source_url}</p>'
    )
    return head + body_html
