import operator
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

import app


def start(graph, thread, alert_id):
    alerts = {a["id"]: a for a in app.load_json("alerts.json")}
    alert = alerts[alert_id]
    return graph.invoke(
        {"incident_id": thread, "incident": alert["title"], "alert": alert, "status": "new", "audit_log": []},
        {"configurable": {"thread_id": thread}},
        durability="sync",
    )


def test_highest_severity_wins():
    assert app.highest_severity("", "SEV3") == "SEV3"
    assert app.highest_severity("SEV3", "SEV1") == "SEV1"
    assert app.highest_severity("SEV2", "SEV4") == "SEV2"


def test_checkout_runs_three_specialists_then_pauses(fakes):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        out = start(graph, "inc-1", "INC-2041")
        snap = graph.get_state({"configurable": {"thread_id": "inc-1"}})
    assert sorted(f["specialist"] for f in snap.values["findings"]) == ["deploys", "logs", "metrics"]
    assert snap.values["severity"] == "SEV2"  # router SEV2 beats specialists' SEV3
    assert snap.next == ("approval_gate", "approval_gate")
    assert fakes["analyst"].calls == 1  # deferred synthesis runs once, not once per specialist
    assert "__interrupt__" in out


def test_low_severity_runs_one_specialist_and_publishes(fakes):
    fakes["route"] = {"severity": "SEV4", "specialists": ["metrics"], "reason": "latency only"}
    fakes["analysis"] = dict(fakes["analysis"], severity="SEV4", actions=[{"service": "search-api", "action": "Raise the search cache TTL back to 600s."}])
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        out = start(app.build_graph(cp), "inc-2", "INC-2042")
    assert [f["specialist"] for f in out["findings"]] == ["metrics"]
    assert out["status"] == "published"


def test_missing_reducer_raises_concurrent_update():
    class Bad(TypedDict, total=False):
        findings: list[str]

    b = StateGraph(Bad)
    b.add_node("inv", lambda t: {"findings": [t["k"]]})
    b.add_conditional_edges(START, lambda s: [Send("inv", {"k": k}) for k in "ab"], ["inv"])
    b.add_edge("inv", END)
    with pytest.raises(InvalidUpdateError, match="Can receive only one value per step"):
        b.compile().invoke({"findings": []})
