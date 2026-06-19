"""
PDF → text extraction.

Extracts raw text from a PDF file using PyMuPDF. Detects scanned/image-only
documents and flags them rather than attempting OCR. Outputs a CSV tracking
file and a summary report after batch runs.
"""

import csv
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

logger = logging.getLogger("pdf_parser")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [pdf_parser] [%(levelname)s] %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

_PAGE_SEP = "\n\n--- PAGE BREAK ---\n\n"
_SCANNED_CHAR_THRESHOLD = 100
_LOW_CONTENT_THRESHOLD = 500


def parse_pdf(filepath: str) -> dict:
    """
    Extract text and metadata from a single PDF file.

    Returns a dict with keys:
        filepath    (str)        — absolute path to source file
        filename    (str)        — basename of the file
        text        (str)        — full extracted text; empty string if scanned or failed
        page_count  (int)        — total pages; 0 if file failed to open
        is_scanned  (bool)       — True if document appears image-only
        parse_error (str | None) — error message on failure; None on success
    """
    abs_path = str(Path(filepath).resolve())
    filename = Path(filepath).name

    def _error_result(msg: str, page_count: int = 0, is_scanned: bool = False) -> dict:
        return {
            "filepath": abs_path,
            "filename": filename,
            "text": "",
            "page_count": page_count,
            "is_scanned": is_scanned,
            "parse_error": msg,
        }

    if not Path(abs_path).exists():
        msg = f"File not found: {abs_path}"
        logger.warning(msg)
        return _error_result(msg)

    try:
        doc = fitz.open(abs_path)
    except Exception as exc:
        msg = f"Failed to open PDF: {exc}"
        logger.warning("%s: %s", filename, msg)
        return _error_result(msg)

    page_count = len(doc)
    page_texts: list[str] = []
    has_images = False
    page_errors: list[str] = []

    for i, page in enumerate(doc):
        try:
            page_text = page.get_text()
            page_texts.append(page_text)
            if page.get_images():
                has_images = True
            logger.debug("%s — page %d: %d chars", filename, i + 1, len(page_text))
        except Exception as exc:
            page_errors.append(f"page {i + 1}: {exc}")
            page_texts.append("")

    doc.close()

    full_text = _PAGE_SEP.join(page_texts)
    # Collapse 3+ consecutive blank lines to 2
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)

    parse_error = None
    if page_errors:
        parse_error = "Extraction errors on " + ", ".join(page_errors)

    # Scanned detection: very little actual content AND at least one image page.
    # Check sum of raw page lengths rather than len(full_text) — the PAGE_SEP
    # string adds ~22 chars per page break and would push a 10-page scanned doc
    # above the 100-char threshold before actual content is considered.
    total_content_chars = sum(len(t) for t in page_texts)
    if total_content_chars < _SCANNED_CHAR_THRESHOLD and has_images:
        logger.info("Scanned document detected: %s", filename)
        return {
            "filepath": abs_path,
            "filename": filename,
            "text": "",
            "page_count": page_count,
            "is_scanned": True,
            "parse_error": None,
        }

    logger.info(
        "Parsed %s — %d pages, is_scanned=False%s",
        filename,
        page_count,
        f", warnings: {parse_error}" if parse_error else "",
    )

    return {
        "filepath": abs_path,
        "filename": filename,
        "text": full_text,
        "page_count": page_count,
        "is_scanned": False,
        "parse_error": parse_error,
    }


def extract_page_images(filepath: str, dpi: int = 150, max_pages: int = 20) -> list[bytes]:
    """
    Render each page of a PDF as a PNG image.
    Used for scanned documents where text extraction fails.

    Args:
        filepath:  Path to the PDF file.
        dpi:       Render resolution. 150 DPI is sufficient for text legibility
                   while keeping image token cost manageable.
        max_pages: Maximum pages to render. Pages beyond this limit are skipped.
                   Default 20 covers substantive content of all fully executed
                   agreements in this corpus.

    Returns:
        List of raw PNG bytes, one per page, in page order.
        Returns empty list if the file cannot be opened.
    """
    try:
        doc = fitz.open(str(Path(filepath).resolve()))
        images = []
        for page_num in range(min(len(doc), max_pages)):
            page = doc[page_num]
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            images.append(pix.tobytes("png"))
        doc.close()
        return images
    except Exception as e:
        logger.warning("extract_page_images failed for %s: %s", filepath, e)
        return []


