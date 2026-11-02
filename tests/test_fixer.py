"""The fix PR, checked offline: a fake GitHub, a fake patcher, and the repo's real test gate."""
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

import app
import fixer
import github_api
from test_linear import CONFIG, resume_all, start
from test_receiver import incident

# A two-file copy of triage-services: the planted bug and its gate.
REPO = {
    "checkout_api/__init__.py": "",
    "checkout_api/config.py": "maximum_pool_size = 5\n",
    "tests/test_peak_load.py": (
        "from checkout_api import config\n\n"
        "def test_pool_serves_peak_checkout_traffic():\n"
        "    assert config.maximum_pool_size >= 41\n\n"
        "def test_pool_stays_inside_the_orders_db_limit():\n"
        "    assert config.maximum_pool_size <= 200\n"
    ),
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["."]\n',
}


class FakeGitHub:
    def __init__(self):
        self.prs = {}
        self.opened = []

    def download(self, ref, into):
        root = into / "learner-triage-services-abc123"
        for path, text in REPO.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        return root

    def find_pr(self, branch):
        return self.prs.get(branch)

    def open_pr(self, branch, path, content, title, body):
        pr = {"html_url": f"https://github.com/learner/triage-services/pull/{len(self.opened) + 1}"}
        self.opened.append({"branch": branch, "path": path, "content": content, "title": title, "body": body})
        self.prs[branch] = pr
        return pr


@pytest.fixture
def github(monkeypatch, fakes):
    hub = FakeGitHub()
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("GITHUB_REPO", "learner/triage-services")
    monkeypatch.setattr(fixer, "GitHub", lambda: hub)
    fakes["patch"] = {"path": "checkout_api/config.py", "content": "maximum_pool_size = 50\n",
                      "summary": "Restore the checkout-api pool to 50"}
    fakes["patcher"] = patcher = type("P", (), {"calls": 0})()

    def invoke(messages):
        patcher.calls += 1
        return fakes["patch"]

    patcher.invoke = invoke
    monkeypatch.setattr(app, "build_patcher", lambda: patcher)
    return hub


def approve_both():
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        start(graph)
        return resume_all(graph, "approve")


def test_approved_fix_opens_a_pr_linked_to_the_incident(github, fake_linear):
    out = approve_both()
    [pr] = github.opened
    assert pr["branch"] == "tri-5-fix-checkout-api"
    assert pr["body"].startswith("Fixes TRI-5\nRefs APR-1")
    assert pr["title"] == "TRI-5: Restore the checkout-api pool to 50"
    assert out["fix"]["status"] == "opened" and out["status"] == "fix_opened"
    comments = [c[2] for c in fake_linear.of("comment")]
    assert comments[-1].startswith("**Fix PR opened:** https://github.com/learner/triage-services/pull/1")
    # The PR ends the run, so no runbook is published.
    assert not any("**Runbook published**" in body for body in comments)
    # Linear's PR automation moves the incident from here, so the run doesn't move it to Done.
    assert [c[3] for c in fake_linear.of("move") if c[1] == "uuid-5"][-1] == "Awaiting Approval"


def test_a_patch_that_fails_the_tests_opens_no_pr(github, fakes, fake_linear):
    fakes["patch"] = {"path": "checkout_api/config.py", "content": "maximum_pool_size = 6\n", "summary": "x"}
    out = approve_both()
    assert github.opened == []
    assert out["fix"]["reason"] == "The repo's tests failed on the patch."
    assert out["status"] == "fix_blocked"
    blocked = [c[2] for c in fake_linear.of("comment") if c[2].startswith("**Fix blocked:**")]
    assert "test_pool_serves_peak_checkout_traffic" in blocked[0]
    assert [c[3] for c in fake_linear.of("move") if c[1] == "uuid-5"][-1] == "Triage"


def test_a_patch_to_the_tests_is_refused_before_they_run(github, fakes, fake_linear):
    fakes["patch"] = {"path": "tests/test_peak_load.py", "content": "", "summary": "x"}
    out = approve_both()
    assert github.opened == []
    assert out["fix"]["reason"] == "tests/test_peak_load.py is not an existing file in checkout_api/."


