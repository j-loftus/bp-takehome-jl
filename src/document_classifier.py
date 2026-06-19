"""
Re-export shim — satisfies the spec's src/document_classifier.py path.
All implementation lives in src/pipeline/classifier.py.
"""

from src.pipeline.classifier import (
    DocType,
    classify_document,
    classify_directory,
    write_classification_results,
    print_classification_summary,
)

__all__ = [
    "DocType",
    "classify_document",
    "classify_directory",
    "write_classification_results",
    "print_classification_summary",
]
