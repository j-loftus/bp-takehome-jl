"""Tests for src/pipeline/extractor.py"""

import csv
import json
from unittest.mock import patch

from src.pipeline.extractor import extract_batch, extract_document
from src.llm_client import LLMCallError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _parse_result(
    filename: str = "test.pdf",
    text: str = "contract text",
    is_scanned: bool = False,
    page_count: int = 3,
) -> dict:
    return {
        "filename": filename,
        "filepath": f"/contracts/{filename}",
        "text": text,
        "page_count": page_count,
        "is_scanned": is_scanned,
        "parse_error": None,
    }


def _classification_result(
    filename: str = "test.pdf",
    doc_type: str = "fully_executed_agreement",
    confidence: str = "high",
) -> dict:
    return {
        "filename": filename,
        "doc_type": doc_type,
        "confidence": confidence,
        "classification_method": "rule_based",
        "reasoning": None,
        "classification_error": None,
    }


# Minimal valid LLM JSON response for a fully_executed_agreement
_VALID_LLM_JSON = json.dumps({
    "contract_number": "23159",
    "vendor_name": "Johnson Controls Inc.",
    "doc_date": "2023-04-15",
    "doc_type": "fully_executed_agreement",
    "county_department": "Facilities and Construction Services",
    "total_contract_value": 248500.00,
    "price_escalator_terms": "cpi_capped",
    "modification_financial_delta": None,
    "contract_start_date": "2023-05-01",
    "contract_end_date": "2024-04-30",
    "renewal_options": "3 × 1-year options",
    "auto_renewal_flag": False,
    "termination_notice_days": 30,
    "service_category": "facilities_maintenance",
    "procurement_vehicle": "direct_rfp",
    "insurance_requirements_flag": True,
    "parent_contract_number": None,
    "extraction_confidence": "high",
    "extraction_notes": None,
})


# ---------------------------------------------------------------------------
# Vision path — scanned documents
# ---------------------------------------------------------------------------

_FAKE_IMAGES = [b"\x89PNG\r\n\x1a\n" + b"\x00" * 100]  # minimal PNG-like bytes


def test_scanned_routes_to_vision_not_skip():
    parse = _parse_result(is_scanned=True)
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm_with_images", return_value=_VALID_LLM_JSON), \
         patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "success"


def test_scanned_calls_vision_not_text():
    parse = _parse_result(is_scanned=True)
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm_with_images", return_value=_VALID_LLM_JSON) as mock_vision, \
         patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES), \
         patch("src.pipeline.extractor.call_llm") as mock_text:
        extract_document(parse, classification)
    mock_vision.assert_called_once()
    mock_text.assert_not_called()


def test_scanned_no_images_returns_failed():
    parse = _parse_result(is_scanned=True)
    classification = _classification_result()
    with patch("src.pipeline.extractor.extract_page_images", return_value=[]):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "no images" in result["failure_reason"].lower()


def test_scanned_missing_filepath_returns_failed():
    parse = _parse_result(is_scanned=True)
    parse.pop("filepath")
    classification = _classification_result()
    result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "filepath" in result["failure_reason"].lower()


def test_scanned_vision_llm_failure_returns_failed():
    parse = _parse_result(is_scanned=True)
    classification = _classification_result()
    with patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES), \
         patch("src.pipeline.extractor.call_llm_with_images", side_effect=LLMCallError("timeout")):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "Vision LLM call failed" in result["failure_reason"]


def test_extraction_method_text_path():
    parse = _parse_result(is_scanned=False)
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON):
        result = extract_document(parse, classification)
    assert result["extraction_method"] == "text"


def test_extraction_method_vision_path():
    parse = _parse_result(is_scanned=True)
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm_with_images", return_value=_VALID_LLM_JSON), \
         patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES):
        result = extract_document(parse, classification)
    assert result["extraction_method"] == "vision"


# ---------------------------------------------------------------------------
# Vision classification now lives in the classifier; extractor uses doc_type as-is
# ---------------------------------------------------------------------------

def test_scanned_uses_doc_type_from_classifier():
    """Extractor builds extraction prompt from the classifier's doc_type, not its own logic."""
    parse = _parse_result(is_scanned=True)
    classification = _classification_result(doc_type="modification_amendment")
    with patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES), \
         patch("src.pipeline.extractor.build_extraction_prompt") as mock_build, \
         patch("src.pipeline.extractor.call_llm_with_images", return_value=_VALID_LLM_JSON):
        extract_document(parse, classification)
    mock_build.assert_called_once_with("modification_amendment", "")


