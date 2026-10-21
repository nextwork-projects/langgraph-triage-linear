# Incident Triage in Linear

Starter code for the NextWork Project **Connect the Triage Workflow to Linear**.

It is the finished code of **Observe a Multi-Agent Triage Workflow with Langfuse**: a LangGraph incident-triage workflow with specialist agents, per-team approval gates, Langfuse traces and a `root_cause_correct` score. In this Project you connect it to Linear, so a new Linear incident starts the run, each owning team approves in Linear, and an approved fix becomes a pull request.

The `solution` branch has the finished Project.

## What's in the repo

| Path | What it is |
|---|---|
| `app.py` | The triage workflow from the Langfuse Project. You add the Linear steps here |
| `specialists.py`, `telemetry.py`, `score.py`, `prompts.py`, `status.py` | Unchanged from the Langfuse Project |
| `data/` | Fixtures: `INC-2041` (planted cause `d-1042`) and `INC-2042` (planted cause `d-1039`) |
| `linear.py` | A ready-made client for Linear's GraphQL API: lookups by name, moves, labels, comments, sub-issues, attachments |
| `github_api.py` | A ready-made client for the GitHub REST calls that open a pull request |
| `linear_setup.py` | Shapes your Linear workspace: Incidents (TRI) and Approvals (APR) teams, their states, and the labels. Safe to run again |
| `seed_incidents.py` | Opens Linear incidents from `data/alerts.json`, as a monitoring tool would |
| `linear_webhook.py` | Points Linear's webhook at your receiver, or deletes it |
| `Dockerfile`, `.dockerignore`, `.gcloudignore` | The Cloud Run image for the webhook receiver you build (`receiver.py`) |
| `teardown.sh` | Deletes everything the Cloud Run deploy creates, or stops Cloud SQL between sessions |
| `tests/` | Offline tests for the starter code |

You build `receiver.py`, `fixer.py`, and the Linear steps in `app.py` during the Project.

## Set up

You need a Google Cloud project with billing, a free Linear workspace of your own, a Langfuse Cloud account, a GitHub account, and Python 3.12 (Cloud Shell has it).

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

## Tests

```bash
python3 -m pytest
```

All tests are offline.
