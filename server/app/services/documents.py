import re
from pathlib import Path
from pypdf import PdfReader
from docx import Document as DocxDocument
from PIL import Image
import pytesseract

# 旧版二进制 Office 格式：允许上传，但只能在微信内置渲染器里按原文预览，
# 没有可用的纯 Python 解析器，因此不进入知识库正文与检索。
PREVIEW_ONLY_SUFFIXES = {".doc", ".ppt", ".xls"}

# 可上传的类型：文本与图片做站内解析；PDF / Word / Excel / PPT 另走微信内置渲染器
# 的原文预览，版式、图片、表格、分页按原文件呈现。
ALLOWED_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".csv", ".pptx", ".ppt", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".webp"}

# 表格类文件逐表截断，避免超大工作表把整轮解析拖垮
MAX_SHEET_ROWS = 400


def _pptx_shape_text(shape) -> list[str]:
    """一个形状里的文字：组合形状递归，表格按行拼列，文本框取整段。"""
    parts: list[str] = []
    if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
        for child in shape.shapes:
            parts.extend(_pptx_shape_text(child))
        return parts
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
        return parts
    if getattr(shape, "has_text_frame", False):
        text = shape.text_frame.text.strip()
        if text:
            parts.append(text)
    return parts


def _extract_pptx(path: Path) -> tuple[str, int]:
    """PPT：按页抽文字（含表格、组合形状、演讲者备注），页数用于引用定位。"""
    from pptx import Presentation

    presentation = Presentation(str(path))
    blocks: list[str] = []
    for index, slide in enumerate(presentation.slides, 1):
        lines: list[str] = []
        for shape in slide.shapes:
            lines.extend(_pptx_shape_text(shape))
        if getattr(slide, "has_notes_slide", False):
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"[备注] {notes}")
        if lines:
            blocks.append(f"【第 {index} 页】\n" + "\n".join(lines))
    return "\n\n".join(blocks), max(1, len(presentation.slides._sldIdLst))


def _extract_xlsx(path: Path) -> tuple[str, int]:
    """Excel：每个工作表逐行还原成「单元格 | 单元格」文本，保留多表结构。"""
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if value is None else str(value).strip() for value in row]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    rows.append(" | ".join(cells))
                if len(rows) >= MAX_SHEET_ROWS:
                    break
            if rows:
                blocks.append(f"【{sheet.title}】\n" + "\n".join(rows))
        return "\n\n".join(blocks), max(1, len(workbook.worksheets))
    finally:
        workbook.close()


def extract_text(path: Path, file_type: str) -> tuple[str, int]:
    suffix = file_type.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\f".join((page.extract_text() or "") for page in reader.pages), len(reader.pages)
    if suffix == ".pptx":
        return _extract_pptx(path)
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    if suffix in PREVIEW_ONLY_SUFFIXES:
        # 旧版二进制格式（.doc / .ppt / .xls）没有可用的纯 Python 解析器：
        # 正文留空，不影响上传，阅读走微信内置渲染器按原文预览。
        return "", 1
    if suffix == ".docx":
        doc = DocxDocument(str(path))
        return "\n".join(p.text for p in doc.paragraphs), max(1, len(doc.paragraphs) // 30)
    if suffix in {".txt", ".md", ".markdown", ".csv"}:
        return path.read_text(encoding="utf-8", errors="ignore"), 1
    if suffix == ".html":
        from lxml import html as lxml_html
        raw = path.read_text(encoding="utf-8", errors="ignore")
        root = lxml_html.fromstring(raw)
        text = re.sub(r"\n{3,}", "\n\n", root.text_content()).strip()
        return text, 1
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        with Image.open(path) as image:
            return pytesseract.image_to_string(image, lang="chi_sim+eng"), 1
    raise ValueError("暂不支持该文件类型")


def split_chunks(text: str, size: int = 900) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not normalized:
        return ["文档暂无可提取文本。"]
    return [normalized[i:i + size] for i in range(0, len(normalized), size)]
