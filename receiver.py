"""Linear webhook receiver: Linear starts each triage run and resumes its paused gates.

- An Incidents issue labelled agent-triage (when it is created, or when the label
  is added later) starts a run, unless that issue already has one.
- Moving an approval sub-issue from Needs Approval to Approved or Rejected
  resumes the gate waiting on it.

Linear gives a delivery 5 seconds, so the receiver checks the signature, answers
200 at once, and does the rest in the background, including every Linear lookup.
The graph, model, and tracing libraries load only when a run starts, which keeps
a cold start near 5 seconds. A delivery that misses the limit still gets handled;
Linear just retries it a minute later, and the retry finds the work already done.

    uvicorn receiver:api --port 8080
"""
import hashlib
import hmac
import json
import os
import threading
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import linear as linear_api
from linear import APPROVALS_TEAM, INCIDENTS_TEAM, TRIGGER_LABEL

MAX_AGE_MS = 60_000
DECISIONS = {"Approved": "approve", "Rejected": "reject"}

api = FastAPI()
_thread_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()  # so two deliveries can't each make a lock for the same thread
_seen_deliveries: OrderedDict[str, None] = OrderedDict()


def verify_signature(raw_body: bytes, signature: str | None) -> None:
    """Linear signs the raw body with HMAC-SHA256, keyed with the webhook's secret."""
    secret = os.environ["LINEAR_WEBHOOK_SECRET"].encode()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Bad signature.")


def first_delivery(delivery_id: str | None) -> bool:
    """Linear retries a delivery it thinks failed. Act on each delivery once."""
    if not delivery_id:
        return True
    if delivery_id in _seen_deliveries:
        return False
    _seen_deliveries[delivery_id] = None
    while len(_seen_deliveries) > 1000:
        _seen_deliveries.popitem(last=False)
    return True


@contextmanager
def thread_lock(thread: str):
    """Work on one thread at a time, so two teams deciding seconds apart don't race.

    The in-process lock covers one receiver. On Cloud Run a second instance can
    start briefly even with --max 1, so a Postgres advisory lock covers them all.
    It is released when its connection closes.
    """
    with _locks_guard:
        lock = _thread_locks[thread]
    with lock:
        url = os.environ.get("DATABASE_URL")
        if not url:
            yield
            return
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (thread,))
            yield


def new_incident(payload: dict) -> str | None:
    """The issue id when agent-triage lands on an Incidents issue, at creation or added later.

    The workflow's own writes never match: its sub-issues are in Approvals, and
    its label and state changes leave agent-triage where it was.
    """
    data = payload.get("data") or {}
    if payload.get("type") != "Issue":
        return None
    linear = linear_api.client()
    if data.get("teamId") != linear.team(INCIDENTS_TEAM)["id"]:
        return None
    trigger = linear.label_id(TRIGGER_LABEL)
    if trigger not in (data.get("labelIds") or []):
        return None
    if payload.get("action") == "create":
        return data["id"]
    old = payload.get("updatedFrom") or {}
    if payload.get("action") == "update" and "labelIds" in old and trigger not in (old["labelIds"] or []):
        return data["id"]
    return None


def approval_decision(payload: dict) -> tuple[str, str, str] | None:
    """(sub-issue id, parent id, decision) when a sub-issue moves from Needs Approval to Approved or Rejected."""
    data = payload.get("data") or {}
    old_state = (payload.get("updatedFrom") or {}).get("stateId")
    if payload.get("type") != "Issue" or payload.get("action") != "update" or not old_state:
        return None
    if not data.get("parentId"):
        return None  # an Approvals issue someone made by hand, not one of the run's sub-issues
    linear = linear_api.client()
    if old_state != linear.state_id(APPROVALS_TEAM, "Needs Approval"):
        return None
    for state, decision in DECISIONS.items():
        if data.get("stateId") == linear.state_id(APPROVALS_TEAM, state):
            return data["id"], data["parentId"], decision
    return None


def start_triage(issue_id: str) -> None:
    import app
    import telemetry

    thread, graph_input = app.issue_input(issue_id)
    # Held for the whole run, so a second trigger waits here and then sees this run's state.
    with thread_lock(thread), app.open_checkpointer() as checkpointer:
        graph = app.build_graph(checkpointer)
        snapshot = graph.get_state({"configurable": {"thread_id": thread}})
        if snapshot.next and not snapshot.interrupts:
            # The run stopped part-way: a model error, or a redeploy shut its instance
            # down. Its checkpoints are in Postgres, so adding the label again retries it.
            print(f"{thread}: run stopped part-way, retrying from its last checkpoint.")
            app.run_graph(graph, thread, "resume", None)
        elif snapshot.values:
            # Paused on approvals or finished. Linear sent the create twice, or the label came back.
            print(f"{thread}: already has a run, not starting another.")
            return
        else:
            print(f"{thread}: {TRIGGER_LABEL} from Linear, starting.")
            app.run_graph(graph, thread, "start", graph_input)
    telemetry.shutdown()


def resume_approval(sub_issue_id: str, parent_id: str, decision: str) -> None:
    from langgraph.types import Command

    import app
    import telemetry

    thread = linear_api.client().issue(parent_id)["identifier"]
    with thread_lock(thread), app.open_checkpointer() as checkpointer:
        graph = app.build_graph(checkpointer)
        config = {"configurable": {"thread_id": thread}}
        gates = [
            pending.id
            for pending in graph.get_state(config).interrupts
            if pending.value.get("approval_issue_id") == sub_issue_id
        ]
        if not gates:
            # Already decided (a late duplicate, or the CLI got there first).
            print(f"{thread}: no gate waiting on sub-issue {sub_issue_id}.")
            return
        print(f"{thread}: {decision} from Linear, resuming.")
        app.run_graph(graph, thread, "webhook", Command(resume={gates[0]: decision}))
    telemetry.shutdown()


def handle_delivery(payload: dict) -> None:
    if issue_id := new_incident(payload):
        start_triage(issue_id)
    elif decision := approval_decision(payload):
        resume_approval(*decision)


@api.post("/linear/webhook")
async def linear_webhook(
    request: Request,
    background: BackgroundTasks,
    linear_signature: str | None = Header(default=None),
    linear_delivery: str | None = Header(default=None),
):
    raw_body = await request.body()
    verify_signature(raw_body, linear_signature)
    payload = json.loads(raw_body)
    # A replayed request has a valid signature, so also refuse anything over a minute old.
    if abs(time.time() * 1000 - payload.get("webhookTimestamp", 0)) > MAX_AGE_MS:
        raise HTTPException(status_code=401, detail="Stale webhook.")
    # One line per delivery in the logs (Cloud Logging on Cloud Run), to check the payload's shape.
    print(json.dumps({"delivery": linear_delivery, "payload": payload}), flush=True)
    if not first_delivery(linear_delivery):
        return {"ok": True, "duplicate": True}

    # Everything that calls Linear happens after the 200. On a cold start the
    # boot alone takes most of Linear's 5 seconds.
    background.add_task(handle_delivery, payload)
    return {"ok": True}


# Not /healthz: Cloud Run reserves paths that end in z and answers them itself.
@api.get("/health")
def health():
    return {"ok": True}