def test_a_retry_reuses_the_open_pr_without_calling_the_model(github, fakes, fake_linear):
    github.prs["tri-5-fix-checkout-api"] = {"html_url": "https://github.com/learner/triage-services/pull/7"}
    out = approve_both()
    assert fakes["patcher"].calls == 0
    assert github.opened == []
    assert out["fix"]["url"].endswith("/pull/7")


def test_a_rejected_fix_never_reaches_github(github, fakes, fake_linear):
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        graph = app.build_graph(cp)
        start(graph)
        resume_all(graph, "reject")
    assert fakes["patcher"].calls == 0


def test_without_github_settings_the_fix_is_skipped(fakes, fake_linear, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = approve_both()
    assert "fix" not in out
    assert fake_linear.of("move")[-1][3] == "Done"


def test_the_real_gate_fails_at_5_and_passes_at_50(tmp_path):
    hub = FakeGitHub()
    root = hub.download("main", tmp_path)
    passed, output = fixer.run_tests(root)
    assert not passed and "1 failed, 1 passed" in output
    (root / "checkout_api/config.py").write_text("maximum_pool_size = 50\n")
    passed, output = fixer.run_tests(root)
    assert passed and "2 passed" in output


def test_the_tests_run_without_the_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secret")
    (tmp_path / "test_env.py").write_text(
        "import os\n\ndef test_no_secrets():\n    assert 'GITHUB_TOKEN' not in os.environ\n"
    )
    assert fixer.run_tests(tmp_path)[0]


def test_a_stopped_run_says_so_on_the_incident(fakes, fake_linear):
    fakes["fail"] = {"metrics"}
    with SqliteSaver.from_conn_string(app.DB_PATH) as cp:
        app.run_graph(app.build_graph(cp), "TRI-5", "start", {
            "incident_id": "TRI-5", "issue_id": "uuid-5", "incident": "x",
            "alert": app.load_json("alerts.json")[0], "status": "new", "audit_log": [],
        })
    [comment] = fake_linear.of("comment")
    assert comment[2].startswith("**Run stopped:** `")
    assert "remove the `agent-triage` label and add it again" in comment[2]


def test_a_linear_run_posts_the_runbook_instead_of_writing_a_file(fakes, fake_linear, tmp_path):
    out = approve_both()
    assert out["runbook_path"] == "Linear issue TRI-5"
    assert not (tmp_path / "runbooks").exists()
    assert "Likely cause: d-1042 cut maximumPoolSize" in fake_linear.of("comment")[-1][2]


def test_open_pr_makes_the_documented_rest_calls():
    """The real GitHub client against a mock transport: branch, blob sha, file, then the PR."""
    import httpx

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.url.params.get("ref", "")))
        path = request.url.path.removeprefix("/repos/learner/triage-services")
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "main-sha"}})
        if path == "/git/refs":
            assert request.read() == b'{"ref":"refs/heads/tri-5-fix-checkout-api","sha":"main-sha"}'
            return httpx.Response(201, json={})
        if path == "/contents/checkout_api/config.py" and request.method == "GET":
            return httpx.Response(200, json={"sha": "blob-sha"})
        if path == "/contents/checkout_api/config.py":
            assert b'"sha":"blob-sha"' in request.read()
            return httpx.Response(200, json={})
        if path == "/pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/learner/triage-services/pull/1"})
        return httpx.Response(404)

    hub = github_api.GitHub(token="github_pat_test", repo="learner/triage-services")
    hub.http._transport = httpx.MockTransport(handler)
    pr = hub.open_pr("tri-5-fix-checkout-api", "checkout_api/config.py", "maximum_pool_size = 50\n", "t", "Fixes TRI-5")
    assert pr["html_url"].endswith("/pull/1")
    assert [(m, p.rsplit("/", 2)[-1]) for m, p, _ in seen] == [
        ("GET", "main"), ("POST", "refs"), ("GET", "config.py"), ("PUT", "config.py"), ("POST", "pulls"),
    ]
    assert seen[2][2] == "tri-5-fix-checkout-api"  # the blob sha is read from the new branch