def test_scanned_other_doc_type_still_succeeds():
    """Classifier returning 'other' for scanned (e.g. vision failed) still allows extraction."""
    parse = _parse_result(is_scanned=True)
    classification = _classification_result(doc_type="other")
    with patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES), \
         patch("src.pipeline.extractor.call_llm_with_images", return_value=_VALID_LLM_JSON):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "success"


# ---------------------------------------------------------------------------
# Skip gate — low-confidence classification
# ---------------------------------------------------------------------------

def test_skip_low_confidence():
    parse = _parse_result()
    classification = _classification_result(confidence="low")
    result = extract_document(parse, classification)
    assert result["extraction_status"] == "skipped"
    assert "confidence" in result["failure_reason"].lower()


def test_skip_low_confidence_does_not_call_llm():
    parse = _parse_result()
    classification = _classification_result(confidence="low")
    with patch("src.pipeline.extractor.call_llm") as mock_llm:
        extract_document(parse, classification)
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# LLM call failure
# ---------------------------------------------------------------------------

def test_llm_call_failure_returns_failed_record():
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", side_effect=LLMCallError("timeout")):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "LLM call failed" in result["failure_reason"]
    assert "timeout" in result["failure_reason"]


# ---------------------------------------------------------------------------
# JSON parse failure
# ---------------------------------------------------------------------------

def test_invalid_json_returns_failed_record():
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value="not json at all"):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "not valid JSON" in result["failure_reason"]


def test_json_with_markdown_fence_is_recovered():
    # extract_json should strip fences and parse successfully
    fenced_response = "```json\n" + _VALID_LLM_JSON + "\n```"
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=fenced_response):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "success"


def test_truly_unparseable_response_returns_failed_record():
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value="Sorry, I cannot help with that."):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "not valid JSON" in result["failure_reason"]


# ---------------------------------------------------------------------------
# Schema validation failures
# ---------------------------------------------------------------------------

def test_missing_contract_number_returns_failed_record():
    payload = json.loads(_VALID_LLM_JSON)
    payload["contract_number"] = None
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=json.dumps(payload)):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"
    assert "validation" in result["failure_reason"].lower()


def test_missing_vendor_name_returns_failed_record():
    payload = json.loads(_VALID_LLM_JSON)
    payload["vendor_name"] = None
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=json.dumps(payload)):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"


def test_invalid_price_escalator_enum_returns_failed_record():
    payload = json.loads(_VALID_LLM_JSON)
    payload["price_escalator_terms"] = "free_text_value"
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=json.dumps(payload)):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"


def test_invalid_date_format_returns_failed_record():
    payload = json.loads(_VALID_LLM_JSON)
    payload["doc_date"] = "April 15, 2023"  # not YYYY-MM-DD
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=json.dumps(payload)):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "failed"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_success_returns_correct_status():
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "success"


def test_success_contains_all_schema_keys():
    expected_keys = {
        "source_filename", "contract_number", "doc_type", "vendor_name",
        "doc_date", "county_department", "total_contract_value",
        "price_escalator_terms", "modification_financial_delta",
        "contract_start_date", "contract_end_date", "renewal_options",
        "auto_renewal_flag", "termination_notice_days", "service_category",
        "procurement_vehicle", "insurance_requirements_flag",
        "parent_contract_number", "extraction_confidence", "extraction_notes",
        "pipeline_run_timestamp", "extraction_status",
    }
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON):
        result = extract_document(parse, classification)
    assert expected_keys.issubset(result.keys())


def test_success_booleans_coerced_to_int():
    parse = _parse_result()
    classification = _classification_result()
    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON):
        result = extract_document(parse, classification)
    # auto_renewal_flag=False → 0, insurance_requirements_flag=True → 1
    assert result["auto_renewal_flag"] == 0
    assert result["insurance_requirements_flag"] == 1


def test_success_source_filename_matches_parse_result():
    parse = _parse_result(filename="agreement_22847.pdf")
    classification = _classification_result(filename="agreement_22847.pdf")
    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON):
        result = extract_document(parse, classification)
    assert result["source_filename"] == "agreement_22847.pdf"


# ---------------------------------------------------------------------------
# VDS — nullable type-specific fields are valid (not a failure)
# ---------------------------------------------------------------------------

