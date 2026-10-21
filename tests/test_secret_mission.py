"""The status board reads a paused thread without resuming it."""
import os
import subprocess
import sys

from test_step3 import ROOT
from test_step4 import run


def status(tmp_path, thread):
    env = dict(os.environ)
    return subprocess.run(
        [sys.executable, str(ROOT / "status.py"), "--thread", thread],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    ).stdout


def test_status_board_mid_approval(tmp_path):
    run(tmp_path, "start", "--thread", "inc-board", "--alert", "INC-2041")
    run(tmp_path, "resume", "--thread", "inc-board", "--team", "payments", "--decision", "approve")
    db_before = (tmp_path / "triage_checkpoints.db").read_bytes()

    out = status(tmp_path, "inc-board")
    assert "Severity: SEV2" in out
    assert "payments (checkout-api): approve" in out  # values already include the resolved gate
    assert "platform-data (orders-db): Add a low-connection alert. [interrupt" in out
    assert "Next: approval_gate" in out
    assert "Runbook: not published" in out
    assert (tmp_path / "triage_checkpoints.db").read_bytes() == db_before
