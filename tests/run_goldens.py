#!/usr/bin/env python3
"""Golden-file regression harness for the retail extractor.

Drop real supplier files into tests/goldens/files/ (gitignored — they usually
contain client pricing). Snapshot the current behaviour once, then run this before
every deploy. A prompt tweak that fixes one supplier and quietly breaks another
shows up here instead of in a client's catalogue.

    python tests/run_goldens.py --update     # write/refresh the snapshot
    python tests/run_goldens.py              # compare against the snapshot

Exit code is non-zero on drift or on an audit failure, so it can gate CI.

Set GEMINI_API_KEY to exercise the AI planning path. Without a key the run still
covers the deterministic parsers, the audit, and the export shape.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.datastructures import FileStorage  # noqa: E402

import app as A  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(HERE, "goldens", "files")
SNAPSHOT = os.path.join(HERE, "goldens", "expected.json")

# Drift beyond these bounds is treated as a regression rather than noise.
TOLERANCES = {
    "row_count": 0.02,        # +/- 2% of rows
    "price_total": 0.01,      # +/- 1% of summed selling price
}
MIN_COVERAGE = 0.90
MIN_COVERAGE_STRUCTURED = 0.30


def fingerprint_rows(products: List[Dict[str, Any]]) -> str:
    """Order-independent hash of the meaningful output."""
    keys = sorted(
        "|".join([
            str(p.get("product_id") or ""),
            str(p.get("attr1_val") or ""),
            str(p.get("attr2_val") or ""),
            f"{A.parse_money(p.get('calculated_price') or p.get('selling_price')):.2f}",
        ])
        for p in products
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def measure(path: str) -> Dict[str, Any]:
    name = os.path.basename(path)
    raw = open(path, "rb").read()
    file_storage = FileStorage(stream=io.BytesIO(raw), filename=name)
    products, meta = A.parse_uploaded_file_ai_assisted(file_storage, parse_mode="variant")
    payload = A.preflight_products_payload(products, parse_mode="variant")
    final = payload["products"]
    audit = meta.get("audit") or {}

    return {
        "row_count": len(final),
        "variant_rows": sum(1 for p in final if str(p.get("variant_enabled", "")).lower() == "yes"),
        "categories": len({str(p.get("category") or "") for p in final}),
        "price_total": round(sum(A.parse_money(p.get("calculated_price") or p.get("selling_price")) for p in final), 2),
        "zero_price_rows": sum(1 for p in final if A.parse_money(p.get("calculated_price") or p.get("selling_price")) <= 0),
        "broken_variant_rows": audit.get("broken_variant_rows", 0),
        "coverage": audit.get("coverage"),
        "layout_strategy": meta.get("layout_strategy"),
        "planner_calls": meta.get("planner_calls", 0),
        "row_hash": fingerprint_rows(final),
    }


def within(actual: Any, expected: Any, tolerance: float) -> bool:
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return actual == expected
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / abs(expected) <= tolerance


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="write the snapshot instead of comparing")
    args = parser.parse_args()

    if not os.path.isdir(FILES_DIR):
        print(f"No corpus directory at {FILES_DIR}")
        return 1
    paths = sorted(
        os.path.join(FILES_DIR, f)
        for f in os.listdir(FILES_DIR)
        if f.rsplit(".", 1)[-1].lower() in {"xlsx", "xlsm", "xls", "csv"} and not f.startswith("~$")
    )
    if not paths:
        print(f"No spreadsheets in {FILES_DIR} — add supplier files to build the corpus.")
        return 1

    expected: Dict[str, Any] = {}
    if os.path.exists(SNAPSHOT) and not args.update:
        expected = json.load(open(SNAPSHOT, encoding="utf-8"))

    results: Dict[str, Any] = {}
    failures: List[str] = []

    header = f"{'file':44s} {'strategy':22s} {'rows':>6s} {'vars':>6s} {'cov':>6s} {'calls':>5s}  status"
    print(header)
    print("-" * len(header))

    for path in paths:
        name = os.path.basename(path)
        try:
            actual = measure(path)
        except Exception as exc:  # a crash is itself a regression
            failures.append(f"{name}: raised {exc.__class__.__name__}: {exc}")
            print(f"{name[:44]:44s} {'CRASH':22s} {'-':>6s} {'-':>6s} {'-':>6s} {'-':>5s}  FAIL")
            continue
        results[name] = actual

        notes: List[str] = []
        floor = MIN_COVERAGE_STRUCTURED if "export" in str(actual["layout_strategy"] or "") else MIN_COVERAGE
        if (actual["coverage"] or 0) < floor:
            notes.append(f"coverage {actual['coverage']} below {floor}")
        if actual["broken_variant_rows"]:
            notes.append(f"{actual['broken_variant_rows']} variant rows without Value 1")

        prior = expected.get(name)
        if prior:
            for key, tolerance in TOLERANCES.items():
                if not within(actual[key], prior.get(key), tolerance):
                    notes.append(f"{key} {prior.get(key)} -> {actual[key]}")
            if prior.get("row_hash") != actual["row_hash"] and not notes:
                notes.append(f"output changed (hash {prior.get('row_hash')} -> {actual['row_hash']})")
            if prior.get("layout_strategy") != actual["layout_strategy"]:
                notes.append(f"strategy {prior.get('layout_strategy')} -> {actual['layout_strategy']}")

        status = "ok" if not notes else "FAIL"
        if notes:
            failures.append(f"{name}: " + "; ".join(notes))
        print(
            f"{name[:44]:44s} {str(actual['layout_strategy'])[:22]:22s} "
            f"{actual['row_count']:6d} {actual['variant_rows']:6d} "
            f"{actual['coverage'] if actual['coverage'] is not None else 0:6.2f} "
            f"{actual['planner_calls']:5d}  {status}"
        )

    if args.update:
        json.dump(results, open(SNAPSHOT, "w", encoding="utf-8"), indent=2, sort_keys=True)
        print(f"\nSnapshot written: {SNAPSHOT} ({len(results)} files)")
        return 0

    if failures:
        print("\nRegressions:")
        for failure in failures:
            print("  -", failure)
        return 1

    print(f"\nAll {len(results)} golden files passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
