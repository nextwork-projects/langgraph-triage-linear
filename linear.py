"""Linear's GraphQL API: every write the workflow makes to Linear goes through here.

The agents never write to Linear. Deterministic graph nodes call this client to
move statuses, set labels, create approval sub-issues, and post comments.
"""
import os
import re

import httpx

API_URL = "https://api.linear.app/graphql"
INCIDENTS_TEAM = os.environ.get("LINEAR_INCIDENTS_TEAM", "TRI")
APPROVALS_TEAM = os.environ.get("LINEAR_APPROVALS_TEAM", "APR")
TRIGGER_LABEL = "agent-triage"
ISSUE_FIELDS = """
    id identifier title description url
    team { id key }
    state { id name type }
    labels { nodes { id name parent { name } } }
"""


class LinearError(RuntimeError):
    pass


class Linear:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ.get("LINEAR_API_KEY")
        if not key:
            raise RuntimeError("LINEAR_API_KEY is not set.")
        # A personal API key goes in the header as is, with no "Bearer" prefix.
        self.http = httpx.Client(
            base_url=API_URL, headers={"Authorization": key}, timeout=30
        )
        self._teams: dict[str, dict] = {}
        self._states: dict[str, dict[str, str]] = {}
        self._labels: dict[tuple[str | None, str], str] | None = None

    def query(self, query: str, **variables) -> dict:
        response = self.http.post("", json={"query": query, "variables": variables})
        try:
            body = response.json()
        except ValueError:
            raise LinearError(f"Linear answered HTTP {response.status_code}: {response.text[:200]!r}")
        if body.get("errors"):
            raise LinearError(body["errors"][0]["message"])
        response.raise_for_status()
        return body["data"]

    # Lookups by name, so no state or label IDs are hard-coded.

    def team(self, key: str) -> dict:
        if key in self._teams:
            return self._teams[key]
        teams = self.query(
            "query($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id key name } } }",
            key=key,
        )["teams"]["nodes"]
        if not teams:
            raise LinearError(f"No Linear team with key {key}. Run linear_setup.py.")
        self._teams[key] = teams[0]
        return teams[0]

    def state_id(self, team_key: str, name: str) -> str:
        if team_key not in self._states:
            nodes = self.query(
                "query($key: String!) { workflowStates(filter: { team: { key: { eq: $key } } }) { nodes { id name } } }",
                key=team_key,
            )["workflowStates"]["nodes"]
            self._states[team_key] = {node["name"]: node["id"] for node in nodes}
        try:
            return self._states[team_key][name]
        except KeyError:
            raise LinearError(f"Team {team_key} has no state {name!r}. Run linear_setup.py.")

    def labels(self) -> dict[tuple[str | None, str], str]:
        """Workspace labels keyed by (group, name), e.g. ("team", "payments")."""
        if self._labels is None:
            nodes = self.query(
                "{ issueLabels(first: 250) { nodes { id name parent { name } } } }"
            )["issueLabels"]["nodes"]
            self._labels = {
                ((node["parent"] or {}).get("name"), node["name"]): node["id"]
                for node in nodes
            }
        return self._labels

    def label_id(self, name: str, group: str | None = None) -> str:
        try:
            return self.labels()[(group, name)]
        except KeyError:
            where = f"group {group!r}" if group else "the workspace"
            raise LinearError(f"No label {name!r} in {where}. Run linear_setup.py.")

    # Reads.

    def issue(self, ref: str) -> dict:
        """One issue by UUID or identifier, such as TRI-5."""
        return self.query(
            f"query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}", id=ref
        )["issue"]

    def sub_issues(self, parent_id: str) -> list[dict]:
        return self.query(
            f"query($id: String!) {{ issue(id: $id) {{ children {{ nodes {{ {ISSUE_FIELDS} }} }} }} }}",
            id=parent_id,
        )["issue"]["children"]["nodes"]

    # Writes.

    def move(self, issue_id: str, team_key: str, state: str) -> None:
        self.query(
            "mutation($id: String!, $state: String!) { issueUpdate(id: $id, input: { stateId: $state }) { success } }",
            id=issue_id,
            state=self.state_id(team_key, state),
        )

    def set_group_label(self, issue_id: str, group: str, name: str) -> None:
        """Add one label from a group and remove the group's others (a group allows one).

        removedLabelIds may only name labels the issue has, or Linear answers
        "Label not on issue", so read the issue's labels first.
        """
        others = [
            label["id"]
            for label in self.issue(issue_id)["labels"]["nodes"]
            if (label["parent"] or {}).get("name") == group and label["name"] != name
        ]
        self.query(
            "mutation($id: String!, $add: [String!], $remove: [String!]) {"
            " issueUpdate(id: $id, input: { addedLabelIds: $add, removedLabelIds: $remove }) { success } }",
            id=issue_id,
            add=[self.label_id(name, group)],
            remove=others,
        )

    def comment(self, issue_id: str, body: str) -> str:
        return self.query(
            "mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { comment { id } } }",
            id=issue_id,
            body=body,
        )["commentCreate"]["comment"]["id"]

    def attach(self, issue_id: str, url: str, title: str, subtitle: str = "") -> None:
        # attachmentCreate updates in place when the issue and URL match, so re-runs don't duplicate.
        self.query(
            "mutation($input: AttachmentCreateInput!) { attachmentCreate(input: $input) { success } }",
            input={"issueId": issue_id, "url": url, "title": title, "subtitle": subtitle},
        )

    def create_issue(self, **fields) -> dict:
        return self.query(
            "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { issue { id identifier url } } }",
            input=fields,
        )["issueCreate"]["issue"]


_client: Linear | None = None


def client() -> Linear:
    """The one shared client, so the team, state, and label lookups are cached once."""
    global _client
    if _client is None:
        _client = Linear()
    return _client


# An incident issue names the fixture alert it came from, so the specialists can
# read that alert's logs, deploys, and metrics. seed_incidents.py writes it as:
#
#   Alert: INC-2041
#   Service: checkout-api
#   Fired: 2026-10-09T14:15:00Z
ALERT_LINE = re.compile(r"^[*_\s]*(Alert|Service|Fired)[*_\s]*:[*_\s]*(\S+)\s*$", re.MULTILINE)


def alert_fields(description: str | None) -> dict:
    """Read the Alert, Service and Fired lines from an incident's description."""
    fields = {key.lower(): value for key, value in ALERT_LINE.findall(description or "")}
    if "alert" not in fields:
        raise LinearError("The incident's description has no 'Alert: INC-…' line.")
    return fields
