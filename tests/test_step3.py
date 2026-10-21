"""Crash one specialist, then resume the thread in a separate OS process."""
import os
import subprocess
import sys
from pathlib import Path

import app

ROOT = Path(__file__).resolve().parent.parent

RUNNER = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import app
from conftest import FakeModel
calls_file = "calls.json"
calls = json.load(open(calls_file)) if os.path.exists(calls_file) else {}
def fake_run_specialist(name, brief):
    calls[name] = calls.get(name, 0) + 1
    json.dump(calls, open(calls_file, "w"))
    if os.environ.get("TRIAGE_FAIL") == name:
        raise RuntimeError("Metrics backend timed out.")
    return {"summary": "ok", "evidence": [], "suspected_cause": "d-1042", "severity": "SEV3"}
app.run_specialist = fake_run_specialist
app.build_router = lambda: FakeModel({"severity": "SEV2", "specialists": ["logs", "deploys", "metrics"], "reason": "r"})
app.build_analyst = lambda: FakeModel({"severity": "SEV2", "summary": "s", "likely_cause": "d-1042", "rationale": "r", "actions": [{"service": "checkout-api", "action": "Roll back d-1042."}]})
sys.argv = ["app.py"] + sys.argv[2:]
app.main()
'''


def run(tmp_path, *args, fail=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "tests"))
    env.pop("TRIAGE_FAIL", None)
    if fail:
        env["TRIAGE_FAIL"] = fail
    script = tmp_path / "runner.py"
    script.write_text(RUNNER)
    return subprocess.run(
        [sys.executable, str(script), str(ROOT), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    ).stdout


def test_crash_then_resume_in_new_process(tmp_path):
    (tmp_path / "data").symlink_to(ROOT / "data")
    out = run(tmp_path, "start", "--thread", "inc-crash", "--alert", "INC-2041", fail="metrics")
    assert "WORKFLOW FAILED: Metrics backend timed out." in out
    assert "Waiting to re-run: investigate" in out
    out = run(tmp_path, "resume", "--thread", "inc-crash")
    assert "WORKFLOW PAUSED" in out
    import json
    assert json.loads((tmp_path / "calls.json").read_text()) == {"logs": 1, "deploys": 1, "metrics": 2}


def test_skip_holds_a_specialist_back(tmp_path):
    out = run(tmp_path, "start", "--thread", "inc-skip", "--alert", "INC-2041", "--skip", "deploys")
    assert "Router: SEV2, sending logs, metrics" in out
    import json
    assert json.loads((tmp_path / "calls.json").read_text()) == {"logs": 1, "metrics": 1}
