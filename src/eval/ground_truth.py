"""
Ground truth evaluation.

Loads manually verified test cases from eval/cases/ and runs them against
the extraction pipeline to compute field-level accuracy.
"""

from pathlib import Path


def load_cases(cases_dir: str | Path) -> list[dict]:
    """Load ground truth cases from JSON files in cases_dir."""
    # TODO: glob cases_dir/*.json, return list of {input_path, expected_fields} dicts
    raise NotImplementedError


def run_ground_truth_eval(cases_dir: str | Path) -> dict:
    """
    Run extraction on each ground truth case and compare to expected output.

    Returns summary metrics dict (see metrics.py).
    """
    # TODO: for each case, run pdf_parser → classifier → extractor, compare to expected
    raise NotImplementedError
