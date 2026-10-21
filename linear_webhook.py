"""Point Linear's webhook at the receiver, or delete it. Needs an Admin-access API key.

    python linear_webhook.py https://<your-domain>/linear/webhook
    python linear_webhook.py --delete

The signing secret comes from LINEAR_WEBHOOK_SECRET. Without one, this prints a
new secret to add to .env.
"""
import argparse
import os
import secrets

from linear import Linear

LABEL = "triage-receiver"


def find(linear: Linear) -> dict | None:
    hooks = linear.query("{ webhooks { nodes { id label url enabled } } }")["webhooks"]["nodes"]
    return next((hook for hook in hooks if hook["label"] == LABEL), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    linear = Linear()
    hook = find(linear)

    if args.delete:
        if hook:
            linear.query("mutation($id: String!) { webhookDelete(id: $id) { success } }", id=hook["id"])
            print(f"Deleted webhook {LABEL}.")
        else:
            print(f"No webhook named {LABEL}.")
        return
    if not args.url:
        parser.error("Pass the receiver URL, or --delete.")

    secret = os.environ.get("LINEAR_WEBHOOK_SECRET")
    if not secret:
        secret = secrets.token_hex(32)
        print(f"New signing secret. Add this line to .env:\nLINEAR_WEBHOOK_SECRET={secret}")
    fields = {"url": args.url, "secret": secret, "enabled": True}
    if hook:
        linear.query(
            "mutation($id: String!, $input: WebhookUpdateInput!) { webhookUpdate(id: $id, input: $input) { success } }",
            id=hook["id"],
            input=fields,
        )
        print(f"Updated webhook {LABEL}: {args.url}")
    else:
        # One webhook for both teams: Incidents starts runs, Approvals carries decisions.
        linear.query(
            "mutation($input: WebhookCreateInput!) { webhookCreate(input: $input) { success } }",
            input=fields | {"label": LABEL, "allPublicTeams": True, "resourceTypes": ["Issue"]},
        )
        print(f"Created webhook {LABEL}: {args.url}")


if __name__ == "__main__":
    main()
