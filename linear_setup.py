"""Shape the Linear workspace for the triage workflow. Safe to run more than once.

- The existing team (key TRI) becomes Incidents, with the states a run moves through.
- A second team, Approvals (key APR), holds one sub-issue per gated action.
- Workspace labels: agent-triage, a severity group, and a team group with every
  owning team in data/services.json.
"""
from linear import APPROVALS_TEAM, INCIDENTS_TEAM, TRIGGER_LABEL, Linear, LinearError
from specialists import load_json

INCIDENT_STATES = [
    ("Triage", "unstarted", "#95a2b3"),
    ("Investigating", "started", "#f2c94c"),
    ("Awaiting Approval", "started", "#f2994a"),
    ("Fixing", "started", "#5e6ad2"),
]
APPROVAL_STATES = [
    ("Needs Approval", "unstarted", "#f2994a"),
    ("Approved", "completed", "#4cb782"),
    ("Rejected", "canceled", "#eb5757"),
]
SEVERITIES = ["SEV1", "SEV2", "SEV3", "SEV4"]


def ensure_team(linear: Linear, key: str, name: str) -> dict:
    try:
        team = linear.team(key)
    except LinearError:
        team = linear.query(
            "mutation($name: String!, $key: String!) { teamCreate(input: { name: $name, key: $key }) { team { id key name } } }",
            name=name,
            key=key,
        )["teamCreate"]["team"]
        print(f"Created team {name} ({key}).")
        return team
    if team["name"] != name:
        linear.query(
            "mutation($id: String!, $name: String!) { teamUpdate(id: $id, input: { name: $name }) { success } }",
            id=team["id"],
            name=name,
        )
        print(f"Renamed team {key} from {team['name']} to {name}.")
    return team


def ensure_states(linear: Linear, team: dict, states: list[tuple[str, str, str]]) -> None:
    for name, kind, color in states:
        try:
            linear.state_id(team["key"], name)
        except LinearError:
            linear.query(
                "mutation($input: WorkflowStateCreateInput!) { workflowStateCreate(input: $input) { success } }",
                input={"teamId": team["id"], "name": name, "type": kind, "color": color},
            )
            print(f"  {team['key']}: added state {name} ({kind}).")
    linear._states.pop(team["key"], None)


def ensure_label(linear: Linear, name: str, group: str | None = None, is_group=False) -> str:
    try:
        return linear.label_id(name, group)
    except LinearError:
        fields = {"name": name, "isGroup": is_group}
        if group:
            fields["parentId"] = linear.label_id(group)
        label_id = linear.query(
            "mutation($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) { issueLabel { id } } }",
            input=fields,
        )["issueLabelCreate"]["issueLabel"]["id"]
        linear.labels()[(group, name)] = label_id
        print(f"  label {group + ': ' if group else ''}{name}")
        return label_id


def main():
    linear = Linear()
    incidents = ensure_team(linear, INCIDENTS_TEAM, "Incidents")
    ensure_states(linear, incidents, INCIDENT_STATES)
    approvals = ensure_team(linear, APPROVALS_TEAM, "Approvals")
    ensure_states(linear, approvals, APPROVAL_STATES)
    # New sub-issues land in Needs Approval unless the code says otherwise.
    linear.query(
        "mutation($id: String!, $state: String!) { teamUpdate(id: $id, input: { defaultIssueStateId: $state }) { success } }",
        id=approvals["id"],
        state=linear.state_id(APPROVALS_TEAM, "Needs Approval"),
    )

    ensure_label(linear, TRIGGER_LABEL)
    ensure_label(linear, "severity", is_group=True)
    for severity in SEVERITIES:
        ensure_label(linear, severity, "severity")
    ensure_label(linear, "team", is_group=True)
    owners = sorted({service["team"] for service in load_json("services.json").values()})
    for owner in owners + ["unowned"]:
        ensure_label(linear, owner, "team")

    print("Linear is ready:")
    print(f"  Incidents ({INCIDENTS_TEAM}): {', '.join(name for name, _, _ in INCIDENT_STATES)}, Done")
    print(f"  Approvals ({APPROVALS_TEAM}): {', '.join(name for name, _, _ in APPROVAL_STATES)}")
    print(f"  Labels: {TRIGGER_LABEL}, severity ({len(SEVERITIES)}), team ({len(owners) + 1})")


if __name__ == "__main__":
    main()
