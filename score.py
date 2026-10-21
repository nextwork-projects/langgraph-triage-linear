"""Secret Mission: score every triage run in Langfuse against the planted cause."""
# Each fixture alert has one planted cause: the deploy that broke it.
ROOT_CAUSES = {"INC-2041": "d-1042", "INC-2042": "d-1039"}


def root_cause_correct(alert_id: str, analysis: dict) -> bool:
    """True when the synthesis names the planted deploy as the likely cause."""
    expected = ROOT_CAUSES.get(alert_id)
    return bool(expected) and expected in analysis.get("likely_cause", "")


def score_run(langfuse, trace_id: str, alert_id: str, analysis: dict) -> bool:
    correct = root_cause_correct(alert_id, analysis)
    langfuse.create_score(
        name="root_cause_correct",
        value=1 if correct else 0,
        trace_id=trace_id,
        data_type="BOOLEAN",
        comment=f"Expected {ROOT_CAUSES.get(alert_id)} in: {analysis.get('likely_cause', '')}",
    )
    return correct