def test_vds_null_type_specific_fields_are_valid():
    vds_json = json.dumps({
        "contract_number": "23159",
        "vendor_name": "Acme Corp",
        "doc_date": "2023-04-15",
        "doc_type": "vendor_disclosure_statement",
        "county_department": None,
        "total_contract_value": None,
        "price_escalator_terms": None,
        "modification_financial_delta": None,
        "contract_start_date": None,
        "contract_end_date": None,
        "renewal_options": None,
        "auto_renewal_flag": None,
        "termination_notice_days": None,
        "service_category": None,
        "procurement_vehicle": None,
        "insurance_requirements_flag": None,
        "parent_contract_number": "23159",
        "extraction_confidence": "high",
        "extraction_notes": None,
    })
    parse = _parse_result()
    classification = _classification_result(doc_type="vendor_disclosure_statement")
    with patch("src.pipeline.extractor.call_llm", return_value=vds_json):
        result = extract_document(parse, classification)
    assert result["extraction_status"] == "success"
    assert result["total_contract_value"] is None
    assert result["service_category"] is None


# ---------------------------------------------------------------------------
# extract_batch — routing
# ---------------------------------------------------------------------------

def test_extract_batch_routes_success_and_failure():
    parse_results = [
        _parse_result(filename="good.pdf"),
        _parse_result(filename="low_conf.pdf"),
    ]
    classification_results = [
        _classification_result(filename="good.pdf"),
        _classification_result(filename="low_conf.pdf", confidence="low"),
    ]

    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON), \
         patch("src.pipeline.extractor._write_batch_outputs"):
        successes, failures = extract_batch(parse_results, classification_results)

    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0]["source_filename"] == "good.pdf"
    assert failures[0]["source_filename"] == "low_conf.pdf"
    assert failures[0]["extraction_status"] == "skipped"


def test_extract_batch_scanned_routes_to_vision():
    parse_results = [
        _parse_result(filename="text.pdf"),
        _parse_result(filename="scanned.pdf", is_scanned=True),
    ]
    classification_results = [
        _classification_result(filename="text.pdf"),
        _classification_result(filename="scanned.pdf"),
    ]

    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON) as mock_text, \
         patch("src.pipeline.extractor.call_llm_with_images", return_value=_VALID_LLM_JSON) as mock_vision, \
         patch("src.pipeline.extractor.extract_page_images", return_value=_FAKE_IMAGES), \
         patch("src.pipeline.extractor._write_batch_outputs"):
        successes, failures = extract_batch(parse_results, classification_results)

    assert len(successes) == 2
    assert len(failures) == 0
    mock_text.assert_called_once()
    mock_vision.assert_called_once()


def test_extract_batch_matches_by_filename_not_order():
    # Classification list is in reverse order relative to parse list
    parse_results = [
        _parse_result(filename="a.pdf"),
        _parse_result(filename="b.pdf"),
    ]
    classification_results = [
        _classification_result(filename="b.pdf", confidence="low"),  # reversed + low conf
        _classification_result(filename="a.pdf"),
    ]

    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON), \
         patch("src.pipeline.extractor._write_batch_outputs"):
        successes, failures = extract_batch(parse_results, classification_results)

    # a.pdf should succeed, b.pdf should be skipped (low confidence)
    assert any(r["source_filename"] == "a.pdf" for r in successes)
    assert any(r["source_filename"] == "b.pdf" for r in failures)


# ---------------------------------------------------------------------------
# extract_batch — CSV output
# ---------------------------------------------------------------------------

def test_extract_batch_writes_csv_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parse_results = [_parse_result(filename="doc.pdf")]
    classification_results = [_classification_result(filename="doc.pdf")]

    with patch("src.pipeline.extractor.call_llm", return_value=_VALID_LLM_JSON):
        extract_batch(parse_results, classification_results)

    results_csv = tmp_path / "outputs" / "extraction_results.csv"
    failures_csv = tmp_path / "outputs" / "extraction_failures.csv"
    summary_txt  = tmp_path / "outputs" / "extraction_token_summary.txt"

    assert results_csv.exists()
    assert failures_csv.exists()
    assert summary_txt.exists()

    with open(results_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["source_filename"] == "doc.pdf"
    assert rows[0]["contract_number"] == "23159"


def test_extract_batch_failure_csv_has_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parse_results = [_parse_result(filename="low_conf.pdf")]
    classification_results = [_classification_result(filename="low_conf.pdf", confidence="low")]

    extract_batch(parse_results, classification_results)

    failures_csv = tmp_path / "outputs" / "extraction_failures.csv"
    with open(failures_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["extraction_status"] == "skipped"
    assert rows[0]["failure_reason"] != ""
    assert rows[0]["failure_reason"] != ""
