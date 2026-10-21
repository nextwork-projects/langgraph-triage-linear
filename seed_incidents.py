"""Open Linear incidents from the fixture alerts, as a monitoring tool would.

Each call creates new issues, so every run gets a fresh incident and thread.
"""
import argparse

from linear import INCIDENTS_TEAM, TRIGGER_LABEL, Linear
from specialists import load_json


def description(alert: dict) -> str:
    return (
        f"Alert: {alert['id']}\n"
        f"Service: {alert['service']}\n"
        f"Fired: {alert['fired_at']}\n\n"
        f"{alert['description']}"
    )


def main():
    alerts = {alert["id"]: alert for alert in load_json("alerts.json")}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("alerts", nargs="*", choices=list(alerts), default=list(alerts))
    parser.add_argument(
        "--no-label", action="store_true", help=f"Leave off {TRIGGER_LABEL}, so the webhook doesn't start a run."
    )
    args = parser.parse_args()

    linear = Linear()
    team = linear.team(INCIDENTS_TEAM)
    for alert_id in args.alerts:
        alert = alerts[alert_id]
        issue = linear.create_issue(
            teamId=team["id"],
            title=alert["title"],
            description=description(alert),
            stateId=linear.state_id(INCIDENTS_TEAM, "Triage"),
            labelIds=[] if args.no_label else [linear.label_id(TRIGGER_LABEL)],
        )
        print(f"{issue['identifier']}  {alert_id}  {issue['url']}")


if __name__ == "__main__":
    main()
