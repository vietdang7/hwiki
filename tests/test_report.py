from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest


FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK))

from hermes_wikiskill import report  # noqa: E402
from hermes_wikiskill.fmt import render  # noqa: E402


INJECTED = "<script>alert(1)</script>"

STATE = {
    "domain": "house-report",
    "baseline": 0.125,
    "r_best": 0.5,
    "next_iter": 3,
    "history": [
        {"iter": 1, "train_mean": 0.25, "r_val": 0.5, "accepted": True, "proposal": "create house-style",
         "r_best_before": 0.125, "skills_visible": True, "ts": "2026-09-28T20:00:00Z"},
        {"iter": 2, "train_mean": 0.375, "r_val": 0.375, "accepted": False, "proposal": "patch house-style",
         "r_best_before": 0.5, "skills_visible": True, "ts": "2026-09-28T20:30:00Z"},
    ],
}


def _result(task_id: str, score: float | None, cost: float | None) -> dict:
    usage = {"input_tokens": 100, "output_tokens": 20}
    if cost is not None:
        usage["cost_usd"] = cost
    return {"task_id": task_id, "score": score, "duration_s": 3.0, "exit_code": 0, "usage": usage,
            "skill_views": [], "session": "s"}


def _summary(tag: str, skills: str, sha: str, reps: list[list[dict]], means: list[float]) -> dict:
    return {
        "tag": tag, "ws": "/w", "skills": skills, "skill_names": ["house-style"] if skills == "active" else [],
        "active_commit": "abc123", "model": "deepseek-v4-flash", "hermes_version": "0.21.3", "max_turns": 15,
        "tasks_file": f"/bench/{sha}.json", "tasks_sha256": sha,
        "repeats": [{"rep": i + 1, "mean": m, "results": r} for i, (r, m) in enumerate(zip(reps, means))],
        "mean": sum(means) / len(means), "started": "2026-09-28T21:00:00Z", "finished": f"2026-09-28T21:{len(tag):02d}:00Z",
    }


def _acceptance(target: str, statuses: dict[str, str]) -> dict:
    checks = [{"id": cid, "name": f"name of {cid}", "status": st,
               "evidence": f"{target} evidence for {cid}: see https://example.com/x <b>bold</b>"}
              for cid, st in statuses.items()]
    return {"target": target, "host": "host-test", "timestamp": "2026-09-28T19:00:00Z", "hermes_version": "0.21.3",
            "checks": checks,
            "summary": {"pass": sum(s == "PASS" for s in statuses.values()),
                        "fail": sum(s == "FAIL" for s in statuses.values()),
                        "skip": sum(s == "SKIP" for s in statuses.values())}}


DOCTOR = {"tool": "hermes-wikiskill", "version": "0.2.0", "ok": True,
          "checks": [{"id": "python", "status": "ok", "detail": "3.12"},
                     {"id": "ram", "status": "warn", "detail": "850 MB free"}]}


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


