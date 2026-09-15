import re
from pathlib import Path
from pypdf import PdfReader
from docx import Document as DocxDocument
from PIL import Image
import pytesseract


def extract_text(path: Path, file_type: str) -> tuple[str, int]:
    suffix = file_type.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\f".join((page.extract_text() or "") for page in reader.pages), len(reader.pages)
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
