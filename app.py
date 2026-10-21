import argparse
import json
import os
import operator
from pathlib import Path
from typing import Annotated, TypedDict

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from specialists import SPECIALISTS, alert_brief, build_model, load_json, run_specialist
import telemetry
from prompts import analyst_messages
from score import score_run

DB_PATH = "triage_checkpoints.db"
SEVERITY_ORDER = ["SEV4", "SEV3", "SEV2", "SEV1"]
RUNBOOK_DIR = Path("runbooks")
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
    skip_specialists: list[str]


class SpecialistTask(TypedDict):
    specialist: str
    brief: str


class ActionTask(TypedDict):
    incident_id: str
    team: str
    service: str
    action: str
    policy_reason: str


def build_analyst():
    return build_model().with_structured_output(
        schema=ANALYSIS_SCHEMA,
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
    return alert_brief(state["alert"]["id"])


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
            "allowed_decisions": ["approve", "reject"],
        }
    )
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'.")
    return {"approvals": [task | {"decision": decision}]}


def publish_runbook(state: IncidentState):
    approvals = state.get("approvals", [])
    approved = [a for a in approvals if a["decision"] in ("approve", "not required")]
    decisions = [
        f"Team {a['team']} decision for {a['service']}: {a['decision']}."
        for a in approvals
    ]
    if not approved:
        return {
            "status": "rejected_without_publish",
            "audit_log": state["audit_log"]
            + decisions
            + ["Every action was rejected. Publishing was skipped."],
        }

    RUNBOOK_DIR.mkdir(exist_ok=True)
    path = RUNBOOK_DIR / f"{safe_incident_id(state['incident_id'])}.md"
    analysis = state["analysis"]
    audit_log = state["audit_log"] + decisions + [f"Published runbook to {path}."]
    audit_lines = "\n".join(f"- {entry}" for entry in audit_log)
    finding_lines = "\n".join(
        f"- {f['specialist']} ({f['severity']}): {f['summary']}"
        for f in state["findings"]
    )
    action_lines = "\n".join(
        f"- {a['team']} / {a['service']}: {a['action']} "
        f"({'approved by team' if a['decision'] == 'approve' else 'approval not required'})"
        for a in approved
    )

    path.write_text(
        "# Policy-Controlled Incident Runbook\n\n"
        f"Incident ID: {state['incident_id']}\n\n"
        f"Incident: {state['incident']}\n\n"
        f"Severity: {state['severity']}\n\n"
        f"Summary: {analysis['summary']}\n\n"
        f"Likely cause: {analysis['likely_cause']}\n\n"
        f"Rationale: {analysis['rationale']}\n\n"
        "Findings:\n"
        f"{finding_lines}\n\n"
        "Approved actions:\n"
        f"{action_lines}\n\n"
        "Audit log:\n"
        f"{audit_lines}\n",
        encoding="utf-8",
    )
    return {
        "status": "published",
        "runbook_path": str(path),
        "audit_log": audit_log,
    }


def build_graph(checkpointer: SqliteSaver):
    builder = StateGraph(IncidentState)
    builder.add_node("route_incident", route_incident)
    builder.add_node("investigate", investigate)
    builder.add_node("analyze_incident", analyze_incident, defer=True)
    builder.add_node("policy_check", policy_check)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("publish_runbook", publish_runbook, defer=True)

    builder.add_edge(START, "route_incident")
    builder.add_conditional_edges(
        "route_incident", dispatch_specialists, ["investigate"]
    )
    builder.add_edge("investigate", "analyze_incident")
    builder.add_edge("analyze_incident", "policy_check")
    builder.add_conditional_edges(
        "policy_check", policy_route, ["approval_gate", "publish_runbook"]
    )
    builder.add_edge("approval_gate", "publish_runbook")
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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a resumable incident-triage workflow."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--thread", required=True)
    start_parser.add_argument("--alert", required=True)
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
    return args


def main():
    args = parse_args()
    config = {"configurable": {"thread_id": args.thread}}
    try:
        run_command(args, config)
    finally:
        telemetry.shutdown()


def run_command(args, config):
    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        graph = build_graph(checkpointer)
        before = graph.get_state(config).values

        if args.command == "start":
            alerts = {alert["id"]: alert for alert in load_json("alerts.json")}
            alert = alerts[args.alert]
            graph_input = {
                "incident_id": args.thread,
                "incident": f"{alert['title']}: {alert['description']}",
                "alert": alert,
                "status": "new",
                "audit_log": [],
                "skip_specialists": args.skip,
            }
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

        alert_id = args.alert if args.command == "start" else before["alert"]["id"]
        skip = args.skip if args.command == "start" else before.get("skip_specialists", [])
        trace_extra, handler = telemetry.trace_config(
            args.thread, args.command, alert_id, skip
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

        values = graph.get_state(config).values
        if handler and handler.last_trace_id:
            print(f"Trace: {telemetry.trace_url(handler.last_trace_id)}")
            if "analysis" in values and "analysis" not in before:
                correct = score_run(
                    telemetry.langfuse_client(),
                    handler.last_trace_id,
                    alert_id,
                    values["analysis"],
                )
                print(f"Score root_cause_correct: {correct}")

if __name__ == "__main__":
    main()