def parse_directory(dirpath: str) -> list[dict]:
    """
    Parse all PDF files in a directory. Returns a list of parse result dicts
    (one per file) in the format returned by parse_pdf().
    Non-PDF files are ignored. Files that fail to open are logged and included
    with parse_error populated.
    """
    dir_path = Path(dirpath)
    pdf_files = sorted(
        p for p in dir_path.iterdir() if p.suffix.lower() == ".pdf"
    )

    logger.info("Found %d PDF files in %s", len(pdf_files), dirpath)

    results: list[dict] = []
    for pdf_file in pdf_files:
        result = parse_pdf(str(pdf_file))
        results.append(result)

    write_parse_results(results, "outputs/parse_results.csv")
    print_parse_summary(results, dirpath, "outputs/parse_summary.txt")

    return results


def write_parse_results(results: list[dict], output_path: str) -> None:
    """
    Write parse results to a CSV tracking file. One row per parsed file.
    Overwrites any existing file at output_path.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "filename",
                "filepath",
                "page_count",
                "char_count",
                "is_scanned",
                "parse_error",
                "parse_status",
            ],
        )
        writer.writeheader()
        for r in results:
            if r["is_scanned"]:
                status = "scanned"
            elif r["parse_error"] and not r["is_scanned"]:
                status = "error"
            else:
                status = "success"

            writer.writerow(
                {
                    "filename": r["filename"],
                    "filepath": r["filepath"],
                    "page_count": r["page_count"],
                    "char_count": len(r["text"]),
                    "is_scanned": r["is_scanned"],
                    "parse_error": r["parse_error"] or "",
                    "parse_status": status,
                }
            )


def print_parse_summary(
    results: list[dict], source_dir: str, output_path: str
) -> None:
    """
    Print and write a summary report of a completed parse run.
    results:    list of dicts from parse_directory()
    source_dir: the directory that was parsed (for display)
    output_path: path to write parse_summary.txt
    """
    total = len(results)
    scanned = [r for r in results if r["is_scanned"]]
    errors = [r for r in results if r["parse_error"] and not r["is_scanned"]]
    successful = [r for r in results if not r["is_scanned"] and not r["parse_error"]]

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "0.0%"

    success_chars = [len(r["text"]) for r in successful]
    total_chars = sum(len(r["text"]) for r in results)
    avg_chars = int(sum(success_chars) / len(success_chars)) if success_chars else 0
    min_chars = min(success_chars) if success_chars else 0
    max_chars = max(success_chars) if success_chars else 0

    low_content = [
        r for r in results
        if not r["is_scanned"] and 0 < len(r["text"]) < _LOW_CONTENT_THRESHOLD
    ]

    csv_path = os.path.abspath("outputs/parse_results.csv")

    lines = [
        "=== PDF Parse Run Summary ===",
        f"Run timestamp:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Source directory:   {source_dir}",
        f"Total files found:  {total}",
        "",
        "--- Status Breakdown ---",
        f"  Successful:       {len(successful):>4}  ({pct(len(successful))})",
        f"  Scanned (vision path): {len(scanned):>4}  ({pct(len(scanned))})",
        f"  Errors:           {len(errors):>4}  ({pct(len(errors))})",
        "",
        "--- Text Volume ---",
        f"  Total chars extracted:  {total_chars:,}",
        f"  Avg chars per doc:      {avg_chars:,}  (successful docs only)",
        f"  Min chars (success):    {min_chars:,}",
        f"  Max chars (success):    {max_chars:,}",
        "",
        f"--- Low-Content Documents (< {_LOW_CONTENT_THRESHOLD} chars, excluding scanned) ---",
    ]

    if low_content:
        for r in low_content:
            lines.append(f"  {r['filename']:<45} — {len(r['text']):>6} chars")
    else:
        lines.append("  (none)")

    lines += ["", "--- Scanned / Image-Only ---"]
    if scanned:
        for r in scanned:
            lines.append(f"  {r['filename']}")
    else:
        lines.append("  (none)")

    lines += ["", "--- Parse Errors ---"]
    if errors:
        for r in errors:
            lines.append(f"  {r['filename']:<45} — {r['parse_error']}")
    else:
        lines.append("  (none)")

    lines += ["", f"Tracking file written to: {csv_path}"]

    report = "\n".join(lines)

    logger.info("\n%s", report)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
