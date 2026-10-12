"""Document text extraction — free, self-hosted (handoff 2026-10-12, D9).

PDFs: the text layer first (pdfplumber). A page with fewer than
EXTRACT_MIN_TEXT_CHARS non-space characters is treated as scanned: it is
rendered at OCR_DPI (pypdfium2) and read by Tesseract (`eng+spa`, `--psm 6`).
Images (PNG / JPEG / TIFF / HEIC) are OCR'd directly; HEIC is converted to RGB
first. Tables found by pdfplumber are returned per page, because invoice line
items are usually tables.

Rules the rest of the service already keeps, kept here too:
  - the file type is judged by CONTENT (magic bytes), never by the name;
  - no paid AI — Tesseract is the only reader;
  - the file lives only in the caller's temp dir and is deleted by the caller;
  - nothing from the document is logged, only counts.

Every call runs inside MODEL_LOCK (app/transcriber.py), so OCR never runs at
the same time as a transcription and CPU/memory stay flat.
"""

import logging
import time
from dataclasses import dataclass

from .transcriber import MODEL_LOCK

log = logging.getLogger("speech.extract")


class ExtractError(Exception):
    """A problem with the caller's document, mapped straight to an HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class ExtractSettings:
    max_pages: int = 40
    ocr_langs: str = "eng+spa"
    ocr_dpi: int = 300
    min_text_chars: int = 40


UNSUPPORTED = ExtractError(415, "Unsupported document type. Allowed: PDF, PNG, JPEG, TIFF, HEIC")
HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heif")


def sniff_kind(path: str) -> str | None:
    """'pdf' | 'image' | 'heic' by the first bytes, or None."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS:
        return "heic"
    return None


def _non_space(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


_tesseract_version: str | None = None


def tesseract_version() -> str:
    """The installed Tesseract version, cached; 'unavailable' when it is not installed
    (the text-layer path still works without it)."""
    global _tesseract_version
    if _tesseract_version is None:
        try:
            import pytesseract

            _tesseract_version = str(pytesseract.get_tesseract_version())
        except Exception:  # noqa: BLE001 - absent binary, odd version strings
            _tesseract_version = "unavailable"
    return _tesseract_version


def ocr_image(image, settings: ExtractSettings) -> str:
    import pytesseract

    if tesseract_version() == "unavailable":
        raise ExtractError(503, "OCR is not available on this service (tesseract missing)")
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return pytesseract.image_to_string(image, lang=settings.ocr_langs, config="--psm 6")


def render_pdf_page(path: str, index: int, dpi: int):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        page = pdf[index]
        try:
            return page.render(scale=dpi / 72).to_pil()
        finally:
            page.close()
    finally:
        pdf.close()


def _clean_table(table) -> list[list[str]]:
    return [["" if c is None else str(c).strip() for c in row] for row in (table or []) if row]


def extract_pdf(path: str, settings: ExtractSettings) -> list[dict]:
    import pdfplumber

    pages: list[dict] = []
    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # noqa: BLE001 - encrypted / corrupt
        raise ExtractError(415, "PDF could not be opened") from exc
    with pdf:
        count = len(pdf.pages)
        if count > settings.max_pages:
            raise ExtractError(413, f"PDF has {count} pages; the limit is {settings.max_pages}")
        for i, page in enumerate(pdf.pages):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                text = ""
            try:
                tables = [_clean_table(t) for t in (page.extract_tables() or [])]
            except Exception:  # noqa: BLE001
                tables = []
            method = "text"
            if _non_space(text) < settings.min_text_chars:
                image = render_pdf_page(path, i, settings.ocr_dpi)
                text = ocr_image(image, settings)
                method = "ocr"
            pages.append({"page": i + 1, "method": method, "chars": _non_space(text), "text": text, "tables": tables})
    return pages


def open_image(path: str, kind: str):
    from PIL import Image

    if kind == "heic":
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError as exc:
            raise ExtractError(415, "HEIC support is not installed") from exc
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise UNSUPPORTED from exc
    return image


def extract_document(path: str, settings: ExtractSettings) -> dict:
    """The whole thing: sniff → pages → one response dict. Runs under MODEL_LOCK."""
    started = time.monotonic()
    kind = sniff_kind(path)
    if kind is None:
        raise UNSUPPORTED
    with MODEL_LOCK:
        if kind == "pdf":
            pages = extract_pdf(path, settings)
        else:
            image = open_image(path, kind)
            text = ocr_image(image, settings)
            pages = [{"page": 1, "method": "ocr", "chars": _non_space(text), "text": text, "tables": []}]
    ocr_pages = sum(1 for p in pages if p["method"] == "ocr")
    text = "\n\n".join(p["text"] for p in pages if p["text"])
    return {
        "text": text,
        "pages": pages,
        "page_count": len(pages),
        "ocr_pages": ocr_pages,
        "engine": f"pdfplumber+tesseract-{tesseract_version()}",
        "seconds": round(time.monotonic() - started, 3),
    }
