"""Offline fakes: no Gemini calls. Each test runs in its own temp directory."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def invoke(self, messages, config=None):
        self.calls += 1
        return self.reply(messages) if callable(self.reply) else self.reply


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "test.db"))
    state = {
        "route": {"severity": "SEV2", "specialists": ["logs", "deploys", "metrics"], "reason": "checkout down"},
        "analysis": {
            "severity": "SEV2",
            "summary": "Pool exhausted after d-1042.",
            "likely_cause": "d-1042 cut maximumPoolSize from 50 to 5.",
            "rationale": "Errors began 10 minutes after the pool change.",
            "actions": [
                {"service": "checkout-api", "action": "Roll back d-1042 on checkout-api."},
                {"service": "orders-db", "action": "Add an alert when checkout-api holds fewer than 10 connections."},
            ],
        },
        "specialist_calls": {"logs": 0, "deploys": 0, "metrics": 0},
        "fail": set(),
    }
    router = FakeModel(lambda m: state["route"])
    analyst = FakeModel(lambda m: state["analysis"])

    def fake_run_specialist(name, brief):
        state["specialist_calls"][name] += 1
        if name in state["fail"]:
            raise RuntimeError("Metrics backend timed out.")
        return {"summary": f"{name} ok", "evidence": [f"{name} line"], "suspected_cause": "d-1042", "severity": "SEV3"}

    monkeypatch.setattr(app, "build_router", lambda: router)
    monkeypatch.setattr(app, "build_analyst", lambda: analyst)
    monkeypatch.setattr(app, "run_specialist", fake_run_specialist)
    state["router"], state["analyst"] = router, analyst
    return state
