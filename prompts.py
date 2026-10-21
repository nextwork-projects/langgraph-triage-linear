"""Secret Mission: serve the analyst prompt from Langfuse prompt management.

`python3 prompts.py push` uploads the original analyst prompt as version 1
with the `production` label. After that, every analysis fetches the
`production` version, and Langfuse links each analyst generation to the
version it used.
"""
import telemetry

ANALYST_PROMPT = "triage-analyst"
ANALYST_MESSAGES = [
    {
        "role": "system",
        "content": "You are an SRE incident analyst. Combine the specialists' "
        "findings into one diagnosis and name the most likely cause with "
        "its evidence. Propose one reversible remediation step for each "
        "service the evidence implicates: the alerting service and every "
        "dependency named in the findings' evidence, at most three. Each "
        "service's owning team must approve its own step. Keep every "
        "field concise.",
    },
    {"role": "user", "content": "{{incident}}\n\nFindings:\n{{findings}}"},
]


def analyst_messages(incident: str, findings: str) -> tuple[object | None, list[dict]]:
    """Return the Langfuse prompt (None when not from Langfuse) and the compiled messages."""
    variables = {"incident": incident, "findings": findings}
    if not telemetry.tracing_enabled():
        return None, compile_local(variables)
    prompt = telemetry.langfuse_client().get_prompt(
        ANALYST_PROMPT, type="chat", fallback=ANALYST_MESSAGES
    )
    if prompt.is_fallback:
        print(f"Analyst prompt: local fallback ({ANALYST_PROMPT} not in Langfuse yet)")
        return None, prompt.compile(**variables)
    print(f"Analyst prompt: {ANALYST_PROMPT} v{prompt.version}")
    return prompt, prompt.compile(**variables)


def compile_local(variables: dict) -> list[dict]:
    messages = []
    for message in ANALYST_MESSAGES:
        content = message["content"]
        for name, value in variables.items():
            content = content.replace("{{" + name + "}}", value)
        messages.append({"role": message["role"], "content": content})
    return messages


def push():
    prompt = telemetry.langfuse_client().create_prompt(
        name=ANALYST_PROMPT,
        type="chat",
        prompt=ANALYST_MESSAGES,
        labels=["production"],
        commit_message="Original analyst prompt",
    )
    telemetry.shutdown()
    print(f"Created {ANALYST_PROMPT} v{prompt.version} with the production label.")


if __name__ == "__main__":
    push()