@pytest.fixture()
def fixture(tmp_path: Path) -> dict:
    ws = tmp_path / "ws"
    _write(ws / "runs" / "state.json", STATE)
    _write(ws / "skills" / "active" / "house-style" / "SKILL.md", "---\nname: house-style\n---\n")
    (ws / "skills" / "active" / "not-a-skill").mkdir(parents=True)

    empty = _summary("test-empty", "empty", "test-sha",
                     [[_result("t1", 0.0, 0.001), _result(INJECTED, 0.2, 0.002)],
                      [_result("t1", 0.2, 0.001), _result(INJECTED, 0.2, None)]], [0.1, 0.2])
    active = _summary("test-active", "active", "test-sha",
                      [[_result("t1", 0.6, 0.003), _result(INJECTED, 0.6, 0.003)],
                       [_result("t1", 0.8, 0.003), _result(INJECTED, 0.6, 0.003)]], [0.6, 0.7])
    calib = _summary("val-calib", "empty", "val-sha", [[_result("v1", 0.0, 0.004)]], [0.0])
    for s in (empty, active, calib):
        _write(ws / "eval" / s["tag"] / "summary.json", s)

    _write(ws / "raw" / "traces" / "iter-01" / "train" / "a.meta.json",
           {"task_id": "a", "split": "train", "score": 1.0, "usage": {"cost_usd": 0.003}})
    _write(ws / "raw" / "traces" / "iter-01" / "val" / "b.meta.json", {"task_id": "b", "split": "val", "score": 0.0})
    _write(ws / "runs" / "maintain-01" / "meta.json", {"usage": {"cost_usd": 0.01}})
    _write(ws / "runs" / "propose-01" / "meta.json", {"usage": {"input_tokens": 5}})
    _write(ws / "runs" / "iter-01" / "train" / "a" / "meta.json", {"usage": {"cost_usd": 5.0}})  # covered by trace
    _write(ws / "runs" / "proposals" / "iter-01.json", {"action": "create", "usage": {"cost_usd": 9.0}})

    acc = tmp_path / "acc"
    stock = _acceptance("stock", {"AC01-install-clean": "PASS", "AC03-skills-visible-live": "FAIL"})
    current = _acceptance("current", {"AC01-install-clean": "PASS", "AC03-skills-visible-live": "PASS"})
    framework = _acceptance("framework", {"AC01-install-clean": "PASS", "AC03-skills-visible-live": "PASS",
                                          "AC14-workspace-lock": "SKIP"})
    for doc in (stock, current, framework):
        _write(acc / f"{doc['target']}.json", doc)
    return {"ws": ws, "acc": acc, "stock": stock, "current": current, "framework": framework}


def test_costs_include_retried_role_attempts(fixture: dict) -> None:
    """INTERFACE §6: kept attempts runs/maintain-NN/attempt-<k>/run.json and propose-NN/attempt-<k>/run.json count."""
    ws = fixture["ws"]
    _write(ws / "runs" / "maintain-01" / "attempt-1" / "run.json", {"usage": {"cost_usd": 0.02}})
    _write(ws / "runs" / "propose-01" / "attempt-1" / "run.json", {"usage": {"cost_usd": 0.03}})
    _write(ws / "runs" / "propose-01" / "attempt-2" / "run.json", {"usage": {"cost_usd": 0.04}})
    runs = report.build(str(ws), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR)["costs"]["by_source"]["runs"]
    assert runs == {"total_usd": 0.1, "runs": 5, "runs_without_cost": 1}


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_build_numbers_equal_fixture_artifacts(fixture: dict) -> None:
    data = report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR)

    assert data["ws"] == str(fixture["ws"])
    assert data["doctor"] == DOCTOR
    assert data["acceptance"] == {t: fixture[t] for t in ("stock", "current", "framework")}

    ev = data["evolve"]
    assert (ev["baseline"], ev["r_best"], ev["next_iter"]) == (0.125, 0.5, 3)
    assert ev["history"] == STATE["history"]
    assert ev["accepted_iters"] == [1]
    assert ev["active_skills"] == ["house-style"]

    eff = data["effectiveness"]
    assert len(eff["evals"]) == 3
    assert eff["empty_mean"] == 0.15
    assert eff["active_mean"] == 0.65
    assert eff["delta"] == 0.5
    assert eff["basis"] == {"tasks_sha256": "test-sha", "tasks_file": "/bench/test-sha.json"}
    group = next(g for g in eff["groups"] if g["tasks_sha256"] == "test-sha")
    assert group["empty_repeat_means"] == [0.1, 0.2]
    assert group["active_repeat_means"] == [0.6, 0.7]
    t1 = next(t for t in group["tasks"] if t["task_id"] == "t1")
    assert t1["empty_scores"] == [0.0, 0.2] and t1["active_scores"] == [0.6, 0.8]
    assert (t1["empty_mean"], t1["active_mean"], t1["delta"]) == (0.1, 0.7, 0.6)
    calib = next(g for g in eff["groups"] if g["tasks_sha256"] == "val-sha")
    assert calib["delta"] is None and calib["empty_mean"] == 0.0

    costs = data["costs"]
    assert costs["by_source"]["traces"] == {"total_usd": 0.003, "runs": 2, "runs_without_cost": 1}
    assert costs["by_source"]["runs"] == {"total_usd": 0.01, "runs": 2, "runs_without_cost": 1}
    assert costs["by_source"]["eval"] == {"total_usd": 0.02, "runs": 9, "runs_without_cost": 1}
    assert costs["total_usd"] == 0.033
    assert (costs["runs"], costs["runs_without_cost"]) == (13, 3)
    assert data["notes"] == []


