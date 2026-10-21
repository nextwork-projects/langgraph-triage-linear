#!/usr/bin/env bash
# Delete everything the Cloud Run deploy created, so nothing keeps billing.
# Cloud SQL is the one that costs money while idle (about $9.40 a month).
#
#   PROJECT=your-project-id ./teardown.sh
#   PROJECT=your-project-id ./teardown.sh --stop   # stop Cloud SQL between sessions instead
#   PROJECT=your-project-id ./teardown.sh --start  # and start it again
set -uo pipefail

PROJECT=${PROJECT:?Set PROJECT to your Google Cloud project ID.}
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-triage-receiver}
INSTANCE=${INSTANCE:-triage-pg}
SA=${SA:-triage-receiver}

if [[ "${1:-}" == "--stop" ]]; then
  # A stopped instance bills only its storage (about $1.70 a month for 10 GiB).
  gcloud sql instances patch "$INSTANCE" --project "$PROJECT" --activation-policy=NEVER --quiet
  exit
fi
if [[ "${1:-}" == "--start" ]]; then
  gcloud sql instances patch "$INSTANCE" --project "$PROJECT" --activation-policy=ALWAYS --quiet
  exit
fi

# Needs LINEAR_API_KEY: set -a && . ./.env && set +a
python linear_webhook.py --delete
gcloud run services delete "$SERVICE" --project "$PROJECT" --region "$REGION" --quiet
gcloud sql instances delete "$INSTANCE" --project "$PROJECT" --quiet
for secret in linear-api-key langfuse-secret-key linear-webhook-secret database-url github-token linear-readonly-key; do
  gcloud secrets delete "$secret" --project "$PROJECT" --quiet
done
gcloud iam service-accounts delete "$SA@$PROJECT.iam.gserviceaccount.com" --project "$PROJECT" --quiet
gcloud artifacts repositories delete cloud-run-source-deploy --project "$PROJECT" --location "$REGION" --quiet
echo "Done. Or delete the whole project: gcloud projects delete $PROJECT"
