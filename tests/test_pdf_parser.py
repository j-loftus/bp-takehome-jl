"""Tests for src/pipeline/pdf_parser.py"""

import csv
from pathlib import Path

import fitz

from src.pipeline.pdf_parser import (
    extract_page_images,
    parse_directory,
    parse_pdf,
    write_parse_results,
)


def _make_pdf(path: Path, text: str = "Hello world contract text") -> Path:
    """Create a minimal single-page PDF with extractable text."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def _make_multipage_pdf(path: Path, n_pages: int, text: str = "Page content") -> Path:
    """Create a multi-page PDF with extractable text."""
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text} {i + 1}")
    doc.save(str(path))
    doc.close()
    return path


# ---------------------------------------------------------------------------
# parse_pdf
# ---------------------------------------------------------------------------

def test_normal_pdf(tmp_path):
    pdf = _make_pdf(tmp_path / "sample.pdf")
    result = parse_pdf(str(pdf))

    assert result["filename"] == "sample.pdf"
    assert result["filepath"] == str(pdf.resolve())
    assert "Hello world" in result["text"]
    assert result["page_count"] == 1
    assert result["is_scanned"] is False
    assert result["parse_error"] is None


def test_multipage_pdf(tmp_path):
    pdf_path = tmp_path / "multi.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1} content")
    doc.save(str(pdf_path))
    doc.close()

    result = parse_pdf(str(pdf_path))

    assert result["page_count"] == 3
    assert result["text"].count("--- PAGE BREAK ---") == 2
    assert result["is_scanned"] is False
    assert result["parse_error"] is None


def test_missing_file():
    result = parse_pdf("/tmp/does_not_exist_xyz.pdf")

    assert result["text"] == ""
    assert result["is_scanned"] is False
    assert result["parse_error"] is not None
    assert result["page_count"] == 0


def test_invalid_pdf(tmp_path):
    bad = tmp_path / "garbage.pdf"
    bad.write_bytes(b"this is not a pdf at all!!")

    result = parse_pdf(str(bad))

    assert result["text"] == ""
    assert result["parse_error"] is not None
    assert result["is_scanned"] is False


# ---------------------------------------------------------------------------
# parse_directory
# ---------------------------------------------------------------------------

def test_parse_directory(tmp_path, monkeypatch):
    # Write outputs relative to cwd; redirect cwd to tmp_path
    monkeypatch.chdir(tmp_path)

    _make_pdf(tmp_path / "a.pdf", "Contract A content")
    _make_pdf(tmp_path / "b.pdf", "Contract B content")
    (tmp_path / "readme.txt").write_text("not a pdf")

    results = parse_directory(str(tmp_path))

    assert len(results) == 2
    filenames = {r["filename"] for r in results}
    assert filenames == {"a.pdf", "b.pdf"}

    csv_path = tmp_path / "outputs" / "parse_results.csv"
    summary_path = tmp_path / "outputs" / "parse_summary.txt"
    assert csv_path.exists()
    assert summary_path.exists()


# ---------------------------------------------------------------------------
# write_parse_results — parse_status derivation
# ---------------------------------------------------------------------------

def test_parse_status_success(tmp_path):
    results = [
        {"filename": "ok.pdf", "filepath": "/x/ok.pdf", "text": "some text",
         "page_count": 1, "is_scanned": False, "parse_error": None},
    ]
    out = tmp_path / "results.csv"
    write_parse_results(results, str(out))

    rows = list(csv.DictReader(out.open()))
    assert rows[0]["parse_status"] == "success"
    assert rows[0]["char_count"] == str(len("some text"))


def test_parse_status_scanned(tmp_path):
    results = [
        {"filename": "scan.pdf", "filepath": "/x/scan.pdf", "text": "",
         "page_count": 2, "is_scanned": True, "parse_error": None},
    ]
    out = tmp_path / "results.csv"
    write_parse_results(results, str(out))

    rows = list(csv.DictReader(out.open()))
    assert rows[0]["parse_status"] == "scanned"


def test_parse_status_error(tmp_path):
    results = [
        {"filename": "bad.pdf", "filepath": "/x/bad.pdf", "text": "",
         "page_count": 0, "is_scanned": False, "parse_error": "File not found"},
    ]
    out = tmp_path / "results.csv"
    write_parse_results(results, str(out))

    rows = list(csv.DictReader(out.open()))
    assert rows[0]["parse_status"] == "error"


def test_parse_status_overwrites_existing(tmp_path):
    out = tmp_path / "results.csv"
    out.write_text("old content")

    results = [
        {"filename": "new.pdf", "filepath": "/x/new.pdf", "text": "hi",
         "page_count": 1, "is_scanned": False, "parse_error": None},
    ]
    write_parse_results(results, str(out))

    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    assert rows[0]["filename"] == "new.pdf"


# ---------------------------------------------------------------------------
# extract_page_images
# ---------------------------------------------------------------------------

def test_extract_page_images_returns_png_bytes(tmp_path):
    pdf = _make_pdf(tmp_path / "sample.pdf")
    images = extract_page_images(str(pdf))
    assert len(images) == 1
    assert isinstance(images[0], bytes)
    # PNG magic bytes: \x89PNG
    assert images[0][:4] == b"\x89PNG"


def test_extract_page_images_respects_max_pages(tmp_path):
    pdf = _make_multipage_pdf(tmp_path / "multi.pdf", n_pages=5)
    images = extract_page_images(str(pdf), max_pages=2)
    assert len(images) == 2


def test_extract_page_images_missing_file_returns_empty():
    images = extract_page_images("/nonexistent/path/missing.pdf")
    assert images == []
