"""Tests for src/pipeline/classifier.py"""

import csv
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.pipeline.classifier import (
    DocType,
    _classify_by_llm,
    _classify_by_rules,
    _classify_by_vision,
    _get_classification_window,
    _is_award_letter,
    _is_fully_executed,
    _is_modification,
    _is_renewal_letter,
    _is_vds,
    classify_document,
    classify_directory,
    print_classification_summary,
    write_classification_results,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _parse_result(
    text: str = "",
    filename: str = "test.pdf",
    is_scanned: bool = False,
    filepath: str = "",
) -> dict:
    return {
        "filename": filename,
        "text": text,
        "is_scanned": is_scanned,
        "page_count": 1,
        "filepath": filepath,
    }


# ---------------------------------------------------------------------------
# Text window
# ---------------------------------------------------------------------------

def test_classification_window_truncates():
    text = "A" * 3000
    assert len(_get_classification_window(text)) == 2000


def test_classification_window_strips():
    assert _get_classification_window("  hello  ") == "hello"


def test_classification_window_shorter_than_limit():
    assert _get_classification_window("short text") == "short text"


# ---------------------------------------------------------------------------
# Rule: vendor_disclosure_statement
# ---------------------------------------------------------------------------

def test_vds_matches_both_signals():
    text = "Vendor Disclosure Statement\nPlease disclose familial relationships below."
    assert _is_vds(text) is True


def test_vds_matches_campaign_contributions():
    text = "vendor disclosure statement\ncampaign contributions must be listed here."
    assert _is_vds(text) is True


def test_vds_requires_header():
    text = "Please disclose familial relationships below."
    assert _is_vds(text) is False


def test_vds_requires_section():
    text = "Vendor Disclosure Statement"
    assert _is_vds(text) is False


# ---------------------------------------------------------------------------
# Rule: renewal_letter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "is hereby renewed",
    "is being renewed",
    "extended for one additional year",
    "renewal of contract",
    "contract renewal",
])
def test_renewal_letter_matches_each_phrase(phrase):
    assert _is_renewal_letter(f"This contract {phrase} effective immediately.") is True


def test_renewal_letter_no_match():
    assert _is_renewal_letter("This is a fully executed agreement.") is False


def test_renewal_letter_case_insensitive():
    assert _is_renewal_letter("IS HEREBY RENEWED") is True


# ---------------------------------------------------------------------------
# Rule: award_letter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "intent to award",
    "intent-to-award",
    "this is not an order",
    "award of contract",
])
def test_award_letter_matches_each_phrase(phrase):
    assert _is_award_letter(f"Notice: {phrase} to Acme Corp.") is True


def test_award_letter_no_match():
    assert _is_award_letter("Please see attached scope of work.") is False


# ---------------------------------------------------------------------------
# Rule: modification_amendment
# ---------------------------------------------------------------------------

def test_modification_amendment_number():
    assert _is_modification("Amendment Number 3 to Agreement 18018") is True


def test_modification_modification_number():
    assert _is_modification("Modification Number 1 effective January 1.") is True


def test_modification_amendment_to_agreement():
    assert _is_modification("This is an amendment to the agreement dated 2020.") is True


def test_modification_to_agreement():
    assert _is_modification("This modification to the agreement changes scope.") is True


def test_modification_whereas_parties():
    assert _is_modification("WHEREAS, the parties entered into that certain agreement...") is True


def test_modification_whereas_county():
    assert _is_modification("WHEREAS, the County and Vendor entered into an agreement...") is True


def test_modification_no_match():
    assert _is_modification("This is a fully executed agreement for janitorial services.") is False


# ---------------------------------------------------------------------------
# Rule: fully_executed_agreement
# ---------------------------------------------------------------------------

def test_fully_executed_all_signals():
    text = "This Agreement covers the scope of work for janitorial services. The initial term is one year."
    assert _is_fully_executed(text) is True


def test_fully_executed_missing_scope():
    text = "This Agreement sets the effective date for the initial term."
    assert _is_fully_executed(text) is False


def test_fully_executed_missing_term():
    text = "This Agreement covers the scope of work for janitorial services."
    assert _is_fully_executed(text) is False


