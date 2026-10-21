"""Trace config, scores, and the Langfuse prompt, checked offline with fakes."""
import sys
import types

import prompts
import telemetry
from score import root_cause_correct, score_run


class FakeHandler:
    last_trace_id = None


def fake_tracing(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    module = types.ModuleType("langfuse.langchain")
    module.CallbackHandler = FakeHandler
    monkeypatch.setitem(sys.modules, "langfuse.langchain", module)
    monkeypatch.setattr(telemetry, "langfuse_client", lambda: None)


def test_tracing_off_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    assert telemetry.trace_config("obs-001", "start", "INC-2041") == ({}, None)


def test_trace_config_groups_by_thread_and_charges_the_owning_team(monkeypatch):
    fake_tracing(monkeypatch)
    extra, handler = telemetry.trace_config("obs-005", "start", "INC-2041", ["deploys"])
    assert extra["callbacks"] == [handler]
    assert extra["run_name"] == "triage start"
    assert extra["metadata"] == {
        "langfuse_session_id": "obs-005",
        "langfuse_user_id": "payments",  # checkout-api is owned by payments
        "langfuse_tags": ["start", "INC-2041", "skip:deploys"],
    }


def test_root_cause_check():
    assert root_cause_correct("INC-2041", {"likely_cause": "Deploy d-1042 cut the pool from 50 to 5."})
    assert not root_cause_correct("INC-2041", {"likely_cause": "orders-db was slow."})
    assert root_cause_correct("INC-2042", {"likely_cause": "d-1039 lowered the cache TTL."})
    assert not root_cause_correct("INC-9999", {"likely_cause": "d-1042"})


def test_score_run_writes_a_boolean_score():
    class FakeLangfuse:
        def create_score(self, **kwargs):
            self.kwargs = kwargs

    langfuse = FakeLangfuse()
    assert score_run(langfuse, "a" * 32, "INC-2041", {"likely_cause": "d-1042"})
    assert langfuse.kwargs["name"] == "root_cause_correct"
    assert langfuse.kwargs["value"] == 1 and langfuse.kwargs["data_type"] == "BOOLEAN"
    assert langfuse.kwargs["trace_id"] == "a" * 32


def test_analyst_prompt_is_local_when_tracing_is_off(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    prompt, messages = prompts.analyst_messages("INC-2041: checkout errors", '[{"a": 1}]')
    assert prompt is None
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == 'INC-2041: checkout errors\n\nFindings:\n[{"a": 1}]'


def test_analyst_prompt_comes_from_langfuse(monkeypatch):
    class FakePrompt:
        is_fallback = False
        version = 2

        def compile(self, **variables):
            return [{"role": "user", "content": variables["incident"]}]

    class FakeLangfuse:
        def get_prompt(self, name, **kwargs):
            self.asked = (name, kwargs["type"])
            return FakePrompt()

    langfuse = FakeLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setattr(telemetry, "langfuse_client", lambda: langfuse)
    prompt, messages = prompts.analyst_messages("incident", "[]")
    assert langfuse.asked == ("triage-analyst", "chat")
    assert prompt.version == 2 and messages == [{"role": "user", "content": "incident"}]
