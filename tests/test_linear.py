"""The workflow's Linear writes, checked offline with a fake client."""
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import app
from linear import LinearError, alert_fields
from seed_incidents import description

CONFIG = {"configurable": {"thread_id": "TRI-5"}}


def start(graph):
    alert = {a["id"]: a for a in app.load_json("alerts.json")}["INC-2041"]
    return graph.invoke(
        {"incident_id": "TRI-5", "issue_id": "uuid-5", "incident": alert["title"],
         "alert": alert, "status": "new", "audit_log": []},
        CONFIG, durability="sync",
    )


def resume_all(graph, decision):
    pending = graph.get_state(CONFIG).interrupts
    return graph.invoke(Command(resume={p.id: decision for p in pending}), CONFIG, durability="sync")


def test_seeded_description_round_trips():
    alert = app.load_json("alerts.json")[0]
    assert alert_fields(description(alert)) == {
        "alert": "INC-2041", "service": "checkout-api", "fired": "2026-10-09T14:15:00Z"
    }


def test_alert_line_survives_bold_markdown():
    assert alert_fields("**Alert:** INC-2042\n\nsearch is slow")["alert"] == "INC-2042"


def test_issue_without_alert_line_is_refused():
    with pytest.raises(LinearError, match="no 'Alert: INC-…' line"):
        alert_fields("checkout is down")


def test_run_moves_the_incident_and_posts_findings(fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        start(graph)
        assert [c[3] for c in fake_linear.of("move")] == ["Investigating", "Awaiting Approval"]
        [comment] = fake_linear.of("comment")
        assert "**Triage findings: SEV2**" in comment[2]
        assert "payments / checkout-api: Roll back d-1042 on checkout-api. (needs team approval in APR-1)" in comment[2]
        assert fake_linear.of("label") == [("label", "uuid-5", "severity", "SEV2")]

        resume_all(graph, "approve")
    assert fake_linear.of("move")[-1][3] == "Done"
    assert "**Runbook published**" in fake_linear.of("comment")[-1][2]


def test_all_rejected_hands_the_incident_back(fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        start(graph)
        out = resume_all(graph, "reject")
    assert out["status"] == "rejected_without_publish"
    assert fake_linear.of("move")[-1][3] == "Triage"
    assert "Every proposed action was rejected" in fake_linear.of("comment")[-1][2]


def test_low_severity_skips_awaiting_approval(fakes, fake_linear):
    fakes["route"] = {"severity": "SEV4", "specialists": ["metrics"], "reason": "latency"}
    fakes["analysis"] = dict(fakes["analysis"], severity="SEV4",
                             actions=[{"service": "search-api", "action": "Raise the cache TTL."}])
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        out = start(app.build_graph(cp))
    assert out["status"] == "published"
    assert [c[3] for c in fake_linear.of("move")] == ["Investigating", "Done"]


def test_fixture_runs_never_touch_linear(fakes, monkeypatch):
    def no_linear():
        raise AssertionError("a fixture run called Linear")
    monkeypatch.setattr(app, "linear_client", no_linear)
    alert = app.load_json("alerts.json")[0]
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        app.build_graph(cp).invoke(
            {"incident_id": "inc-1", "incident": "x", "alert": alert, "status": "new", "audit_log": []},
            {"configurable": {"thread_id": "inc-1"}}, durability="sync",
        )


def test_comment_is_the_last_write_so_a_retry_posts_it_once(fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        start(app.build_graph(cp))
    writes = [c[0] for c in fake_linear.calls if c[0] in ("move", "label", "comment")]
    assert writes[-3:] == ["label", "move", "comment"]


def test_one_sub_issue_per_gated_action_and_the_gate_knows_it(fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        start(graph)
        pending = graph.get_state(CONFIG).interrupts
    created = [c[1] for c in fake_linear.of("create")]
    assert [f["labelIds"] for f in created] == [["label-team-payments"], ["label-team-platform-data"]]
    assert all(f["parentId"] == "uuid-5" and f["stateId"] == "APR:Needs Approval" for f in created)
    assert sorted(p.value["approval_issue"] for p in pending) == ["APR-1", "APR-2"]
    assert "needs team approval in APR-1" in fake_linear.of("comment")[0][2]


def test_retried_run_reuses_existing_sub_issues(fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        start(app.build_graph(cp))
    fakes["analysis"] = dict(fakes["analysis"])  # same actions, so same approval keys
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        graph.invoke(
            {"incident_id": "TRI-5", "issue_id": "uuid-5", "incident": "x",
             "alert": app.load_json("alerts.json")[0], "status": "new", "audit_log": []},
            {"configurable": {"thread_id": "TRI-5-retry"}}, durability="sync",
        )
    assert len(fake_linear.of("create")) == 2


def test_cli_decision_moves_the_sub_issue(fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        start(graph)
        resume_all(graph, "approve")
    moved = [c for c in fake_linear.of("move") if c[2] == "APR"]
    assert sorted((c[1], c[3]) for c in moved) == [("sub-1", "Approved"), ("sub-2", "Approved")]


def test_long_titles_cut_at_a_word():
    title = app.short_title("Approve for payments: " + "restore the pool size " * 10)
    assert len(title) <= 120 and title.endswith("…")
    assert title[:-1].split()[-1] in {"restore", "the", "pool", "size"}  # no word cut in half