def test_fully_executed_missing_identity():
    text = "The scope of work is defined below. The initial term begins January 1."
    assert _is_fully_executed(text) is False


# ---------------------------------------------------------------------------
# Rule priority ordering
# ---------------------------------------------------------------------------

def test_rules_renewal_beats_fully_executed():
    # Contains both renewal phrase AND agreement identity/scope/term signals
    text = (
        "This contract is hereby renewed. "
        "It covers the scope of work for services. "
        "The initial term begins January 1. "
        "This Agreement is between the County and Vendor."
    )
    doc_type, confidence = _classify_by_rules(text)
    assert doc_type == "renewal_letter"
    assert confidence == "high"


def test_rules_modification_beats_fully_executed():
    text = (
        "Amendment Number 2 to the Agreement. "
        "Scope of work is updated per Exhibit A. "
        "Effective date is January 1."
    )
    doc_type, confidence = _classify_by_rules(text)
    assert doc_type == "modification_amendment"


def test_rules_no_match_returns_none():
    doc_type, confidence = _classify_by_rules("Some random text with no signals.")
    assert doc_type is None
    assert confidence is None


def test_rules_vds_confidence_high():
    text = "Vendor Disclosure Statement\nfamilial relationships disclosure"
    doc_type, confidence = _classify_by_rules(text)
    assert doc_type == "vendor_disclosure_statement"
    assert confidence == "high"


def test_rules_modification_confidence_medium():
    text = "Amendment Number 1 to Agreement 18018"
    doc_type, confidence = _classify_by_rules(text)
    assert doc_type == "modification_amendment"
    assert confidence == "medium"


def test_rules_fully_executed_confidence_medium():
    text = "This Agreement covers the scope of work. The effective date is January 1."
    doc_type, confidence = _classify_by_rules(text)
    assert doc_type == "fully_executed_agreement"
    assert confidence == "medium"


# ---------------------------------------------------------------------------
# classify_document — structure and bypass paths
# ---------------------------------------------------------------------------

def test_classify_document_returns_all_keys():
    text = "This contract is hereby renewed."
    result = classify_document(_parse_result(text=text))
    for key in ("filename", "doc_type", "confidence", "classification_method", "reasoning", "classification_error"):
        assert key in result


def test_classify_document_scanned_no_filepath_falls_back():
    # No filepath → vision classification cannot run → falls back to other/low
    result = classify_document(_parse_result(is_scanned=True))
    assert result["doc_type"] == "other"
    assert result["confidence"] == "low"
    assert result["classification_method"] == "vision"
    assert result["classification_error"] is not None


def test_classify_document_scanned_vision_success():
    vision_response = json.dumps({
        "doc_type": "modification_amendment",
        "confidence": "high",
        "reasoning": "WHEREAS recitals and amendment number visible on page 1.",
    })
    with patch("src.pipeline.classifier.call_llm_with_images", return_value=vision_response), \
         patch("src.pipeline.classifier.extract_page_images", return_value=[b"\x89PNG\r\n" + b"\x00" * 50]):
        result = classify_document(_parse_result(is_scanned=True, filepath="/docs/mod.pdf"))
    assert result["doc_type"] == "modification_amendment"
    assert result["confidence"] == "high"
    assert result["classification_method"] == "vision"
    assert result["classification_error"] is None


def test_classify_document_scanned_vision_passes_filepath():
    with patch("src.pipeline.classifier._classify_by_vision") as mock_vision:
        mock_vision.return_value = ("fully_executed_agreement", "high", "Signed agreement.", None)
        classify_document(_parse_result(is_scanned=True, filepath="/docs/agreement.pdf"))
    mock_vision.assert_called_once_with("/docs/agreement.pdf")


def test_classify_document_empty_text_bypass():
    result = classify_document(_parse_result(text="   "))
    assert result["doc_type"] == "other"
    assert result["confidence"] == "low"
    assert "No extractable text" in result["classification_error"]


def test_classify_document_rule_match_no_llm():
    text = "This contract is hereby renewed."
    with patch("src.pipeline.classifier.call_llm") as mock_llm:
        result = classify_document(_parse_result(text=text))
    mock_llm.assert_not_called()
    assert result["doc_type"] == "renewal_letter"
    assert result["classification_method"] == "rule_based"
    assert result["reasoning"] is None
    assert result["classification_error"] is None


