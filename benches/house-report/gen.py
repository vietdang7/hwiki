#!/usr/bin/env python3
"""Generate the deterministic ACME house-report train/val and held-out data.

VAL and TEST graders are SEALED: they carry only ``expected_sha256`` (sha256 hex
of the UTF-8 ``normalize(expected)``), so no file an agent can reach contains a
val/test answer (D19). TRAIN graders keep plaintext ``expected`` (the framework's
TRAIN-only grader feedback needs it). ``--plain OUT_DIR`` writes the
upstream-compatible plaintext files for the stock comparison and tests only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import re
import sys
from pathlib import Path
from typing import Any


SEED = 20260928
SEALED_SPLITS = ("val", "test")
OUTPUTS = ("tasks.json", "test.json")
SPLIT_OFFSETS = {"train": 1_000, "val": 120_000, "test": 240_000}
COUNTS = {
    "train": (("sales", 3), ("expenses", 3), ("orders", 2)),
    "val": (("sales", 2), ("expenses", 2), ("orders", 2)),
    "test": (("sales", 4), ("expenses", 3), ("orders", 3)),
}
FAMILIES = {
    "sales": {
        "filename": "sales.csv",
        "field": "region",
        "groups": ("North", "East", "West"),
        "active_status": "posted",
    },
    "expenses": {
        "filename": "expenses.json",
        "field": "category",
        "groups": ("Travel", "Meals", "Software"),
        "active_status": "approved",
    },
    "orders": {
        "filename": "orders.jsonl",
        "field": "team",
        "groups": ("Blue", "Gold", "Red"),
        "active_status": "complete",
    },
}


def normalize(value: object) -> str:
    """Same as hermes_wikiskill.scoring.normalize (upstream whitespace rule)."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def answer_sha256(expected: str) -> str:
    return hashlib.sha256(normalize(expected).encode("utf-8")).hexdigest()


def seal(task: dict[str, Any]) -> dict[str, Any]:
    """Replace a val/test plaintext answer by its hash; train tasks are returned unchanged."""
    if task["split"] not in SEALED_SPLITS:
        return task
    grader = {k: v for k, v in task["grader"].items() if k != "expected"}
    grader["expected_sha256"] = answer_sha256(task["grader"]["expected"])
    return {**task, "grader": grader}


def _money(cents: int) -> str:
    whole, fraction = divmod(cents, 100)
    return f"{whole}.{fraction:02d}"


def _expected(report_id: str, rows: list[dict[str, Any]], group_field: str) -> str:
    totals: dict[str, int] = {}
    for row in rows:
        if row["status"] == "void":
            continue
        group = str(row[group_field])
        totals[group] = totals.get(group, 0) + int(row["amount_cents"])
    lines = [f"== {report_id.upper()} =="]
    lines.extend(
        f"{group} | {_money(amount)}"
        for group, amount in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    )
    lines.append(f"TOTAL | {_money(sum(totals.values()))}")
    return "\n".join(lines) + "\n"


def _make_rows(
    task_id: str, family: dict[str, Any], base: int, rng: random.Random
) -> list[dict[str, Any]]:
    group_field = family["field"]
    first, middle, last = family["groups"]
    active_status = family["active_status"]
    specs = (
        (first, base + 400, active_status),
        (first, base + 900, active_status),
        (middle, base + 1_000, active_status),
        (middle, base + 100, active_status),
        (last, base + 800, active_status),
        (last, base + 500, active_status),
        (middle, base + 2_800, "void"),
    )
    rows = []
    for index, (group, amount, status) in enumerate(specs, start=1):
        rows.append(
            {
                "record_id": f"{task_id}-r{index:02d}",
                group_field: group,
                "amount_cents": amount,
                "status": status,
            }
        )
    rng.shuffle(rows)
    return rows


def _file_content(family_name: str, rows: list[dict[str, Any]]) -> str:
    filename = FAMILIES[family_name]["filename"]
    if filename.endswith(".csv"):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=("record_id", "region", "amount_cents", "status"),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()
    if filename.endswith(".json"):
        return json.dumps({"expenses": rows}, ensure_ascii=False, indent=2) + "\n"
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def generate(plain: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(train+val, test) tasks; val/test graders sealed unless ``plain``."""
    rng = random.Random(SEED)
    workspace_tasks: list[dict[str, Any]] = []
    held_out_tasks: list[dict[str, Any]] = []
    local_index = {split: 0 for split in COUNTS}

    for split, family_counts in COUNTS.items():
        destination = held_out_tasks if split == "test" else workspace_tasks
        for family_name, count in family_counts:
            family = FAMILIES[family_name]
            for family_index in range(1, count + 1):
                index = local_index[split]
                local_index[split] += 1
                task_id = f"{split}-{family_name}-{family_index:02d}"
                base = SPLIT_OFFSETS[split] + index * 5_000 + rng.randrange(0, 250)
                rows = _make_rows(task_id, family, base, rng)
                filename = family["filename"]
                field_label = family["field"]
                task = {
                    "id": task_id,
                    "split": split,
                    "title": f"ACME {family_name} summary {family_index:02d}",
                    "prompt": (
                        f"Create report.txt for report id {task_id}, following the ACME "
                        f"house report format. Summarize the {family_name} by {field_label} "
                        f"from {filename}. Return only the report contents."
                    ),
                    "sandbox": {filename: _file_content(family_name, rows)},
                    "grader": {
                        "type": "exact",
                        "file": "report.txt",
                        "expected": _expected(task_id, rows, field_label),
                    },
                }
                destination.append(task)

    if plain:
        return workspace_tasks, held_out_tasks
    return [seal(t) for t in workspace_tasks], [seal(t) for t in held_out_tasks]


def _json_bytes(value: list[dict[str, Any]]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify checked-in JSON files match deterministic generation",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory for tasks.json and test.json (defaults to this directory)",
    )
    parser.add_argument(
        "--plain",
        type=Path,
        metavar="OUT_DIR",
        help="write UNSEALED (plaintext val/test answers) tasks.json/test.json to OUT_DIR only "
        "(stock comparison, tests); never into this bench directory",
    )
    args = parser.parse_args(argv)

    if args.plain is not None:
        if args.check:
            parser.error("--plain and --check are exclusive")
        out = args.plain.resolve()
        if out == Path(__file__).resolve().parent:
            print("refusing to write plaintext answers into the bench directory", file=sys.stderr)
            return 2
        train_val, held_out = generate(plain=True)
        out.mkdir(parents=True, exist_ok=True)
        (out / "tasks.json").write_bytes(_json_bytes(train_val))
        (out / "test.json").write_bytes(_json_bytes(held_out))
        print(f"wrote PLAINTEXT (unsealed) tasks.json and test.json to {out}")
        return 0

    train_val, held_out = generate()
    generated = {"tasks.json": _json_bytes(train_val), "test.json": _json_bytes(held_out)}
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.check:
        mismatches = []
        for name, expected in generated.items():
            path = args.output_dir / name
            if not path.is_file() or path.read_bytes() != expected:
                mismatches.append(name)
        if mismatches:
            print("Generated JSON differs: " + ", ".join(mismatches), file=sys.stderr)
            return 1
        print("house-report JSON is deterministic and up to date")
        return 0

    for name, contents in generated.items():
        (args.output_dir / name).write_bytes(contents)
    print(f"wrote {len(train_val)} train/val tasks and {len(held_out)} held-out tasks to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
