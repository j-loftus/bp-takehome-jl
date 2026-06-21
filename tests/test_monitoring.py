"""Unit tests for eval/monitoring.py — snapshot aggregation and drift detection.

Judge calls are monkeypatched (no real API spend); these tests exercise the
aggregation/comparison logic, which is what's reused by run_eval.py.
"""

import eval.monitoring as monitoring


def _fake_judge_extraction(record):
    return {
        "source_filename": record["source_filename"],
        "per_field": {"contract_number": {"supported": True}, "vendor_name": {"supported": False, "reason": "not found"}},
        "doc_faithfulness_score": 4,
        "judge_method": "text",
    }


def _fake_judge_chat(question, retrieved_chunks, answer):
    return {"faithfulness": 5, "relevance": 4}


def test_monitoring_snapshot_basic(monkeypatch):
    monkeypatch.setattr(monitoring, "judge_extraction", _fake_judge_extraction)
    monkeypatch.setattr(monitoring, "judge_chat", _fake_judge_chat)

    batch = {
        "extraction_records": [
            {
                "source_filename": "a.pdf",
                "doc_type": "renewal_letter",
                "extracted_fields": {"contract_number": "1", "vendor_name": "Acme"},
                "is_scanned": False,
                "extraction_failed": False,
            },
            {
                "source_filename": "b.pdf",
                "doc_type": "renewal_letter",
                "extracted_fields": {"contract_number": "2", "vendor_name": None},
                "is_scanned": True,
                "extraction_failed": False,
            },
        ],
        "chat_records": [{"question": "q", "retrieved_chunks": [], "answer": "a"}],
        "classification_records": [{"source_filename": "a.pdf", "confidence": "high"}, {"source_filename": "b.pdf", "confidence": "low"}],
    }

    snapshot = monitoring.monitoring_snapshot(batch)

    assert snapshot["n_extraction_records"] == 2
    assert snapshot["extraction_judge_faithfulness"]["mean"] == 4.0
    assert snapshot["chat_judge_faithfulness"]["mean"] == 5.0
    assert snapshot["scanned_document_rate"] == 0.5
    assert snapshot["classification_confidence_distribution"] == {"high": 1, "low": 1}
    assert len(snapshot["hallucination_flags"]) == 2  # vendor_name unsupported on both docs


def test_compare_to_baseline_flags_judge_score_drop():
    snapshot = {
        "extraction_judge_faithfulness": {"mean": 2.0},
        "chat_judge_faithfulness": {"mean": 5.0},
        "null_rate_by_doc_type": {},
        "scanned_document_rate": 0.1,
        "classification_confidence_distribution": {"high": 9, "low": 1},
        "extraction_failure_rate": 0.0,
    }
    baseline = {
        "extraction_judge_faithfulness": {"mean": 4.5},
        "chat_judge_faithfulness": {"mean": 4.8},
        "null_rate_by_doc_type": {},
        "scanned_document_rate": 0.1,
        "classification_confidence_distribution": {"high": 9, "low": 1},
        "extraction_failure_rate": 0.0,
    }
    alerts = monitoring.compare_to_baseline(snapshot, baseline)
    assert any("extraction_judge_faithfulness" in a for a in alerts)
    assert not any("chat_judge_faithfulness" in a for a in alerts)


def test_compare_to_baseline_no_alerts_when_stable():
    snapshot = {
        "extraction_judge_faithfulness": {"mean": 4.5},
        "chat_judge_faithfulness": {"mean": 4.8},
        "null_rate_by_doc_type": {"renewal_letter": 0.2},
        "scanned_document_rate": 0.25,
        "classification_confidence_distribution": {"high": 9, "low": 1},
        "extraction_failure_rate": 0.01,
    }
    baseline = dict(snapshot)
    alerts = monitoring.compare_to_baseline(snapshot, baseline)
    assert alerts == []


def test_save_and_load_baseline_roundtrip(tmp_path):
    path = tmp_path / "baseline.json"
    snapshot = {"extraction_judge_faithfulness": {"mean": 4.0}, "scanned_document_rate": 0.2}
    monitoring.save_baseline(snapshot, path)
    loaded = monitoring.load_baseline(path)
    assert loaded == snapshot