def test_classify_document_no_rule_invokes_llm():
    text = "Some unusual document with no matching signals whatsoever."
    llm_response = json.dumps({"doc_type": "other", "confidence": "medium", "reasoning": "No clear signals."})
    with patch("src.pipeline.classifier.call_llm", return_value=llm_response):
        result = classify_document(_parse_result(text=text))
    assert result["classification_method"] == "llm"


# ---------------------------------------------------------------------------
# _classify_by_vision — response handling
# ---------------------------------------------------------------------------

_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


def test_classify_by_vision_no_filepath():
    doc_type, confidence, _, error = _classify_by_vision("")
    assert doc_type == "other"
    assert confidence == "low"
    assert error is not None


def test_classify_by_vision_no_images():
    with patch("src.pipeline.classifier.extract_page_images", return_value=[]):
        doc_type, confidence, _, error = _classify_by_vision("/some/file.pdf")
    assert doc_type == "other"
    assert error is not None


def test_classify_by_vision_success():
    response = json.dumps({
        "doc_type": "fully_executed_agreement",
        "confidence": "high",
        "reasoning": "Signed contract with scope of work visible.",
    })
    with patch("src.pipeline.classifier.extract_page_images", return_value=[_FAKE_PNG]), \
         patch("src.pipeline.classifier.call_llm_with_images", return_value=response):
        doc_type, confidence, reasoning, error = _classify_by_vision("/docs/agreement.pdf")
    assert doc_type == "fully_executed_agreement"
    assert confidence == "high"
    assert reasoning is not None
    assert error is None


def test_classify_by_vision_invalid_doc_type():
    response = json.dumps({"doc_type": "purchase_order", "confidence": "high", "reasoning": "Looks like a PO."})
    with patch("src.pipeline.classifier.extract_page_images", return_value=[_FAKE_PNG]), \
         patch("src.pipeline.classifier.call_llm_with_images", return_value=response):
        doc_type, confidence, _, error = _classify_by_vision("/docs/doc.pdf")
    assert doc_type == "other"
    assert "invalid doc_type" in error


def test_classify_by_vision_low_confidence_overrides_to_other():
    response = json.dumps({"doc_type": "renewal_letter", "confidence": "low", "reasoning": "Uncertain."})
    with patch("src.pipeline.classifier.extract_page_images", return_value=[_FAKE_PNG]), \
         patch("src.pipeline.classifier.call_llm_with_images", return_value=response):
        doc_type, confidence, _, error = _classify_by_vision("/docs/doc.pdf")
    assert doc_type == "other"
    assert confidence == "low"
    assert error is None


def test_classify_by_vision_uses_first_two_pages_only():
    response = json.dumps({"doc_type": "award_letter", "confidence": "high", "reasoning": "Award notice visible."})
    with patch("src.pipeline.classifier.extract_page_images", return_value=[_FAKE_PNG]) as mock_images, \
         patch("src.pipeline.classifier.call_llm_with_images", return_value=response):
        _classify_by_vision("/docs/doc.pdf")
    mock_images.assert_called_once_with("/docs/doc.pdf", dpi=150, max_pages=2)


# ---------------------------------------------------------------------------
# _classify_by_llm — response handling
# ---------------------------------------------------------------------------

def test_llm_valid_high_confidence():
    response = json.dumps({"doc_type": "award_letter", "confidence": "high", "reasoning": "Contains intent to award."})
    with patch("src.pipeline.classifier.call_llm", return_value=response):
        doc_type, confidence, reasoning, error = _classify_by_llm("some text")
    assert doc_type == "award_letter"
    assert confidence == "high"
    assert reasoning == "Contains intent to award."
    assert error is None


def test_llm_low_confidence_overrides_to_other():
    response = json.dumps({"doc_type": "renewal_letter", "confidence": "low", "reasoning": "Uncertain."})
    with patch("src.pipeline.classifier.call_llm", return_value=response):
        doc_type, confidence, reasoning, error = _classify_by_llm("some text")
    assert doc_type == "other"
    assert confidence == "low"
    assert error is None


