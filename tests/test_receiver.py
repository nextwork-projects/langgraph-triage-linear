"""The webhook receiver, driven with signed fake Linear payloads."""
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import app
import receiver
from test_linear import CONFIG, start

SECRET = "test-secret"


TRIGGER = "label-None-agent-triage"


@pytest.fixture
def idle_client(monkeypatch, fakes, fake_linear):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    receiver._seen_deliveries.clear()
    return TestClient(receiver.api)


@pytest.fixture
def client(idle_client):
    with app.open_checkpointer() as cp:
        start(app.build_graph(cp))  # pauses on APR-1 (payments) and APR-2 (platform-data)
    return idle_client


def post(client, payload, delivery=None, age_ms=0, secret=SECRET):
    payload = payload | {"webhookTimestamp": int(time.time() * 1000) - age_ms}
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"Linear-Signature": signature, "Content-Type": "application/json"}
    if delivery:
        headers["Linear-Delivery"] = delivery
    return client.post("/linear/webhook", content=body, headers=headers)


def send(client, sub_issue, new_state, old_state="Needs Approval", **kwargs):
    """A sub-issue changing state in Approvals."""
    return post(client, {
        "type": "Issue",
        "action": "update",
        "data": {"id": sub_issue, "teamId": "team-APR", "parentId": "uuid-5", "stateId": f"APR:{new_state}"},
        "updatedFrom": {"stateId": f"APR:{old_state}"},
    }, **kwargs)


def incident(client, action="create", labels=(TRIGGER,), old_labels=None, team="team-TRI"):
    """TRI-5 created, or its labels changed, in Incidents."""
    payload = {
        "type": "Issue",
        "action": action,
        "data": {"id": "uuid-5", "teamId": team, "labelIds": list(labels), "stateId": "TRI:Triage"},
    }
    if old_labels is not None:
        payload["updatedFrom"] = {"labelIds": list(old_labels)}
    return post(client, payload)


def pending_teams():
    with app.open_checkpointer() as cp:
        interrupts = app.build_graph(cp).get_state(CONFIG).interrupts
    return sorted(p.value["team"] for p in interrupts)


def test_bad_signature_is_refused(client):
    assert send(client, "sub-1", "Approved", secret="wrong").status_code == 401
    assert pending_teams() == ["payments", "platform-data"]


def test_stale_timestamp_is_refused(client):
    assert send(client, "sub-1", "Approved", age_ms=120_000).status_code == 401
    assert pending_teams() == ["payments", "platform-data"]


def test_one_approval_leaves_the_other_gate_pending(client):
    assert send(client, "sub-1", "Approved").json() == {"ok": True}
    assert pending_teams() == ["platform-data"]


def test_both_decisions_finish_the_run(client, fake_linear):
    send(client, "sub-1", "Approved")
    send(client, "sub-2", "Rejected")
    assert pending_teams() == []
    assert fake_linear.of("move")[-1][1:] == ("uuid-5", "TRI", "Done")


def test_duplicate_delivery_resumes_once(client, capsys):
    send(client, "sub-1", "Approved", delivery="d-1")
    assert send(client, "sub-1", "Approved", delivery="d-1").json()["duplicate"] is True
    assert pending_teams() == ["platform-data"]


def test_redecided_sub_issue_is_ignored(client, capsys):
    send(client, "sub-1", "Approved", delivery="d-1")
    send(client, "sub-1", "Approved", delivery="d-2")  # a new delivery, same decision
    assert "no gate waiting on sub-issue sub-1" in capsys.readouterr().out
    assert pending_teams() == ["platform-data"]


def test_an_approvals_issue_with_no_parent_is_ignored(client):
    post(client, {
        "type": "Issue", "action": "update",
        "data": {"id": "sub-9", "teamId": "team-APR", "stateId": "APR:Approved"},
        "updatedFrom": {"stateId": "APR:Needs Approval"},
    })
    assert pending_teams() == ["payments", "platform-data"]


def test_other_state_changes_do_nothing(client):
    send(client, "sub-1", "Approved", old_state="Rejected")
    send(client, "sub-1", "In Progress")
    assert pending_teams() == ["payments", "platform-data"]


def test_labelled_new_incident_starts_a_run(idle_client):
    assert incident(idle_client).json() == {"ok": True}
    assert pending_teams() == ["payments", "platform-data"]


def test_adding_the_label_later_starts_a_run(idle_client):
    incident(idle_client, action="update", labels=[TRIGGER, "label-x"], old_labels=["label-x"])
    assert pending_teams() == ["payments", "platform-data"]


def test_unlabelled_or_other_team_issues_start_nothing(idle_client):
    incident(idle_client, labels=[])
    incident(idle_client, team="team-APR")
    assert pending_teams() == []


def test_the_runs_own_label_changes_start_nothing(client, fake_linear):
    # report_findings adds a severity label; agent-triage was already there.
    incident(client, action="update", labels=[TRIGGER, "sev1"], old_labels=[TRIGGER])
    assert len(fake_linear.of("comment")) == 1


def test_an_issue_with_a_run_is_not_started_twice(client, fake_linear, capsys):
    # The label removed and added back: a valid trigger, but the thread exists.
    incident(client, action="update", labels=[TRIGGER], old_labels=[])
    assert "TRI-5: already has a run" in capsys.readouterr().out
    assert len(fake_linear.of("comment")) == 1


def test_the_answer_needs_no_linear_call(idle_client, monkeypatch):
    """A cold start leaves little of Linear's 5 seconds, so the 200 must not wait on Linear."""
    answered = []
    monkeypatch.setattr(receiver, "handle_delivery", lambda payload: answered.append(payload["action"]))
    monkeypatch.setattr(receiver.linear_api, "_client", "no Linear client before the 200")
    assert incident(idle_client).json() == {"ok": True}
    assert answered == ["create"]


def test_adding_the_label_again_retries_a_stopped_run(idle_client, fakes, capsys):
    fakes["fail"] = {"metrics"}
    incident(idle_client)  # the metrics specialist fails, so the run stops part-way
    assert pending_teams() == []
    fakes["fail"] = set()
    incident(idle_client, action="update", labels=[TRIGGER], old_labels=[])
    assert "retrying from its last checkpoint" in capsys.readouterr().out
    assert pending_teams() == ["payments", "platform-data"]
    assert fakes["specialist_calls"]["logs"] == 1  # saved findings weren't re-run
