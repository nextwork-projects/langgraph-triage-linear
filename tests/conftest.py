"""Offline fakes: no Gemini calls. Each test runs in its own temp directory."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import linear as linear_api  # noqa: E402


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


class FakeLinear:
    """Records every Linear write instead of calling the API."""

    def __init__(self):
        self.calls = []
        self.children = {}
        alert = app.load_json("alerts.json")[0]
        self.issues = {"uuid-5": {"id": "uuid-5", "identifier": "TRI-5", "title": alert["title"],
                                  "description": f"Alert: {alert['id']}\nService: {alert['service']}"}}

    def team(self, key):
        return {"id": f"team-{key}", "key": key}

    def state_id(self, team_key, name):
        return f"{team_key}:{name}"

    def label_id(self, name, group=None):
        return f"label-{group}-{name}"

    def issue(self, ref):
        return self.issues[ref]

    def sub_issues(self, parent_id):
        return self.children.get(parent_id, [])

    def create_issue(self, **fields):
        number = len(self.of("create")) + 1
        child = {"id": f"sub-{number}", "identifier": f"APR-{number}", "description": fields.get("description")}
        self.calls.append(("create", fields))
        self.children.setdefault(fields.get("parentId"), []).append(child)
        self.issues[child["id"]] = child | {"parent": fields.get("parentId")}
        return child

    def move(self, issue_id, team_key, state):
        self.calls.append(("move", issue_id, team_key, state))

    def comment(self, issue_id, body):
        self.calls.append(("comment", issue_id, body))
        return f"comment-{len(self.calls)}"

    def set_group_label(self, issue_id, group, name):
        self.calls.append(("label", issue_id, group, name))

    def attach(self, issue_id, url, title, subtitle=""):
        self.calls.append(("attach", issue_id, url, title))

    def of(self, kind):
        return [call for call in self.calls if call[0] == kind]


@pytest.fixture
def fake_linear(monkeypatch):
    # recall_history reads Linear through MCP whenever LINEAR_API_KEY is set, so keep it offline.
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    linear = FakeLinear()
    monkeypatch.setattr(linear_api, "_client", linear)
    return linear
