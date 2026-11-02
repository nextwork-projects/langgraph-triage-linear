"""Secret Mission: the read-only MCP agent, against an in-process FastMCP server and a scripted model."""
import json

from fastmcp import FastMCP
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

import app
import linear_mcp
from test_linear import start

server = FastMCP("fake-linear-readonly")
calls = []


@server.tool
def list_issues(query: str, team: str | None = None) -> str:
    """List issues that match a search."""
    calls.append(query)
    return json.dumps([{"identifier": "TRI-2", "title": "checkout-api 5xx after a pool change", "state": "Done"}])


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def scripted():
    return ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "list_issues", "args": {"query": "checkout-api"}, "id": "c1"}]),
        AIMessage(content="- TRI-2: checkout-api 5xx after a pool change (Done)"),
    ])


def test_the_agent_reads_past_incidents_through_mcp():
    calls.clear()
    answer = linear_mcp.incident_history(scripted(), "checkout-api", "TRI-5", target=server)
    assert calls == ["checkout-api"]
    assert answer == "- TRI-2: checkout-api 5xx after a pool change (Done)"


def test_the_history_reaches_the_router(fakes, fake_linear, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_read")
    monkeypatch.setattr(app, "build_model", scripted)
    monkeypatch.setattr(linear_mcp, "adapter", lambda target=None, _real=linear_mcp.adapter: _real(server))
    briefs = []
    fakes["router"].reply = lambda messages: briefs.append(messages[1][1]) or fakes["route"]
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        start(app.build_graph(cp))
    assert "Earlier incidents in Linear:\n- TRI-2" in briefs[0]


def test_without_the_linear_key_nothing_changes(fakes, fake_linear, monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        out = start(app.build_graph(cp))
    assert "history" not in out


def test_the_mcp_agent_works_inside_the_streamed_run(fakes, fake_linear, monkeypatch):
    """run_graph streams with version="v3" and durability="sync", as the receiver does."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_read")
    monkeypatch.setattr(app, "build_model", scripted)
    monkeypatch.setattr(linear_mcp, "adapter", lambda target=None, _real=linear_mcp.adapter: _real(server))
    alert = app.load_json("alerts.json")[0]
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        values = app.run_graph(app.build_graph(cp), "TRI-5", "start", {
            "incident_id": "TRI-5", "issue_id": "uuid-5", "incident": "x",
            "alert": alert, "status": "new", "audit_log": [],
        })
    assert values["history"].startswith("- TRI-2")
    assert values["status"] == "awaiting_approval"