def test_build_is_read_only(fixture: dict) -> None:
    before = _tree_digest(fixture["ws"].parent)
    report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR)
    assert _tree_digest(fixture["ws"].parent) == before


def test_missing_artifacts_become_null_with_notes(tmp_path: Path) -> None:
    ws = tmp_path / "empty-ws"
    ws.mkdir()
    data = report.build(str(ws), acceptance_dir=str(tmp_path / "no-acc"))

    assert data["doctor"] is None
    assert data["acceptance"] == {"stock": None, "current": None, "framework": None}
    assert data["evolve"] is None
    eff = data["effectiveness"]
    assert eff["evals"] == [] and eff["empty_mean"] is None and eff["active_mean"] is None and eff["delta"] is None
    assert data["costs"]["total_usd"] == 0.0 and data["costs"]["runs"] == 0
    notes = "\n".join(data["notes"])
    for needle in ("doctor result", "stock.json", "current.json", "framework.json", "state.json",
                   str(ws / "eval")):
        assert needle in notes
    assert all(n.startswith(("missing: ", "unreadable: ")) for n in data["notes"])

    page = report.render_html(data)
    assert "Missing artifacts" in page
    assert page.count("Missing:") >= 4


def test_unreadable_json_and_unpaired_evals_are_notes_not_guesses(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write(ws / "runs" / "state.json", "{not json")
    _write(ws / "eval" / "only-empty" / "summary.json",
           _summary("only-empty", "empty", "sha", [[_result("t1", 0.5, None)]], [0.5]))
    data = report.build(str(ws), acceptance_dir=str(tmp_path), doctor=DOCTOR)
    assert data["evolve"] is None
    assert any(n.startswith("unreadable: ") and "state.json" in n for n in data["notes"])
    assert data["effectiveness"]["delta"] is None
    assert any("both an empty-skills and an active-skills eval" in n for n in data["notes"])
    assert data["costs"]["total_usd"] is None  # one eval run, cost unknown → not 0
    assert data["costs"]["runs_without_cost"] == 1


def test_eval_dir_override_is_used(fixture: dict, tmp_path: Path) -> None:
    other = tmp_path / "other-eval"
    _write(other / "x" / "summary.json", _summary("x", "active", "s", [[_result("t", 1.0, 0.5)]], [1.0]))
    data = report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR,
                        eval_dir=str(other))
    assert [e["tag"] for e in data["effectiveness"]["evals"]] == ["x"]
    assert data["costs"]["by_source"]["eval"]["total_usd"] == 0.5


def test_html_is_offline_escaped_and_complete(fixture: dict) -> None:
    data = report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR)
    page = report.render_html(data)

    assert page.startswith("<!doctype html>")
    for forbidden in ("http://", "https://", "//cdn", "//"):
        assert forbidden not in page
    assert not re.search(r"""(?:src|href)\s*=""", page, re.I)
    assert "@import" not in page and "url(" not in page

    assert INJECTED not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>bold</b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page

    for doc in (fixture["stock"], fixture["current"], fixture["framework"]):
        for check in doc["checks"]:
            assert check["id"] in page
    for label in ("Install / doctor", "Acceptance", "Evolve history", "Held-out effectiveness", "Costs"):
        assert label in page
    assert "&amp;" not in page  # no double-escaped entities in labels
    assert "<svg" in page and "polyline" in page
    assert "house-style" in page and "+0.5" in page and "$0.0330" in page


