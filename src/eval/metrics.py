"""
Evaluation metric calculations.

Aggregates raw eval results into summary statistics for reporting.
"""

import pandas as pd


def field_accuracy(results: list[dict]) -> pd.DataFrame:
    """
    Compute per-field extraction accuracy across all evaluated documents.

    Returns DataFrame with columns: field, accuracy, null_rate, n_evaluated.
    """
    # TODO: compare extracted vs. expected per field, aggregate
    raise NotImplementedError


def null_rate_by_doc_type(results: list[dict]) -> pd.DataFrame:
    """
    Compute null rate per field broken down by doc_type.

    Useful for identifying fields that are structurally absent in certain doc types.
    """
    # TODO: group by doc_type, compute null fraction per field
    raise NotImplementedError


def judge_summary(judge_results: list[dict]) -> dict:
    """Aggregate LLM judge scores across all evaluated QA triples."""
    # TODO: mean faithfulness, completeness, relevance; flag low-score outliers
    raise NotImplementedError
