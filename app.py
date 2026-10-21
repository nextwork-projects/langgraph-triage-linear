import argparse
import hashlib
import json
import os
import operator
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, TypedDict

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from specialists import SPECIALISTS, alert_brief, build_model, load_json, run_specialist
import telemetry
import fixer
import linear_mcp
import linear as linear_api
from linear import APPROVALS_TEAM, INCIDENTS_TEAM, Linear, alert_fields
from prompts import analyst_messages
from score import score_run

DB_PATH = "triage_checkpoints.db"
SEVERITY_ORDER = ["SEV4", "SEV3", "SEV2", "SEV1"]
RUNBOOK_DIR = Path("runbooks")
# Services whose code is in the fix repo (triage-services). An approved action on one gets a fix PR.
FIXABLE_SERVICES = {"checkout-api"}
RESTRICTED_TERMS = (
    "delete",
    "disable",
    "drop",
    "production",
    "restart",
    "rotate",
)

ANALYSIS_SCHEMA = {
    "title": "IncidentPlan",
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["SEV1", "SEV2", "SEV3", "SEV4"],
        },
        "summary": {"type": "string"},
        "likely_cause": {"type": "string"},
        "rationale": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["service", "action"],
            },
        },
    },
    "required": [
        "severity",
        "summary",
        "likely_cause",
        "rationale",
        "actions",
    ],
}

ROUTER_SCHEMA = {
    "title": "TriageDecision",
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["SEV1", "SEV2", "SEV3", "SEV4"],
        },
        "specialists": {
            "type": "array",
            "items": {"type": "string", "enum": list(SPECIALISTS)},
        },
        "reason": {"type": "string"},
    },
    "required": ["severity", "specialists", "reason"],
}


def highest_severity(current: str, new: str) -> str:
    if current not in SEVERITY_ORDER:
        return new
    return max(current, new, key=SEVERITY_ORDER.index)


class IncidentState(TypedDict, total=False):
    incident_id: str
    issue_id: str
    incident: str
    alert: dict
    severity: Annotated[str, highest_severity]
    specialists: list[str]
    findings: Annotated[list[dict], operator.add]
    analysis: dict
    actions: list[dict]
    approvals: Annotated[list[dict], operator.add]
    status: str
    audit_log: list[str]
    runbook_path: str
    fix: dict
    history: str
    skip_specialists: list[str]


class SpecialistTask(TypedDict):
    specialist: str
    brief: str


class ActionTask(TypedDict, total=False):
    incident_id: str
    team: str
    service: str
    action: str
    policy_reason: str
    approval_issue_id: str
    approval_issue: str


def linear_client() -> Linear:
    return linear_api.client()


def build_analyst():
    return build_model().with_structured_output(
        schema=ANALYSIS_SCHEMA,
        method="json_schema",
    )


def build_patcher():
    return build_model().with_structured_output(
        schema=fixer.PATCH_SCHEMA,
        method="json_schema",
    )


def build_router():
    return build_model().with_structured_output(
        schema=ROUTER_SCHEMA,
        method="json_schema",
    )


def safe_incident_id(incident_id: str) -> str:
    safe_id = "".join(
        character
        for character in incident_id
        if character.isalnum() or character in {"-", "_"}
    )
    if not any(character.isalnum() for character in safe_id):
        raise ValueError("Incident ID must contain a letter or number.")
    return safe_id


def incident_brief(state: IncidentState) -> str:
    brief = alert_brief(state["alert"]["id"])
    if state.get("history"):
        brief += f"\n\nEarlier incidents in Linear:\n{state['history']}"
    return brief


def pick_up_incident(state: IncidentState):
    if state.get("issue_id"):
        linear_client().move(state["issue_id"], INCIDENTS_TEAM, "Investigating")
    return {"status": "investigating"}


def recall_history(state: IncidentState):
    """Secret Mission: a read-only agent looks up earlier incidents through Linear's MCP server."""
    if not state.get("issue_id") or not linear_mcp.configured():
        return {}
    history = linear_mcp.incident_history(build_model(), state["alert"]["service"], state["incident_id"])
    print(f"History: {history}")
    return {
        "history": history,
        "audit_log": state["audit_log"] + ["Read earlier incidents from Linear (read-only MCP)."],
    }