def test_html_has_light_dark_and_toggle(fixture: dict) -> None:
    page = report.render_html(report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR))
    assert "prefers-color-scheme: dark" in page
    assert ':root[data-theme="dark"]' in page
    assert 'id="theme-toggle"' in page
    assert 'name="viewport"' in page
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))


def test_embedded_json_round_trips(fixture: dict) -> None:
    data = report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR)
    page = report.render_html(data)
    island = re.search(r'<script type="application/json" id="hws-data">(.*?)</script>', page, re.S)
    assert island is not None
    assert json.loads(island.group(1)) == data


def test_render_html_tolerates_garbage_shapes() -> None:
    page = report.render_html({"doctor": "x", "acceptance": {"stock": {"checks": ["bad", {"id": 1}]}},
                               "evolve": {"history": [{"r_val": "nan"}]}, "effectiveness": {"evals": [1]},
                               "costs": {"by_source": {"traces": None}}, "notes": [None, "missing: x"]})
    assert page.startswith("<!doctype html>") and "missing: x" in page


def test_fmt_report_summary(fixture: dict) -> None:
    data = report.build(str(fixture["ws"]), acceptance_dir=str(fixture["acc"]), doctor=DOCTOR)
    text = render("report", data)
    assert text.splitlines()[0] == f"Report: {fixture['ws']}"
    assert "Doctor: OK" in text
    assert "framework pass=2 fail=0 skip=1" in text
    assert "stock     pass=1 fail=1 skip=0" in text
    assert "Evolve: baseline=0.125 r_best=0.5 iterations=2 accepted=1" in text
    assert "Effectiveness: empty=0.15 evolved=0.65 delta=0.5 evals=3" in text
    assert "Costs: total_usd=0.033 runs=13 runs_without_cost=3" in text
    assert "\x1b" not in text


def test_fmt_report_missing_and_empty_shapes() -> None:
    assert render("report", {}).strip()
    text = render("report", {"ws": "/w", "doctor": None, "acceptance": None, "evolve": None,
                             "effectiveness": None, "costs": None, "notes": ["missing: /w/runs/state.json"]})
    assert "Doctor: missing" in text and "Evolve: missing" in text
    assert "framework missing" in text
    assert "missing: /w/runs/state.json" in text


SKILL = FRAMEWORK / "skills" / "hermes-wikiskill" / "SKILL.md"
VERBS = {"init", "bench", "status", "evolve", "gate", "compare", "transfer", "run-task", "maintain", "propose",
         "reset", "version", "doctor", "setup-profile", "eval", "promote", "demote", "report"}


def test_operator_skill_frontmatter_and_commands() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front, body = text[4:].split("\n---\n", 1)
    fields = dict(line.split(":", 1) for line in front.splitlines() if line and not line.startswith(" ") and ":" in line)
    assert fields["name"].strip() == "hermes-wikiskill"
    description = fields["description"].strip().strip('"')
    assert 0 < len(description) <= 200
    assert "wikiskill" in description.lower()

    used = set(re.findall(r"\bhws (?:--[\w-]+ \S+ )*([a-z][a-z-]+)", body))
    assert used and used <= VERBS, used - VERBS
    assert {"status", "report", "doctor", "promote", "demote", "evolve"} <= used
    assert "--dry-run" in body and "--i-am-owner" in body and "OWNER-APPROVAL" in body
    assert "~/ops/hermes-wikiskill/workspaces" in body
    secret_like = "sk-[A-Za-z0-9_-]{20,}|" + "_API_KEY" + "=|[0-9]{8,10}:[A-Za-z0-9_-]{35}"  # split: keep C7 clean
    assert not re.search(secret_like, text)
