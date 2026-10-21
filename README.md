# Incident Triage in Linear: Demo

The author's working proof for the NextWork Project **Connect the Triage Workflow to Linear**. It starts from the finished code of **Observe a Multi-Agent Triage Workflow with Langfuse** (the `solution` branch of `langgraph-triage-observability`) and puts the workflow inside Linear:

- **A new Linear incident starts the run.** An issue in the Incidents team with the `agent-triage` label reaches a FastAPI receiver on Cloud Run through a signed webhook. No CLI is needed after deploy.
- **The run reports in Linear.** The incident moves through Triage → Investigating → Awaiting Approval, gets a `severity` label, a findings comment, and a link to its Langfuse trace.
- **Each owning team approves in Linear.** Every gated action becomes a sub-issue in the Approvals team with a `team` label. Moving it to Approved or Rejected resumes only that team's paused gate.
- **An approved fix becomes a pull request.** Gemini patches `triage-services` (your copy of the bug repo), the repo's own tests gate the patch, and the PR body says `Fixes TRI-n`, so Linear links it. A person reviews and merges.
- **Secret Mission:** a read-only agent looks up earlier incidents through Linear's MCP server before the router decides.

Every write to Linear and GitHub is plain code in a graph node. The models only read and suggest.

## What's verified

| Part | How |
|---|---|
| Linear setup, run from an issue, sub-issues across teams, webhook resume, two approvals at once, Cloud Run + Cloud SQL, retry after a redeploy | Live on TRI-5 to TRI-8, 2026-10-20 |
| Fix PR (`fixer.py`, `open_fix_pr`) | Offline only: a fake GitHub, a mock transport for the REST calls, and the real test gate in a subprocess |
| Linear's PR automations (Fixing on open, Done on merge) on the Free plan | Not tested |
| Read-only MCP agent (`linear_mcp.py`) | Offline only: `MCPAdapter` against a local FastMCP server, with the Bearer key checked over HTTP in a separate test |
| Dedicated service account, `teardown.sh` | Not run yet |


## What's in the repo

| Path | What it is |
|---|---|
| `app.py` | The triage graph, plus the Linear nodes: `pick_up_incident`, `create_approval_subissues`, `report_findings`, `open_fix_pr`, and the Linear runbook in `publish_runbook` |
| `linear.py` | The GraphQL client: every Linear write, and lookups by name so no IDs are hard-coded |
| `receiver.py` | The webhook receiver: signature and timestamp checks, a 200 before any Linear call, then start or resume in the background |
| `github_api.py` | The GitHub REST client: download the repo, find or open the PR |
| `fixer.py` | The code fix: patch one file in the service's package, run the repo's tests with no secrets, then open the PR |
| `linear_mcp.py` | Secret Mission: the read-only MCP agent |
| `linear_setup.py` | Shapes the workspace: Incidents (TRI) and Approvals (APR) teams, their states, and the labels |
| `seed_incidents.py` | Opens Linear incidents from `data/alerts.json`, as a monitoring tool would |
| `linear_webhook.py` | Points Linear's webhook at the receiver, or deletes it |
| `Dockerfile`, `teardown.sh` | The Cloud Run image, and the cleanup |
| `specialists.py`, `telemetry.py`, `score.py`, `prompts.py`, `status.py`, `data/` | Unchanged from the earlier Projects |
| `tests/` | Offline tests: fake models, a fake Linear, a fake GitHub |

## Set up

You need:

