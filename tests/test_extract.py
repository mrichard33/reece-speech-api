"""/v1/extract and kind=extract jobs (2026-10-12).

Fixtures are generated in the test — no documents are committed:
  - a text-layer PDF, hand-written with the standard Helvetica font (pdfplumber
    reads it without any font embedding),
  - a "scanned" PDF and a PNG: an image with large text, saved by Pillow,
  - a 41-page PDF for the page cap, and a text file named .pdf for the type check.
OCR assertions run only where Tesseract is installed (CI installs it); the
text-layer, type, size and auth checks run everywhere.
"""

import shutil

import pytest

from app import extract as extract_mod
from tests.conftest import AUTH, wait_for_job

HAS_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract is not installed here")


def make_text_pdf(path, lines, pages=1):
    """A minimal valid PDF with one text block per page, standard Helvetica (no embedding)."""
    objects = []  # list of bytes, 1-based object numbers

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    pages_id = len(objects) + 1 + 2 * pages  # reserved: the Pages object comes after every page + stream
    for _ in range(pages):
        text_ops = b"BT /F1 14 Tf 72 720 Td 18 TL " + b" ".join(
            b"(" + ln.encode("latin-1").replace(b"(", b"\\(").replace(b")", b"\\)") + b") Tj T*" for ln in lines
        ) + b" ET"
        stream = add(b"<< /Length " + str(len(text_ops)).encode() + b" >>\nstream\n" + text_ops + b"\nendstream")
        page = add(b"<< /Type /Page /Parent " + str(pages_id).encode() + b" 0 R /MediaBox [0 0 612 792] "
                   b"/Resources << /Font << /F1 " + str(font).encode() + b" 0 R >> >> /Contents " + str(stream).encode() + b" 0 R >>")
        page_ids.append(page)
    kids = b" ".join(str(p).encode() + b" 0 R" for p in page_ids)
    real_pages_id = add(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>")
    assert real_pages_id == pages_id
    catalog = add(b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root " + str(catalog).encode() + b" 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    path.write_bytes(bytes(out))
    return path


def make_text_image(path, lines, fmt="PNG"):
    """A white page with big black text, the kind Tesseract reads reliably."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1600, 800), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    y = 80
    for ln in lines:
        draw.text((80, y), ln, fill="black", font=font)
        y += 110
    img.save(path, fmt)
    return path


def post_file(client, path, route="/v1/extract", field="file", **data):
    with open(path, "rb") as f:
        return client.post(route, headers=AUTH, files={field: (path.name, f)}, data=data)


def test_text_layer_pdf_is_read_without_ocr(build_client, tmp_path):
    client = build_client()
    pdf = make_text_pdf(tmp_path / "invoice.pdf", ["Lead Gurus LLC", "Invoice # LG-2026-0912", "Service Period: 09/01/2026 - 09/30/2026", "Amount Due $53,080.11"])
    r = post_file(client, pdf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page_count"] == 1
    assert body["ocr_pages"] == 0
    assert body["pages"][0]["method"] == "text"
    assert "LG-2026-0912" in body["text"]
    assert "53,080.11" in body["text"]
    assert body["engine"].startswith("pdfplumber+tesseract-")
    assert isinstance(body["pages"][0]["tables"], list)
    assert body["seconds"] >= 0


def test_wrong_file_type_is_415(build_client, tmp_path):
    client = build_client()
    fake = tmp_path / "notes.pdf"  # the extension lies; content decides
    fake.write_text("this is not a pdf\n" * 20)
    r = post_file(client, fake)
    assert r.status_code == 415
    assert "Allowed: PDF" in r.json()["detail"]
    assert post_file(client, fake, route="/v1/jobs", kind="extract").status_code == 415


def test_too_many_pages_is_413(build_client, tmp_path):
    client = build_client(extract_max_pages=40)
    pdf = make_text_pdf(tmp_path / "long.pdf", ["A page with enough words on it to count as a text layer page"], pages=41)
    r = post_file(client, pdf)
    assert r.status_code == 413
    assert "41 pages" in r.json()["detail"]


def test_missing_key_is_401(build_client, tmp_path):
    client = build_client()
    pdf = make_text_pdf(tmp_path / "x.pdf", ["hello"])
    with open(pdf, "rb") as f:
        assert client.post("/v1/extract", files={"file": ("x.pdf", f)}).status_code == 401


def test_exactly_one_source(build_client):
    client = build_client()
    r = client.post("/v1/extract", headers=AUTH, data={})
    assert r.status_code == 400
    assert "file (file) or file_url" in r.json()["detail"]


def test_sniff_kind_by_content(tmp_path):
    (tmp_path / "a").write_bytes(b"%PDF-1.4 ...")
    (tmp_path / "b").write_bytes(b"\x89PNG\r\n\x1a\n....")
    (tmp_path / "c").write_bytes(b"\xff\xd8\xff\xe0....")
    (tmp_path / "d").write_bytes(b"\x00\x00\x00\x18ftypheic....")
    (tmp_path / "e").write_bytes(b"II*\x00....")
    (tmp_path / "f").write_bytes(b"hello")
    assert extract_mod.sniff_kind(str(tmp_path / "a")) == "pdf"
    assert extract_mod.sniff_kind(str(tmp_path / "b")) == "image"
    assert extract_mod.sniff_kind(str(tmp_path / "c")) == "image"
    assert extract_mod.sniff_kind(str(tmp_path / "d")) == "heic"
    assert extract_mod.sniff_kind(str(tmp_path / "e")) == "image"
    assert extract_mod.sniff_kind(str(tmp_path / "f")) is None


def test_extract_job_runs_through_the_queue(build_client, tmp_path):
    client = build_client()
    pdf = make_text_pdf(tmp_path / "job.pdf", ["MyHomePros invoice 4471 for September 2026", "Leads delivered to Fort Myers: 90", "Total Due $1,234.56"], pages=2)
    r = post_file(client, pdf, route="/v1/jobs", kind="extract")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["chunks_total"] == 1
    done = wait_for_job(client, body["job_id"])
    assert done["status"] == "done", done
    assert done["result"]["page_count"] == 2
    assert done["result"]["ocr_pages"] == 0
    assert "1,234.56" in done["result"]["text"]
    assert done["result"]["engine"].startswith("pdfplumber+tesseract-")
    assert done["chunks"] == [{"index": 0, "status": "done"}]


def test_unknown_kind_is_400(build_client, tmp_path):
    client = build_client()
    pdf = make_text_pdf(tmp_path / "k.pdf", ["x"])
    assert post_file(client, pdf, route="/v1/jobs", kind="summarize").status_code == 400


@needs_tesseract
def test_scanned_pdf_is_ocrd_and_the_total_is_readable(build_client, tmp_path):
    client = build_client()
    scanned = make_text_image(tmp_path / "scan.pdf", ["INVOICE 4471", "TOTAL DUE 1234.56"], fmt="PDF")
    r = post_file(client, scanned)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"][0]["method"] == "ocr"
    assert body["ocr_pages"] == 1
    assert "1234.56" in body["text"].replace(",", "")


@needs_tesseract
def test_png_photo_of_an_invoice_works(build_client, tmp_path):
    client = build_client()
    png = make_text_image(tmp_path / "photo.png", ["AMOUNT DUE 987.65"])
    r = post_file(client, png)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page_count"] == 1 and body["ocr_pages"] == 1
    assert "987.65" in body["text"].replace(",", "")


@pytest.mark.skipif(HAS_TESSERACT, reason="only meaningful without tesseract")
def test_without_tesseract_a_scanned_page_is_503_not_a_crash(build_client, tmp_path):
    client = build_client()
    scanned = make_text_image(tmp_path / "scan.pdf", ["TOTAL 1"], fmt="PDF")
    r = post_file(client, scanned)
    assert r.status_code == 503
    assert "OCR is not available" in r.json()["detail"]