def route_incident(state: IncidentState):
    decision = build_router().invoke(
        [
            (
                "system",
                "You are the incident commander. Set a first severity and choose "
                "which specialists investigate: logs, deploys, metrics. For SEV1 "
                "or SEV2, send every specialist. For SEV3 or SEV4, send only the "
                "one or two most likely to explain the symptom.",
            ),
            ("human", incident_brief(state)),
        ]
    )
    # Secret Mission: hold a specialist back to see the score catch a weak run.
    skipped = state.get("skip_specialists", [])
    decision["specialists"] = [
        name for name in decision["specialists"] if name not in skipped
    ]
    print(f"Router: {decision['severity']}, sending {', '.join(decision['specialists'])}")
    return {
        "severity": decision["severity"],
        "specialists": decision["specialists"],
        "status": "routed",
        "audit_log": state["audit_log"]
        + [f"Router chose {decision['specialists']}: {decision['reason']}"],
    }


def dispatch_specialists(state: IncidentState):
    brief = incident_brief(state)
    return [
        Send("investigate", {"specialist": name, "brief": brief})
        for name in state["specialists"]
    ]


def investigate(task: SpecialistTask):
    name = task["specialist"]
    print(f"  {name} specialist investigating...")
    finding = run_specialist(name, task["brief"])
    print(f"  {name} specialist done: {finding['severity']}, {finding['suspected_cause']}")
    return {
        "findings": [{"specialist": name, **finding}],
        "severity": finding["severity"],
    }


def analyze_incident(state: IncidentState):
    analyst = build_analyst()
    findings = json.dumps(state["findings"], indent=2)
    # Secret Mission: the prompt comes from Langfuse, and the generation links to its version.
    prompt, messages = analyst_messages(state["incident"], findings)
    analysis = analyst.invoke(
        messages,
        config={"metadata": {"langfuse_prompt": prompt}} if prompt else None,
    )
    return {
        "analysis": analysis,
        "severity": analysis["severity"],
        "status": "analyzed",
        "audit_log": state["audit_log"]
        + [f"Model combined {len(state['findings'])} findings into one analysis."],
    }


def policy_check(state: IncidentState):
    services = load_json("services.json")
    severity_requires_review = state["severity"] in {"SEV1", "SEV2"}
    actions, approvals = [], []
    for proposed in state["analysis"]["actions"]:
        service = services.get(proposed["service"], {"team": "unowned"})
        searchable_text = f"{state['incident']} {proposed['action']}".lower()
        term_requires_review = any(
            term in searchable_text for term in RESTRICTED_TERMS
        )
        requires_approval = severity_requires_review or term_requires_review
        action = {
            "incident_id": state["incident_id"],
            "team": service["team"],
            "service": proposed["service"],
            "action": proposed["action"],
            "policy_reason": (
                "High severity or restricted operational language detected."
                if requires_approval
                else "No high-risk policy rule matched."
            ),
        }
        actions.append(action | {"requires_approval": requires_approval})
        if not requires_approval:
            approvals.append(action | {"decision": "not required"})
    gated = sum(action["requires_approval"] for action in actions)
    return {
        "actions": actions,
        "approvals": approvals,
        "status": "policy_checked",
        "audit_log": state["audit_log"]
        + [f"Policy check: {gated} of {len(actions)} actions need team approval."],
    }


APPROVAL_LINE = re.compile(r"^Approval: (\w+)$", re.MULTILINE)


def approval_key(action: dict) -> str:
    text = f"{action['team']}|{action['service']}|{action['action']}"
    return hashlib.sha1(text.encode()).hexdigest()[:8]


