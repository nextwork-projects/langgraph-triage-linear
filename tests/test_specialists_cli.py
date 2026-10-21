"""`python3 specialists.py <specialist> <alert>` runs one specialist on its own."""
import json
import sys

import specialists


class FakeAgent:
    def __init__(self):
        self.brief = None
        self.config = None

    def invoke(self, payload, config=None):
        self.brief = payload["messages"][0][1]
        self.config = config
        tool_call = type("Message", (), {"type": "tool"})()
        return {"messages": [tool_call, tool_call], "structured_response": {"summary": "Pool exhausted.", "evidence": ["HikariPool-1 timeout"], "suspected_cause": "d-1042", "severity": "SEV2"}}


def test_runs_one_specialist_on_one_alert(monkeypatch, capsys):
    agent = FakeAgent()
    monkeypatch.setattr(specialists, "build_specialist", lambda name: agent)
    monkeypatch.setattr(sys, "argv", ["specialists.py", "logs", "INC-2041"])
    specialists.main()
    out = capsys.readouterr().out
    assert out.startswith("logs specialist investigating INC-2041...")
    assert json.loads(out.split("\n", 1)[1])["suspected_cause"] == "d-1042"
    assert "Services to check: checkout-api, orders-db, payments-gateway." in agent.brief
    assert agent.config == {"recursion_limit": 25}
