from __future__ import annotations

import sys
from pathlib import Path

import pytest


FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK))

from hermes_wikiskill.fmt import render  # noqa: E402


@pytest.mark.parametrize(
    "kind",
    ("version", "doctor", "status", "evolve", "gate", "run-task", "eval", "promote", "demote", "setup-profile"),
)
def test_missing_json_fields_never_raise(kind: str) -> None:
    result = render(kind, {})
    assert isinstance(result, str)
    assert result.strip()
    assert "\x1b[" not in result


def test_version_uses_plain_text_and_keeps_unknown_fields() -> None:
    result = render(
        "version",
        {
            "tool": "hermes-wikiskill",
            "version": "0.2.0",
            "hermes_version": "0.21.3",
            "python": "3.12.1",
            "build": "local",
        },
    )
    assert result.splitlines()[:3] == [
        "hermes-wikiskill 0.2.0",
        "Hermes: 0.21.3",
        "Python: 3.12.1",
    ]
    assert "build : local" in result
    assert "\x1b" not in result


def test_doctor_formats_checks_and_partial_check_entries() -> None:
    result = render(
        "doctor",
        {
            "tool": "hermes-wikiskill",
            "version": "0.2.0",
            "ok": False,
            "checks": [
                {"id": "hermes-binary", "status": "fail", "detail": "missing"},
                {"id": "ram", "status": "warn"},
                {"status": "ok", "detail": "extra check"},
            ],
        },
    )
    assert "Doctor: FAILED" in result
    assert "FAIL hermes-binary: missing" in result
    assert "WARN ram" in result
    assert "OK   check: extra check" in result


def test_eval_summarizes_repeat_means_and_keeps_unknown_fields() -> None:
    result = render(
        "eval",
        {
            "skills": "active",
            "mean": 0.75,
            "repeats": [
                {"rep": 1, "mean": 0.7, "results": [{"task_id": "x"}]},
                {"rep": 2, "mean": 0.8, "results": []},
            ],
            "model": "test-model",
        },
    )
    assert "Evaluation (active skills)" in result
    assert "Mean: 0.75" in result
    assert "Repeat 1: mean=0.7, tasks=1" in result
    assert "Repeat 2: mean=0.8, tasks=0" in result
    assert "model : test-model" in result


@pytest.mark.parametrize(
    ("kind", "data", "heading"),
    [
        ("status", {"domain": "demo", "r_best": 1.0}, "Workspace status"),
        ("evolve", {"iter": 1, "accepted": True}, "Evolution"),
        ("gate", {"r_val": 0.8, "r_best": 0.7}, "Gate result"),
        ("run-task", {"task_id": "a", "score": 1.0}, "Task run"),
        ("promote", {"profile": "hws-test-a", "skills": ["x"]}, "Skill promotion"),
        ("demote", {"profile": "hws-test-a", "restored": True}, "Skill demotion"),
    ],
)
def test_generic_kinds_render_aligned_key_values(kind: str, data: dict, heading: str) -> None:
    result = render(kind, data)
    assert result.startswith(heading)
    assert all(str(key) in result for key in data)
    assert "\x1b" not in result


def test_setup_profile_and_unknown_kind_have_generic_fallbacks() -> None:
    setup = render("setup-profile", {"changed": True, "profile": "wikiskill", "extra": 2})
    unknown = render("future-verb", {"zeta": 1, "alpha": "x"})
    assert "Profile setup: changed" in setup
    assert "profile : wikiskill" in setup
    assert unknown.splitlines() == ["alpha : x", "zeta  : 1"]


def test_partial_known_shape_falls_back_without_losing_fields() -> None:
    result = render("doctor", {"ok": False, "unexpected": {"nested": True}})
    assert "ok         : false" in result
    assert 'unexpected : {"nested":true}' in result

    partial_version = render("version", {"tool": "hermes-wikiskill"})
    assert partial_version == "tool : hermes-wikiskill"
