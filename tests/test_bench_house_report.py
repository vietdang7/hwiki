from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


FRAMEWORK = Path(__file__).resolve().parents[1]
ROOT = FRAMEWORK.parent
BENCH = FRAMEWORK / "benches" / "house-report"
GEN = BENCH / "gen.py"
WORKSPACE_TASKS = json.loads((BENCH / "tasks.json").read_text(encoding="utf-8"))
TEST_TASKS = json.loads((BENCH / "test.json").read_text(encoding="utf-8"))


def _input_records(task: dict) -> tuple[str, list[dict]]:
    files = task["sandbox"]
    if "sales.csv" in files:
        rows = list(csv.DictReader(files["sales.csv"].splitlines()))
        return "region", rows
    if "expenses.json" in files:
        rows = json.loads(files["expenses.json"])["expenses"]
        return "category", rows
    assert "orders.jsonl" in files
    rows = [json.loads(line) for line in files["orders.jsonl"].splitlines() if line]
    return "team", rows


def _reference_report(task: dict) -> str:
    """Independent grader oracle, deliberately separate from gen.py helpers."""
    field, rows = _input_records(task)
    totals: dict[str, int] = {}
    for row in rows:
        if row["status"] == "void":
            continue
        group = row[field]
        totals[group] = totals.get(group, 0) + int(row["amount_cents"])

    report_id = re.search(r"report id ([a-z0-9-]+)", task["prompt"])
    assert report_id is not None

    def as_units(cents: int) -> str:
        return f"{cents // 100}.{cents % 100:02d}"

    result = [f"== {report_id.group(1).upper()} =="]
    for group, cents in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        result.append(f"{group} | {as_units(cents)}")
    result.append(f"TOTAL | {as_units(sum(totals.values()))}")
    return "\n".join(result) + "\n"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _validate_upstream_style(tasks: list[dict], allowed_splits: set[str]) -> None:
    seen = set()
    for task in tasks:
        assert set(("id", "split", "title", "prompt", "sandbox", "grader")) <= set(task)
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", task["id"])
        assert task["id"] not in seen
        seen.add(task["id"])
        assert task["split"] in allowed_splits
        assert task["prompt"]
        assert task["sandbox"]
        assert task["grader"]["type"] == "exact"
        assert task["grader"]["file"] == "report.txt"
        if task["split"] == "train":  # plaintext: TRAIN-only grader feedback needs it
            assert set(task["grader"]) == {"type", "file", "expected"}
            assert task["grader"]["expected"].endswith("\n")
        else:  # D19: val/test sealed, no plaintext answer anywhere
            assert set(task["grader"]) == {"type", "file", "expected_sha256"}
            assert re.fullmatch(r"[0-9a-f]{64}", task["grader"]["expected_sha256"])


def test_deterministic_regeneration_and_committed_check(tmp_path: Path) -> None:
    checked = subprocess.run(
        [sys.executable, str(GEN), "--check"], cwd=ROOT, capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr

    generated = subprocess.run(
        [sys.executable, str(GEN), "--output-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    first = {name: (tmp_path / name).read_bytes() for name in ("tasks.json", "test.json")}
    generated_again = subprocess.run(
        [sys.executable, str(GEN), "--output-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert generated_again.returncode == 0, generated_again.stdout + generated_again.stderr
    assert first == {name: (tmp_path / name).read_bytes() for name in first}


def test_workspace_file_has_only_train_and_val_upstream_tasks() -> None:
    _validate_upstream_style(WORKSPACE_TASKS, {"train", "val"})
    assert len(WORKSPACE_TASKS) == 14
    assert sum(task["split"] == "train" for task in WORKSPACE_TASKS) == 8
    assert sum(task["split"] == "val" for task in WORKSPACE_TASKS) == 6


def test_held_out_file_has_test_split_and_disjoint_ids_and_records() -> None:
    _validate_upstream_style(TEST_TASKS, {"test"})
    assert len(TEST_TASKS) == 10

    workspace_ids = {task["id"] for task in WORKSPACE_TASKS}
    test_ids = {task["id"] for task in TEST_TASKS}
    assert workspace_ids.isdisjoint(test_ids)

    def signatures(tasks: list[dict]) -> set[tuple[str, int, str]]:
        records = set()
        for task in tasks:
            field, rows = _input_records(task)
            records.update((row[field], int(row["amount_cents"]), row["status"]) for row in rows)
        return records

    assert signatures(WORKSPACE_TASKS).isdisjoint(signatures(TEST_TASKS))


def _sha(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def test_independent_reference_solver_reproduces_every_expected_output() -> None:
    for task in WORKSPACE_TASKS + TEST_TASKS:
        if task["split"] == "train":
            assert task["grader"]["expected"] == _reference_report(task), task["id"]
        else:
            assert task["grader"]["expected_sha256"] == _sha(_reference_report(task)), task["id"]


def test_plain_mode_writes_unsealed_upstream_files_elsewhere_only(tmp_path: Path) -> None:
    out = tmp_path / "plain"
    proc = subprocess.run([sys.executable, str(GEN), "--plain", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    plain = json.loads((out / "tasks.json").read_text()) + json.loads((out / "test.json").read_text())
    sealed = {t["id"]: t for t in WORKSPACE_TASKS + TEST_TASKS}
    for task in plain:  # upstream schema: plaintext `expected` on every split, identical otherwise
        assert task["grader"]["expected"] == _reference_report(task), task["id"]
        other = sealed[task["id"]]
        assert {k: v for k, v in task.items() if k != "grader"} == {k: v for k, v in other.items() if k != "grader"}
        if task["split"] != "train":
            assert other["grader"]["expected_sha256"] == _sha(task["grader"]["expected"])

    refused = subprocess.run([sys.executable, str(GEN), "--plain", str(BENCH)], capture_output=True, text=True)
    assert refused.returncode == 2 and "refusing" in refused.stderr
    assert subprocess.run([sys.executable, str(GEN), "--check"], capture_output=True).returncode == 0


def test_no_val_or_test_answer_text_in_committed_bench_files() -> None:
    answers = [_reference_report(t) for t in WORKSPACE_TASKS + TEST_TASKS if t["split"] != "train"]
    needles = set()
    for answer in answers:
        needles.add(answer.strip())
        needles.add(_normalize(answer))
        needles.update(line for line in answer.splitlines() if " | " in line and not line.startswith("TOTAL"))
        needles.add(answer.splitlines()[0])  # the "== VAL-...-NN ==" header
    for path in sorted(p for p in BENCH.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        text = path.read_text(encoding="utf-8", errors="replace")
        text_json = text + json.dumps(text)  # also escaped forms
        for needle in needles:
            assert needle not in text_json, f"{path.name} contains val/test answer text {needle!r}"


def test_plausible_but_unconventional_report_fails_exact_grading() -> None:
    task = WORKSPACE_TASKS[0]
    expected = task["grader"]["expected"]
    report_id = re.search(r"report id ([a-z0-9-]+)", task["prompt"]).group(1)
    plausible_variant = expected.replace(
        f"== {report_id.upper()} ==", f"== {report_id} ==", 1
    )
    assert _normalize(plausible_variant) != _normalize(expected)
