#!/usr/bin/env python3
"""
sample_contracts.py — Contract family sampling script

Parses contract numbers from filenames in contracts/, groups documents into
contract families, analyzes family size distribution, and runs a stratified
random sample to produce a reproducible ~100-document manifest.

Random seed: 42 (fixed for reproducibility)
Output:
  data/sample_manifest.csv   — one row per selected document
  data/unlinked_files.txt    — filenames with no parseable contract number
"""

import re
import os
import csv
import random
from pathlib import Path
from collections import defaultdict

CONTRACTS_DIR = Path(__file__).parent.parent / "contracts"
OUTPUT_DIR = Path(__file__).parent.parent / "data"
RANDOM_SEED = 42
TARGET_FAMILIES = 22  # midpoint of 20-25 per sampling.md
MIN_FAMILY_SIZE = 2   # exclude singletons

# Patterns applied in order; first match wins.
# Pattern 1: leading 5-6 digit contract number  →  18018_Award_Letter.pdf
# Pattern 2: embedded "Contract_NNNNN"          →  ...Contract_22041_Amendment...
# Pattern 3: any embedded 5-6 digit block       →  FULLY_EXECUTED_AGREEMENT_19098_...
PATTERNS = [
    re.compile(r"^(\d{5,6})_"),
    re.compile(r"[Cc]ontract[_\s](\d{4,6})"),
    re.compile(r"(?:_|^)(\d{5,6})(?:_|$)"),
]


def parse_contract_number(filename: str) -> str | None:
    stem = Path(filename).stem
    for pattern in PATTERNS:
        m = pattern.search(stem)
        if m:
            return m.group(1)
    return None


def get_size_bucket(n: int) -> str:
    if n <= 3:
        return "small"
    if n <= 6:
        return "medium"
    return "large"


def main() -> None:
    all_files = sorted(f for f in os.listdir(CONTRACTS_DIR) if f.lower().endswith(".pdf"))

    families: dict[str, list[str]] = defaultdict(list)
    unlinked: list[str] = []

    for filename in all_files:
        contract_num = parse_contract_number(filename)
        if contract_num:
            families[contract_num].append(filename)
        else:
            unlinked.append(filename)

    # ── Distribution analysis ──────────────────────────────────────────────
    family_sizes = {k: len(v) for k, v in families.items()}
    sizes = sorted(family_sizes.values())
    n_families = len(sizes)

    print("\n=== Contract Family Distribution ===")
    print(f"Total PDFs          : {len(all_files)}")
    print(f"Linked PDFs         : {sum(sizes)}")
    print(f"Unlinked PDFs       : {len(unlinked)}")
    print(f"Total families      : {n_families}")
    print(f"Min family size     : {sizes[0]}")
    print(f"Max family size     : {sizes[-1]}")
    print(f"Median family size  : {sizes[n_families // 2]}")
    print(f"Mean family size    : {sum(sizes) / n_families:.1f}")

    singletons = [c for c, s in family_sizes.items() if s == 1]
    print(f"\nSingleton families  : {len(singletons)}")
    if singletons:
        for c in sorted(singletons):
            print(f"  {c}: {families[c][0]}")

    # Eligible families (size >= MIN_FAMILY_SIZE), grouped into buckets
    buckets: dict[str, list[str]] = defaultdict(list)
    for contract_num, size in family_sizes.items():
        if size >= MIN_FAMILY_SIZE:
            buckets[get_size_bucket(size)].append(contract_num)

    print(f"\nEligible families (size >= {MIN_FAMILY_SIZE}):")
    for bucket in ("small", "medium", "large"):
        members = buckets[bucket]
        total_docs = sum(family_sizes[c] for c in members)
        print(f"  {bucket:6s}: {len(members):3d} families, {total_docs:3d} docs")

    # ── Stratified sample ─────────────────────────────────────────────────
    random.seed(RANDOM_SEED)

    eligible_count = sum(len(v) for v in buckets.values())
    selected: list[str] = []

    for bucket_name in ("small", "medium", "large"):
        bucket_families = buckets[bucket_name]
        if not bucket_families:
            continue
        proportion = len(bucket_families) / eligible_count
        n_to_sample = max(1, round(TARGET_FAMILIES * proportion))
        n_to_sample = min(n_to_sample, len(bucket_families))
        sampled = random.sample(bucket_families, n_to_sample)
        selected.extend(sampled)

    total_selected_docs = sum(family_sizes[c] for c in selected)
    print(f"\n=== Sample (seed={RANDOM_SEED}) ===")
    print(f"Selected families   : {len(selected)}")
    print(f"Selected documents  : {total_selected_docs}")

    # ── Write outputs ─────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(exist_ok=True)

    manifest_path = OUTPUT_DIR / "sample_manifest.csv"
    fieldnames = ["contract_number", "filename", "filepath", "family_size", "size_bucket"]
    with open(manifest_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for contract_num in sorted(selected):
            size = family_sizes[contract_num]
            bucket = get_size_bucket(size)
            for filename in sorted(families[contract_num]):
                writer.writerow({
                    "contract_number": contract_num,
                    "filename": filename,
                    "filepath": f"contracts/{filename}",
                    "family_size": size,
                    "size_bucket": bucket,
                })

    unlinked_path = OUTPUT_DIR / "unlinked_files.txt"
    with open(unlinked_path, "w") as f:
        for filename in unlinked:
            f.write(filename + "\n")

    print(f"\nManifest  → {manifest_path}")
    print(f"Unlinked  → {unlinked_path} ({len(unlinked)} files)\n")


if __name__ == "__main__":
    main()
