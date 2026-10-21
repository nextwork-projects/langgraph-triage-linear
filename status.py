import argparse
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from app import DB_PATH, RUNBOOK_DIR, build_graph, safe_incident_id


def parse_args():
    parser = argparse.ArgumentParser(
        description="Show an incident's status without resuming it."
    )
    parser.add_argument("--thread", required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    if not Path(DB_PATH).exists():
        raise SystemExit(f"No checkpoint database found at {DB_PATH}.")
    config = {"configurable": {"thread_id": args.thread}}

    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        snapshot = build_graph(checkpointer).get_state(config)

    values = snapshot.values
    if not values:
        raise SystemExit(f"No saved thread named {args.thread}.")

    print(f"INCIDENT STATUS: {args.thread}")
    print(f"Status: {values.get('status')}  Severity: {values.get('severity')}")

    print("Findings:")
    for finding in values.get("findings", []):
        print(f"  {finding['specialist']} ({finding['severity']}): {finding['summary']}")

    print("Decisions so far:")
    for approval in values.get("approvals", []):
        print(f"  {approval['team']} ({approval['service']}): {approval['decision']}")

    print("Waiting on:")
    for pending in snapshot.interrupts:
        request = pending.value
        print(
            f"  {request['team']} ({request['service']}): {request['action']} "
            f"[interrupt {pending.id[:8]}]"
        )
    if not snapshot.interrupts:
        print("  nobody")

    print(f"Next: {', '.join(snapshot.next) or 'finished'}")

    runbook = RUNBOOK_DIR / f"{safe_incident_id(args.thread)}.md"
    print(f"Runbook: {runbook if runbook.exists() else 'not published'}")


if __name__ == "__main__":
    main()