- a Google Cloud project with billing;
- a free Linear workspace of your own (not your company's);
- a Langfuse Cloud account;
- a GitHub account with a copy of `triage-services` made with **Use this template**;
- Python 3.12, or Cloud Shell.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com run.googleapis.com sqladmin.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env   # then fill it in
set -a && . ./.env && set +a
python linear_setup.py   # safe to run again
```

In Linear, open **Settings > Teams > Incidents > Workflows & automations** and set the pull request automations: **Fixing** when a PR opens, **Done** when it merges. Install Linear's GitHub app on your `triage-services` repo.

## Run locally

```bash
python seed_incidents.py INC-2041 --no-label       # prints TRI-n
python app.py start --issue TRI-n
python app.py resume --thread TRI-n --team payments --decision approve
python app.py resume --thread TRI-n --team platform-data --decision approve
```

Without `--issue`, `python app.py start --thread obs-001 --alert INC-2041` still runs the fixture alert with no Linear calls and writes `runbooks/obs-001.md`.

## Deploy to Cloud Run

```bash
PROJECT=$(gcloud config get-value project) REGION=us-central1
gcloud sql instances create triage-pg --edition=ENTERPRISE --tier=db-f1-micro \
  --database-version=POSTGRES_16 --region=$REGION          # about 8 minutes
PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
gcloud sql databases create triage --instance=triage-pg
gcloud sql users create triage --instance=triage-pg --password="$PW"   # = matters: the password can start with -

SA=triage-receiver@$PROJECT.iam.gserviceaccount.com
gcloud iam service-accounts create triage-receiver
for role in roles/aiplatform.user roles/cloudsql.client; do
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=$role
done

DB_URL="postgresql://triage:$PW@/triage?host=/cloudsql/$PROJECT:$REGION:triage-pg"
for pair in linear-api-key=$LINEAR_API_KEY langfuse-secret-key=$LANGFUSE_SECRET_KEY \
            linear-webhook-secret=$LINEAR_WEBHOOK_SECRET database-url=$DB_URL github-token=$GITHUB_TOKEN; do
  printf %s "${pair#*=}" | gcloud secrets create "${pair%%=*}" --data-file=-
  gcloud secrets add-iam-policy-binding "${pair%%=*}" --member=serviceAccount:$SA \
    --role=roles/secretmanager.secretAccessor
done

gcloud run deploy triage-receiver --source . --region $REGION --service-account $SA \
  --no-cpu-throttling --cpu-boost --min 0 --max 1 --memory 1Gi --no-invoker-iam-check \
  --add-cloudsql-instances $PROJECT:$REGION:triage-pg \
  --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT,LANGFUSE_PUBLIC_KEY=$LANGFUSE_PUBLIC_KEY,LANGFUSE_BASE_URL=$LANGFUSE_BASE_URL,LANGFUSE_TRACING_ENVIRONMENT=$LANGFUSE_TRACING_ENVIRONMENT,GITHUB_REPO=$GITHUB_REPO \
  --set-secrets LINEAR_API_KEY=linear-api-key:1,LANGFUSE_SECRET_KEY=langfuse-secret-key:1,LINEAR_WEBHOOK_SECRET=linear-webhook-secret:1,DATABASE_URL=database-url:1,GITHUB_TOKEN=github-token:1

python linear_webhook.py "$(gcloud run services describe triage-receiver --region $REGION --format='value(status.url)')/linear/webhook"
python seed_incidents.py INC-2041   # with agent-triage: the run starts on Cloud Run
```

- `--no-cpu-throttling` keeps the CPU on after the 200, so the background run isn't starved.
- Cloud Run's disk is in memory, so checkpoints go to Postgres. On Cloud Run the runbook is the Linear comment, not a file.
- A cold start takes 3.6 to 5.45 s, against Linear's 5 s limit. A late delivery is still handled, and Linear's retry a minute later finds the work done.
- **Don't redeploy while a run is going.** Cloud Run stops the old instance about 10 s later, and the run dies. To retry it from its last checkpoint, remove `agent-triage` from the incident and add it again.
- Read the logs with `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="triage-receiver"'`.

## Tests

```bash
python3 -m pytest
```

All tests are offline. Leave `DATABASE_URL` unset.

## Cost

| Item | Cost |
|---|---|
| Gemini 3.7 Flash | About $0.02 to $0.03 per run, including the fixer |
| Cloud SQL `db-f1-micro` | $0.0105 an hour while it runs. A stopped instance bills only its storage. **About $9.40 a month if you forget it.** |
| Cloud Run, Cloud Build, Secret Manager, Artifact Registry | Inside the free tiers |
| Linear Free, GitHub, Langfuse Hobby | $0 |

Linear's Free plan allows 250 issues. Each run uses about three (the incident and its sub-issues).

## Tear down

```bash
set -a && . ./.env && set +a
PROJECT=YOUR_PROJECT_ID ./teardown.sh          # or --stop / --start for Cloud SQL between sessions
```

Or delete the whole project with `gcloud projects delete YOUR_PROJECT_ID`. Revoke the Linear keys and the GitHub token.