def short_title(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def approval_description(state: IncidentState, action: dict) -> str:
    return (
        f"Approval: {approval_key(action)}\n"
        f"Incident: {state['incident_id']}\n"
        f"Team: {action['team']}\n"
        f"Service: {action['service']}\n\n"
        f"**Action:** {action['action']}\n\n"
        f"**Why it needs approval:** {action['policy_reason']}\n\n"
        "Move this issue to **Approved** or **Rejected** to decide. "
        "The paused workflow resumes as soon as you do."
    )


def create_approval_subissues(state: IncidentState):
    """One Approvals sub-issue per gated action, created before the gates pause.

    This runs in its own node because a resumed node re-runs from its start: a
    sub-issue created inside approval_gate would be created again on every resume.
    It also looks up existing sub-issues first, so a retried run doesn't duplicate them.
    """
    gated = [action for action in state["actions"] if action["requires_approval"]]
    if not state.get("issue_id") or not gated:
        return {}
    linear = linear_client()
    existing = {}
    for child in linear.sub_issues(state["issue_id"]):
        match = APPROVAL_LINE.search(child["description"] or "")
        if match:
            existing[match.group(1)] = child
    approvals_team = linear.team(APPROVALS_TEAM)
    actions = []
    for action in state["actions"]:
        if action["requires_approval"]:
            child = existing.get(approval_key(action)) or linear.create_issue(
                teamId=approvals_team["id"],
                parentId=state["issue_id"],
                title=short_title(f"Approve for {action['team']}: {action['action']}"),
                description=approval_description(state, action),
                stateId=linear.state_id(APPROVALS_TEAM, "Needs Approval"),
                labelIds=[linear.label_id(action["team"], "team")],
            )
            action = action | {
                "approval_issue_id": child["id"],
                "approval_issue": child["identifier"],
            }
        actions.append(action)
    print(f"Approval sub-issues: {', '.join(a['approval_issue'] for a in actions if a.get('approval_issue'))}")
    return {"actions": actions}


def findings_comment(state: IncidentState) -> str:
    analysis = state["analysis"]
    finding_lines = "\n".join(
        f"- **{f['specialist']}** ({f['severity']}): {f['summary']}"
        for f in state["findings"]
    )
    action_lines = "\n".join(
        f"- {a['team']} / {a['service']}: {a['action']} "
        + (
            f"(needs team approval in {a['approval_issue']})"
            if a.get("approval_issue")
            else "(needs team approval)" if a["requires_approval"] else "(no approval needed)"
        )
        for a in state["actions"]
    )
    return (
        f"**Triage findings: {state['severity']}**\n\n"
        f"**Summary:** {analysis['summary']}\n\n"
        f"**Likely cause:** {analysis['likely_cause']}\n\n"
        f"**Rationale:** {analysis['rationale']}\n\n"
        f"**Specialists**\n{finding_lines}\n\n"
        f"**Proposed actions**\n{action_lines}"
    )


def report_findings(state: IncidentState):
    gated = any(action["requires_approval"] for action in state["actions"])
    if state.get("issue_id"):
        # Repeatable writes first, the comment last: a node that fails part-way
        # re-runs from its start, and an earlier comment would be posted twice.
        linear = linear_client()
        linear.set_group_label(state["issue_id"], "severity", state["severity"])
        if gated:
            linear.move(state["issue_id"], INCIDENTS_TEAM, "Awaiting Approval")
        linear.comment(state["issue_id"], findings_comment(state))
    return {"status": "awaiting_approval" if gated else "reported"}


def policy_route(state: IncidentState):
    gated = [action for action in state["actions"] if action["requires_approval"]]
    if not gated:
        return "publish_runbook"
    return [
        Send(
            "approval_gate",
            {key: value for key, value in action.items() if key != "requires_approval"},
        )
        for action in gated
    ]


def approval_gate(task: ActionTask):
    decision = interrupt(
        {
            "question": f"Approve this action for team {task['team']}?",
            "incident_id": task["incident_id"],
            "team": task["team"],
            "service": task["service"],
            "action": task["action"],
            "policy_reason": task["policy_reason"],
            "approval_issue_id": task.get("approval_issue_id", ""),
            "approval_issue": task.get("approval_issue", ""),
            "allowed_decisions": ["approve", "reject"],
        }
    )
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'.")
    if task.get("approval_issue_id"):
        # Keep the sub-issue in step when the decision came from the CLI instead of Linear.
        outcome = "Approved" if decision == "approve" else "Rejected"
        linear_client().move(task["approval_issue_id"], APPROVALS_TEAM, outcome)
    return {"approvals": [task | {"decision": decision}]}


def open_fix_pr(state: IncidentState):
    """After the gates: one fix PR for the first approved action on a service in the fix repo.

    It runs before publish_runbook, so the runbook can say where the fix is. Its
    comment is its last write, and propose_fix looks for an existing PR first, so
    a retry neither repeats the model call nor opens a second PR.
    """
    if not state.get("issue_id") or not fixer.configured() or state.get("fix"):
        return {}
    approved = [
        a for a in state.get("approvals", [])
        if a["decision"] == "approve" and a["service"] in FIXABLE_SERVICES
    ]
    if not approved:
        return {}
    action = approved[0]
    print(f"Fixer: patching {action['service']} for {state['incident_id']}...")
    fix = fixer.propose_fix(
        build_patcher, state["incident_id"], state["analysis"], action, action.get("approval_issue", "")
    )
    if fix["status"] == "opened":
        print(f"Fix PR: {fix['url']}")
        body = f"**Fix PR opened:** {fix['url']}\n\nThe repo's tests passed on the patch. Review it before you merge."
    else:
        print(f"Fix blocked: {fix['reason']}")
        body = (
            f"**Fix blocked:** {fix['reason']} No pull request was opened."
            + (f"\n\n```\n{fix['output']}\n```" if fix["output"] else "")
        )
    linear_client().comment(state["issue_id"], body)
    return {
        "fix": fix,
        "audit_log": state["audit_log"] + [f"Fixer for {action['service']}: {fix['status']}."],
    }


def runbook_markdown(state: IncidentState, heading: str, approved: list[dict], audit_log: list[str]) -> str:
    analysis = state["analysis"]
    finding_lines = "\n".join(
        f"- {f['specialist']} ({f['severity']}): {f['summary']}"
        for f in state["findings"]
    )
    action_lines = "\n".join(
        f"- {a['team']} / {a['service']}: {a['action']} "
        f"({'approved by team' if a['decision'] == 'approve' else 'approval not required'})"
        for a in approved
    )
    audit_lines = "\n".join(f"- {entry}" for entry in audit_log)
    fix = state.get("fix") or {}
    fix_line = f"Fix PR: {fix['url']}\n\n" if fix.get("status") == "opened" else ""
    return (
        f"{heading}\n\n"
        f"Incident ID: {state['incident_id']}\n\n"
        f"Incident: {state['incident']}\n\n"
        f"Severity: {state['severity']}\n\n"
        f"Summary: {analysis['summary']}\n\n"
        f"Likely cause: {analysis['likely_cause']}\n\n"
        f"Rationale: {analysis['rationale']}\n\n"
        f"{fix_line}"
        "Findings:\n"
        f"{finding_lines}\n\n"
        "Approved actions:\n"
        f"{action_lines}\n\n"
        "Audit log:\n"
        f"{audit_lines}\n"
    )


def publish_runbook(state: IncidentState):
    approvals = state.get("approvals", [])
    approved = [a for a in approvals if a["decision"] in ("approve", "not required")]
    decisions = [
        f"Team {a['team']} decision for {a['service']}: {a['decision']}."
        for a in approvals
    ]
    if not approved:
        if state.get("issue_id"):
            # Every action was turned down, so hand the incident back to people.
            linear = linear_client()
            linear.move(state["issue_id"], INCIDENTS_TEAM, "Triage")
            linear.comment(
                state["issue_id"],
                "**Every proposed action was rejected.** Nothing was published, "
                "and the incident is back in Triage.\n\n"
                + "\n".join(f"- {line}" for line in decisions),
            )
        return {
            "status": "rejected_without_publish",
            "audit_log": state["audit_log"]
            + decisions
            + ["Every action was rejected. Publishing was skipped."],
        }

    if state.get("issue_id"):
        # On Cloud Run the disk is wiped when the instance stops, so the Linear
        # comment is the runbook. A fixture run (--alert) still writes the file.
        location = f"Linear issue {state['incident_id']}"
        audit_log = state["audit_log"] + decisions + [f"Published runbook to {location}."]
        fix = state.get("fix") or {}
        linear = linear_client()
        if fix.get("status") == "opened":
            pass  # Linear's GitHub automation moves the incident: Fixing on PR open, Done on merge.
        elif fix.get("status") == "blocked":
            linear.move(state["issue_id"], INCIDENTS_TEAM, "Triage")  # the code fix needs a person
        else:
            linear.move(state["issue_id"], INCIDENTS_TEAM, "Done")
        linear.comment(
            state["issue_id"],
            runbook_markdown(state, "**Runbook published**", approved, audit_log),
        )
    else:
        RUNBOOK_DIR.mkdir(exist_ok=True)
        path = RUNBOOK_DIR / f"{safe_incident_id(state['incident_id'])}.md"
        location = str(path)
        audit_log = state["audit_log"] + decisions + [f"Published runbook to {path}."]
        path.write_text(
            runbook_markdown(state, "# Policy-Controlled Incident Runbook", approved, audit_log),
            encoding="utf-8",
        )
    return {
        "status": "published",
        "runbook_path": location,
        "audit_log": audit_log,
    }


_postgres_ready = threading.Lock()
_postgres_set_up = False


@contextmanager
def open_checkpointer():
    """SQLite on your machine. Postgres when DATABASE_URL is set, as on Cloud Run.

    Cloud Run's disk is in memory and is wiped when the instance stops, so a
    paused run saved there would be lost while it waits for an approval.
    """
    global _postgres_set_up
    url = os.environ.get("DATABASE_URL")
    if not url:
        with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
            yield checkpointer
        return
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(url) as checkpointer:
        with _postgres_ready:
            if not _postgres_set_up:
                checkpointer.setup()  # creates the checkpoint tables the first time
                _postgres_set_up = True
        yield checkpointer


def build_graph(checkpointer):
    builder = StateGraph(IncidentState)
    builder.add_node("pick_up_incident", pick_up_incident)
    builder.add_node("recall_history", recall_history)
    builder.add_node("route_incident", route_incident)
    builder.add_node("investigate", investigate)
    builder.add_node("analyze_incident", analyze_incident, defer=True)
    builder.add_node("policy_check", policy_check)
    builder.add_node("create_approval_subissues", create_approval_subissues)
    builder.add_node("report_findings", report_findings)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("open_fix_pr", open_fix_pr, defer=True)
    builder.add_node("publish_runbook", publish_runbook, defer=True)

    builder.add_edge(START, "pick_up_incident")
    builder.add_edge("pick_up_incident", "recall_history")
    builder.add_edge("recall_history", "route_incident")
    builder.add_conditional_edges(
        "route_incident", dispatch_specialists, ["investigate"]
    )
    builder.add_edge("investigate", "analyze_incident")
    builder.add_edge("analyze_incident", "policy_check")
    builder.add_edge("policy_check", "create_approval_subissues")
    builder.add_edge("create_approval_subissues", "report_findings")
    builder.add_conditional_edges(
        "report_findings", policy_route, ["approval_gate", "publish_runbook"]
    )
    builder.add_edge("approval_gate", "open_fix_pr")
    builder.add_edge("open_fix_pr", "publish_runbook")
    builder.add_edge("publish_runbook", END)

    return builder.compile(checkpointer=checkpointer)


def display_stream(stream):
    final_state = stream.output
    if stream.interrupted:
        display_pending(stream.interrupts)
        return

    print("WORKFLOW COMPLETE")
    print(f"Status: {final_state['status']}")
    print(f"Runbook: {final_state.get('runbook_path', 'not published')}")


def display_pending(interrupts):
    print(f"WORKFLOW PAUSED: {len(interrupts)} team approvals pending")
    for pending in interrupts:
        request = pending.value
        print(f"- {request['team']} ({request['service']}): {request['action']}")


def display_failure(graph, config, error):
    snapshot = graph.get_state(config)
    saved = [finding["specialist"] for finding in snapshot.values.get("findings", [])]
    print(f"WORKFLOW FAILED: {error}")
    print(f"Saved findings: {', '.join(saved) or 'none'}")
    print(f"Waiting to re-run: {', '.join(snapshot.next)}")


def report_failure(graph, config, error):
    """Say on the incident that the run stopped. On Cloud Run nobody sees the terminal."""
    values = graph.get_state(config).values
    if not values.get("issue_id"):
        return
    try:
        linear_client().comment(
            values["issue_id"],
            f"**Run stopped:** `{str(error)[:300]}`\n\n"
            "Its progress is saved. To retry from the last checkpoint, remove the "
            "`agent-triage` label and add it again.",
        )
    except Exception as comment_error:
        print(f"Couldn't post the failure to Linear: {comment_error}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a resumable incident-triage workflow."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--thread", help="Defaults to the issue identifier.")
    source = start_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--issue", help="A Linear incident, such as TRI-5.")
    source.add_argument("--alert", help="A fixture alert, without Linear.")
    start_parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        choices=list(SPECIALISTS),
        help="Specialists to hold back, to test that the score catches a weak run.",
    )

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--thread", required=True)
    resume_parser.add_argument(
        "--decision",
        choices=["approve", "reject"],
        help="Leave out to retry a failed run.",
    )
    resume_parser.add_argument("--team", help="The team whose action you decide.")

    args = parser.parse_args()
    if args.command == "resume" and args.decision and not args.team:
        parser.error("--decision needs --team.")
    if args.command == "start" and not (args.thread or args.issue):
        parser.error("--alert needs --thread.")
    if args.command == "start" and not args.thread:
        args.thread = args.issue
    return args


def main():
    args = parse_args()
    config = {"configurable": {"thread_id": args.thread}}
    try:
        run_command(args, config)
    finally:
        telemetry.shutdown()


def incident_input(thread: str, alert: dict, issue: dict | None = None, skip=()) -> dict:
    return {
        "incident_id": thread,
        "issue_id": issue["id"] if issue else "",
        "incident": f"{alert['title']}: {alert['description']}",
        "alert": alert,
        "status": "new",
        "audit_log": [],
        "skip_specialists": list(skip),
    }


def issue_input(issue_ref: str, thread: str | None = None, skip=()) -> tuple[str, dict]:
    """Read a Linear incident and build the graph input. The thread defaults to its identifier."""
    issue = linear_client().issue(issue_ref)
    alert_id = alert_fields(issue["description"])["alert"]
    alert = {a["id"]: a for a in load_json("alerts.json")}[alert_id]
    print(f"Picked up {issue['identifier']}: {issue['title']} ({alert_id})")
    thread = thread or issue["identifier"]
    return thread, incident_input(thread, alert, issue, skip)


def run_graph(graph, thread: str, command: str, graph_input) -> dict:
    """Run or resume one thread, then link its trace on the incident and score it."""
    config = {"configurable": {"thread_id": thread}}
    before = graph.get_state(config).values
    started = graph_input if isinstance(graph_input, dict) else before
    alert_id = started["alert"]["id"]
    trace_extra, handler = telemetry.trace_config(
        thread, command, alert_id, started.get("skip_specialists", [])
    )
    try:
        stream = graph.stream_events(
            graph_input,
            config={**config, **trace_extra},
            durability="sync",
            version="v3",
        )
        display_stream(stream)
    except Exception as error:
        display_failure(graph, config, error)
        report_failure(graph, config, error)

    values = graph.get_state(config).values
    if handler and handler.last_trace_id:
        url = telemetry.trace_url(handler.last_trace_id)
        print(f"Trace: {url}")
        if values.get("issue_id") and url:
            linear_client().attach(values["issue_id"], url, f"Langfuse trace: {command}", thread)
        if "analysis" in values and "analysis" not in before:
            correct = score_run(
                telemetry.langfuse_client(),
                handler.last_trace_id,
                alert_id,
                values["analysis"],
            )
            print(f"Score root_cause_correct: {correct}")
    return values


def run_command(args, config):
    with open_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)

        if args.command == "start" and args.issue:
            args.thread, graph_input = issue_input(args.issue, args.thread, args.skip)
        elif args.command == "start":
            alerts = {alert["id"]: alert for alert in load_json("alerts.json")}
            graph_input = incident_input(args.thread, alerts[args.alert], skip=args.skip)
        elif args.decision:
            pending = graph.get_state(config).interrupts
            team_requests = {
                request.id: args.decision
                for request in pending
                if request.value["team"] == args.team
            }
            if not team_requests:
                teams = sorted({request.value["team"] for request in pending})
                print(
                    f"No pending approval for team {args.team}. "
                    f"Waiting on: {', '.join(teams) or 'nobody'}"
                )
                return
            graph_input = Command(resume=team_requests)
            print(f"Recorded {args.decision} for {args.team}.")
        else:
            graph_input = None

        run_graph(graph, args.thread, args.command, graph_input)


if __name__ == "__main__":
    main()
