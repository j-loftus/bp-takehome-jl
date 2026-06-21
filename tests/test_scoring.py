"""Unit tests for eval/scoring.py — pure bucket classification and aggregation."""

from eval.scoring import (
    CORRECT,
    HALLUCINATED,
    MISSED,
    REVIEW,
    SKIPPED,
    TRUE_NEGATIVE,
    UNLABELED,
    WRONG_VALUE,
    aggregate_classification,
    aggregate_extraction,
    classify_cell,
    normalize_renewal_options,
    normalize_vendor_name,
    score_document_fields,
)


def test_classify_cell_correct_enum():
    assert classify_cell("service_category", "Facilities_Maintenance", "facilities_maintenance") == CORRECT


def test_classify_cell_wrong_value_enum():
    assert classify_cell("service_category", "staffing", "facilities_maintenance") == WRONG_VALUE


def test_classify_cell_missed():
    assert classify_cell("contract_number", "22847", None) == MISSED


def test_classify_cell_hallucinated():
    assert classify_cell("parent_contract_number", None, "22847") == HALLUCINATED


def test_classify_cell_true_negative():
    assert classify_cell("parent_contract_number", None, None) == TRUE_NEGATIVE


def test_classify_cell_skipped():
    assert classify_cell("contract_number", UNLABELED, "22847") == SKIPPED


def test_classify_cell_money_within_tolerance():
    assert classify_cell("total_contract_value", 125000.00, 125000.40) == CORRECT


def test_classify_cell_money_outside_tolerance():
    assert classify_cell("total_contract_value", 125000.00, 130000.00) == WRONG_VALUE


def test_classify_cell_date_match():
    assert classify_cell("doc_date", "2023-04-12", "2023-04-12") == CORRECT


def test_classify_cell_date_mismatch():
    assert classify_cell("doc_date", "2023-04-12", "2023-04-13") == WRONG_VALUE


def test_classify_cell_bool_match():
    assert classify_cell("auto_renewal_flag", True, 1) == CORRECT
    assert classify_cell("auto_renewal_flag", False, 0) == CORRECT


def test_classify_cell_int_match():
    assert classify_cell("termination_notice_days", 60, 60) == CORRECT
    assert classify_cell("termination_notice_days", 60, 30) == WRONG_VALUE


def test_classify_cell_county_department_fuzzy_match():
    assert classify_cell(
        "county_department", "Lake County Department of Public Works", "LAKE COUNTY DEPARTMENT OF PUBLIC WORKS"
    ) == CORRECT


def test_classify_cell_county_department_mismatch():
    assert classify_cell("county_department", "Lake County Public Works", "Clerk of the Circuit Court") == WRONG_VALUE


def test_classify_cell_vendor_name_fuzzy_match():
    assert classify_cell("vendor_name", "Johnson Controls Inc.", "johnson controls") == CORRECT


def test_classify_cell_vendor_name_mismatch():
    assert classify_cell("vendor_name", "Johnson Controls Inc.", "Acme Corp") == WRONG_VALUE


def test_normalize_vendor_name_strips_suffix_and_punct():
    assert normalize_vendor_name("Johnson Controls, Inc.") == "johnson controls"


def test_classify_cell_renewal_options_match():
    assert classify_cell("renewal_options", "3 x 1-year options", "3 x 1-year") == CORRECT


def test_classify_cell_renewal_options_review_when_unparseable():
    assert classify_cell("renewal_options", "auto-extends indefinitely", "3 x 1-year") == REVIEW


def test_normalize_renewal_options_basic():
    assert normalize_renewal_options("3 x 1-year options") == (3, "1-year")
    assert normalize_renewal_options("two renewals") is None


def test_score_document_fields_ignores_non_schema_keys():
    gold = {"contract_number": "123", "notes": "irrelevant"}
    pred = {"contract_number": "123"}
    result = score_document_fields(gold, pred)
    assert result == {"contract_number": CORRECT}


def test_aggregate_extraction_overall_metrics():
    results = [
        {
            "source_filename": "doc1.pdf",
            "doc_type": "fully_executed_agreement",
            "field_buckets": {
                "contract_number": CORRECT,
                "vendor_name": CORRECT,
                "total_contract_value": WRONG_VALUE,
                "parent_contract_number": TRUE_NEGATIVE,
                "renewal_options": MISSED,
            },
        },
        {
            "source_filename": "doc2.pdf",
            "doc_type": "renewal_letter",
            "field_buckets": {
                "contract_number": CORRECT,
                "parent_contract_number": HALLUCINATED,
            },
        },
    ]
    metrics = aggregate_extraction(results)
    # extracted (non-inferred) populated cells: correct=3, wrong=1, missed=1 -> 3/5
    assert metrics["field_accuracy"] == 3 / 5
    # null cells: hallucinated=1, true_negative=1 -> 1/2
    assert metrics["hallucination_rate"] == 1 / 2
    assert "fully_executed_agreement" in metrics["by_doc_type"]
    assert "renewal_letter" in metrics["by_doc_type"]


def test_aggregate_extraction_skips_unlabeled_with_warning():
    results = [
        {
            "source_filename": "doc1.pdf",
            "doc_type": "fully_executed_agreement",
            "field_buckets": {"contract_number": SKIPPED},
        }
    ]
    metrics = aggregate_extraction(results)
    assert metrics["field_accuracy"] is None
    assert len(metrics["warnings"]) == 1


def test_aggregate_extraction_inferred_fields_reported_separately():
    results = [
        {
            "source_filename": "doc1.pdf",
            "doc_type": "fully_executed_agreement",
            "field_buckets": {
                "contract_number": WRONG_VALUE,  # extracted, intentionally wrong
                "service_category": CORRECT,  # inferred
            },
        }
    ]
    metrics = aggregate_extraction(results)
    assert metrics["field_accuracy"] == 0.0  # only the wrong_value extracted field counts
    assert metrics["inferred_field_accuracy"] == 1.0


def test_aggregate_extraction_review_cases_excluded_from_accuracy():
    results = [
        {
            "source_filename": "doc1.pdf",
            "doc_type": "fully_executed_agreement",
            "field_buckets": {"renewal_options": REVIEW, "contract_number": CORRECT},
        }
    ]
    metrics = aggregate_extraction(results)
    assert metrics["field_accuracy"] == 1.0
    assert metrics["review_cases"] == [{"source_filename": "doc1.pdf", "field": "renewal_options"}]


def test_aggregate_classification_accuracy_and_confusion_matrix():
    labels = [
        {"source_filename": "a.pdf", "gold_doc_type": "renewal_letter", "pred_doc_type": "renewal_letter"},
        {"source_filename": "b.pdf", "gold_doc_type": "renewal_letter", "pred_doc_type": "vendor_disclosure_statement"},
        {"source_filename": "c.pdf", "gold_doc_type": UNLABELED, "pred_doc_type": "other"},
    ]
    result = aggregate_classification(labels)
    assert result["n_labeled"] == 2
    assert result["accuracy"] == 0.5
    assert result["confusion_matrix"]["renewal_letter"]["vendor_disclosure_statement"] == 1
    assert len(result["warnings"]) == 1
