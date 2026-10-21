"""The code fix: after a team approves, Gemini patches the service and a PR opens.

The model only writes the new file. Everything else is plain code: it downloads
the repo, checks the patch stays inside the service's package, runs the repo's
own tests, and opens the pull request only when they pass. The agent never
merges; a person reviews and merges the PR.

Needs GITHUB_TOKEN (a fine-grained PAT for one repo: Contents and Pull requests,
read and write) and GITHUB_REPO (owner/name). Without them the fix is skipped.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from github_api import GitHub

TEST_TIMEOUT_S = 120

PATCH_SCHEMA = {
    "title": "CodeFix",
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The one file to replace, relative to the repo root."},
        "content": {"type": "string", "description": "The whole new file."},
        "summary": {"type": "string", "description": "One sentence for the PR title, under 70 characters."},
    },
    "required": ["path", "content", "summary"],
}


def configured() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"))


def package_for(service: str) -> str:
    """checkout-api lives in checkout_api/."""
    return service.replace("-", "_")


def source_files(root: Path, package: str) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted((root / package).rglob("*.py"))
    }


def check_patch(patch: dict, files: dict[str, str], package: str) -> str | None:
    """Why a patch is refused, or None. It may only replace one existing file in the service's package.

    So the model can't edit the tests that gate it, or add a file the tests would import.
    """
    path = patch["path"].strip().lstrip("./")
    if path not in files:
        return f"{path} is not an existing file in {package}/."
    if patch["content"] == files[path]:
        return f"The patch doesn't change {path}."
    return None


def run_tests(root: Path) -> tuple[bool, str]:
    """Run the repo's tests on the patched copy, in a subprocess with no secrets in its environment.

    An empty environment keeps the keys out of the model's code by accident. It is not
    a sandbox: the code still runs as this process's user, with its network access.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=root,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"The tests took longer than {TEST_TIMEOUT_S} s."
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, "\n".join(output.splitlines()[-15:])


def patch_messages(analysis: dict, action: dict, files: dict[str, str]) -> list:
    listing = "\n\n".join(f"### {path}\n```python\n{text}\n```" for path, text in files.items())
    return [
        (
            "system",
            "You fix production incidents with the smallest safe code change. "
            "Replace exactly one of the files below. Return the whole new file, "
            "keep its comments up to date, and change nothing unrelated.",
        ),
        (
            "human",
            f"Likely cause: {analysis['likely_cause']}\n\n"
            f"Approved action for {action['service']}: {action['action']}\n\n"
            f"Files in {package_for(action['service'])}/:\n\n{listing}",
        ),
    ]


def propose_fix(build_patcher, incident: str, analysis: dict, action: dict, approval_issue: str) -> dict:
    """Patch, test, and open a PR. Returns {"status": "opened" | "blocked", ...}."""
    github = GitHub()
    package = package_for(action["service"])
    branch = f"{incident.lower()}-fix-{action['service']}"
    if pr := github.find_pr(branch):
        # Checked before the model runs, so a retry neither pays for a second patch nor opens a second PR.
        return {"status": "opened", "url": pr["html_url"], "branch": branch, "path": "", "output": ""}
    with tempfile.TemporaryDirectory() as tmp:
        root = github.download("main", Path(tmp))
        files = source_files(root, package)
        patch = build_patcher().invoke(patch_messages(analysis, action, files))
        path = patch["path"].strip().lstrip("./")
        if problem := check_patch(patch, files, package):
            return {"status": "blocked", "reason": problem, "output": ""}
        (root / path).write_text(patch["content"], encoding="utf-8")
        passed, output = run_tests(root)
        if not passed:
            return {"status": "blocked", "reason": "The repo's tests failed on the patch.", "output": output}

    refs = f"\nRefs {approval_issue}" if approval_issue else ""
    pr = github.open_pr(
        branch,
        path,
        patch["content"],
        title=f"{incident}: {patch['summary']}",
        body=(
            f"Fixes {incident}{refs}\n\n"
            f"**Approved action:** {action['action']}\n\n"
            f"**Likely cause:** {analysis['likely_cause']}\n\n"
            f"Written by the triage workflow's fixer. The repo's tests passed on this patch:\n\n"
            f"```\n{output}\n```\n\nReview it before you merge."
        ),
    )
    return {"status": "opened", "url": pr["html_url"], "branch": branch, "path": path, "output": output}
