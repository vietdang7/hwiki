"""qa adversarial (D5/D19): every HERMES_HOME a rollout actually runs in (workspace profile or isolated rollout home) holds
only the provider key, 0600, a key-less config and no auth.json; after a full evolve + eval no decoy secret of the source
profile exists anywhere under the workspace, HWS_HOME or the rollout root."""
import json
import os

from conftest import fw_ws, make_task, mini_tasks
from hermes_wikiskill import evaluate, harness
from test_qa_support import chat_homes, qa_hermes

DECOYS = ("fake-bot", "fake-groq", "fake-inline", "fake-nous")     # never needed by the deepseek route
PROVIDER = "fake-key-1"                                              # allowed only inside 0600 .env files


def test_qa_every_rollout_home_is_minimal(tmp_path, monkeypatch, answers):
    ts = mini_tasks()
    te = [make_task("te-0", "test")]
    answers(ts + te)
    log = qa_hermes(tmp_path, monkeypatch)
    ws = fw_ws(tmp_path, ts)
    (tmp_path / "test.json").write_text(json.dumps(te))
    harness.evolve(ws, iters=1, verbose=False)
    evaluate.evaluate(ws, str(tmp_path / "test.json"), skills="empty", repeat=1, tag="qa-cr")
    homes = chat_homes(log)
    assert len(homes) >= 5, "expected baseline/train/maintain/propose/eval rollouts, saw %d" % len(homes)
    for h in homes:
        f = h["files"]
        assert ".env" in f and f[".env"]["mode"] == "600", h
        assert set(f[".env"]["env_names"]) <= {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}, h
        assert "auth.json" not in f, h
        assert "config.yaml" not in f or f["config.yaml"]["api_key_lines"] == 0, h


def test_qa_no_decoy_secret_anywhere_after_runs(tmp_path, answers):
    ts = mini_tasks()
    te = [make_task("te-0", "test")]
    answers(ts + te)
    ws = fw_ws(tmp_path, ts)
    (tmp_path / "test.json").write_text(json.dumps(te))
    harness.evolve(ws, iters=1, verbose=False)
    evaluate.evaluate(ws, str(tmp_path / "test.json"), skills="empty", repeat=1, tag="qa-cr")
    src = str(tmp_path / "source-home")
    hits = []
    for dirpath, dirnames, files in os.walk(str(tmp_path)):
        if dirpath.startswith(src):
            continue
        dirnames[:] = [d for d in dirnames if d != "qa-hermes-bin"]
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.islink(p) or not os.path.isfile(p) or f == "fake-hermes.log":
                continue
            data = open(p, "rb").read()
            for d in DECOYS:
                if d.encode() in data:
                    hits.append("%s: %s" % (os.path.relpath(p, str(tmp_path)), d))
            if PROVIDER.encode() in data and not (f == ".env" and oct(os.stat(p).st_mode & 0o777) == "0o600"):
                hits.append("%s: provider key outside a 0600 .env" % os.path.relpath(p, str(tmp_path)))
    assert not hits, hits