def test_llm_invalid_enum_value():
    response = json.dumps({"doc_type": "purchase_order", "confidence": "high", "reasoning": "Looks like a PO."})
    with patch("src.pipeline.classifier.call_llm", return_value=response):
        doc_type, confidence, reasoning, error = _classify_by_llm("some text")
    assert doc_type == "other"
    assert "invalid doc_type" in error


def test_llm_malformed_json():
    with patch("src.pipeline.classifier.call_llm", return_value="not json at all"):
        doc_type, confidence, reasoning, error = _classify_by_llm("some text")
    assert doc_type == "other"
    assert "not valid JSON" in error


def test_llm_api_exception():
    with patch("src.pipeline.classifier.call_llm", side_effect=Exception("connection timeout")):
        doc_type, confidence, reasoning, error = _classify_by_llm("some text")
    assert doc_type == "other"
    assert "LLM classification call failed" in error
    assert "connection timeout" in error


# ---------------------------------------------------------------------------
# write_classification_results
# ---------------------------------------------------------------------------

def test_write_classification_results_creates_csv(tmp_path):
    results = [
        {
            "filename": "doc1.pdf", "doc_type": "renewal_letter", "confidence": "high",
            "classification_method": "rule_based", "reasoning": None, "classification_error": None,
        },
        {
            "filename": "doc2.pdf", "doc_type": "other", "confidence": "low",
            "classification_method": "llm", "reasoning": "No signals.", "classification_error": None,
        },
    ]
    out = tmp_path / "results.csv"
    write_classification_results(results, str(out))

    assert out.exists()
    with open(out, newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    assert rows[0]["filename"] == "doc1.pdf"
    assert rows[0]["doc_type"] == "renewal_letter"
    assert rows[1]["reasoning"] == "No signals."


def test_write_classification_results_creates_parent_dir(tmp_path):
    results = [{"filename": "a.pdf", "doc_type": "other", "confidence": "low",
                "classification_method": "rule_based", "reasoning": None, "classification_error": None}]
    out = tmp_path / "nested" / "dir" / "results.csv"
    write_classification_results(results, str(out))
    assert out.exists()


# ---------------------------------------------------------------------------
# print_classification_summary
# ---------------------------------------------------------------------------

def test_print_classification_summary_creates_file(tmp_path):
    results = [
        {"filename": "a.pdf", "doc_type": "renewal_letter", "confidence": "high",
         "classification_method": "rule_based", "reasoning": None, "classification_error": None},
        {"filename": "b.pdf", "doc_type": "other", "confidence": "low",
         "classification_method": "llm", "reasoning": "Unclear.", "classification_error": None},
    ]
    out = tmp_path / "summary.txt"
    print_classification_summary(results, str(out))

    assert out.exists()
    content = out.read_text()
    assert "Document Classification Run Summary" in content
    assert "Doc Type Distribution" in content
    assert "Classification Method" in content
    assert "Confidence Distribution" in content
    assert "Low-Confidence Documents" in content
    assert "Classification Errors" in content


def test_print_classification_summary_scanned_label(tmp_path):
    results = [
        {"filename": "scanned.pdf", "doc_type": "other", "confidence": "low",
         "classification_method": "rule_based", "reasoning": None,
         "classification_error": "Document is scanned or image-only; classification skipped"},
    ]
    out = tmp_path / "summary.txt"
    print_classification_summary(results, str(out))
    content = out.read_text()
    assert "[scanned]" in content


# ---------------------------------------------------------------------------
# classify_directory — integration (no live LLM)
# ---------------------------------------------------------------------------

def test_classify_directory_returns_one_result_per_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parse_results = [
        _parse_result(text="This contract is hereby renewed.", filename="a.pdf"),
        _parse_result(is_scanned=True, filename="b.pdf"),  # no filepath → vision fallback → other
    ]
    results = classify_directory(parse_results)
    assert len(results) == 2
    assert results[0]["doc_type"] == "renewal_letter"
    assert results[1]["doc_type"] == "other"
    assert results[1]["classification_method"] == "vision"


def test_classify_directory_writes_csv_and_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parse_results = [_parse_result(text="Award of contract to Acme Corp.", filename="x.pdf")]
    classify_directory(parse_results)
    assert (tmp_path / "outputs" / "classification_results.csv").exists()
    assert (tmp_path / "outputs" / "classification_summary.txt").exists()
