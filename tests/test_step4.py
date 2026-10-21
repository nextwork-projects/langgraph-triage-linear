"""One approval gate per owning team, resumed one team at a time from separate processes."""
import os
import subprocess
import sys
from pathlib import Path

from test_step3 import RUNNER, ROOT

TWO_TEAMS = RUNNER.replace(
    '"actions": [{"service": "checkout-api", "action": "Roll back d-1042."}]',
    '"actions": [{"service": "checkout-api", "action": "Roll back d-1042."}, '
    '{"service": "orders-db", "action": "Add a low-connection alert."}]',
)


def run(tmp_path, *args):
    script = tmp_path / "runner.py"
    script.write_text(TWO_TEAMS)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "tests"))
    env.pop("TRIAGE_FAIL", None)
    return subprocess.run(
        [sys.executable, str(script), str(ROOT), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    ).stdout


def test_each_team_approves_separately(tmp_path):
    out = run(tmp_path, "start", "--thread", "inc-teams", "--alert", "INC-2041")
    assert "WORKFLOW PAUSED: 2 team approvals pending" in out
    assert "- payments (checkout-api)" in out and "- platform-data (orders-db)" in out

    out = run(tmp_path, "resume", "--thread", "inc-teams", "--team", "payments", "--decision", "approve")
    assert "Recorded approve for payments." in out
    assert "WORKFLOW PAUSED: 1 team approvals pending" in out
    assert "platform-data" in out
    assert not (tmp_path / "runbooks").exists()

    out = run(tmp_path, "resume", "--thread", "inc-teams", "--team", "payments", "--decision", "approve")
    assert "No pending approval for team payments. Waiting on: platform-data" in out

    out = run(tmp_path, "resume", "--thread", "inc-teams", "--team", "platform-data", "--decision", "reject")
    assert "Status: published" in out
    runbook = (tmp_path / "runbooks" / "inc-teams.md").read_text()
    assert "payments / checkout-api: Roll back d-1042." in runbook
    assert "Add a low-connection alert." not in runbook.split("Audit log:")[0]
    assert "Team platform-data decision for orders-db: reject." in runbook


def test_all_rejected_publishes_nothing(tmp_path):
    run(tmp_path, "start", "--thread", "inc-no", "--alert", "INC-2041")
    run(tmp_path, "resume", "--thread", "inc-no", "--team", "payments", "--decision", "reject")
    out = run(tmp_path, "resume", "--thread", "inc-no", "--team", "platform-data", "--decision", "reject")
    assert "Status: rejected_without_publish" in out
    assert not (tmp_path / "runbooks").exists()
