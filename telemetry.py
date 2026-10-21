"""Langfuse tracing for the triage workflow.

Tracing turns on when LANGFUSE_PUBLIC_KEY is set. Without it, every helper is
a no-op, so the offline tests run without network access.
"""
import os

from specialists import load_json

_langfuse = None


def tracing_enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))


def langfuse_client():
    global _langfuse
    if _langfuse is None:
        from langfuse import get_client

        _langfuse = get_client()
    return _langfuse


def owning_team(alert_id: str) -> str:
    """The team that owns the alerting service. Its incidents' cost is charged to it."""
    alert = {a["id"]: a for a in load_json("alerts.json")}[alert_id]
    return load_json("services.json")[alert["service"]]["team"]


def trace_config(
    thread_id: str, command: str, alert_id: str, skip: list[str] = ()
) -> tuple[dict, object | None]:
    """Return extra graph config and the Langfuse handler (None when tracing is off).

    One session per thread groups every start and resume of an incident. The
    user is the owning team, so Langfuse can total cost per team. Tags make
    traces filterable by command, alert, and held-back specialist.
    """
    if not tracing_enabled():
        return {}, None
    from langfuse.langchain import CallbackHandler

    langfuse_client()
    handler = CallbackHandler()
    tags = [command, alert_id] + [f"skip:{name}" for name in skip]
    return {
        "callbacks": [handler],
        "run_name": f"triage {command}",
        "metadata": {
            "langfuse_session_id": thread_id,
            "langfuse_user_id": owning_team(alert_id),
            "langfuse_tags": tags,
        },
    }, handler


def trace_url(trace_id: str) -> str | None:
    return langfuse_client().get_trace_url(trace_id=trace_id)


def shutdown():
    """Flush traces and scores before the CLI exits."""
    if _langfuse is not None:
        _langfuse.flush()
