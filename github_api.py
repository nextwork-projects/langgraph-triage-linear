"""GitHub's REST API: the calls the fixer makes to open a pull request.

Like linear.py, it holds no workflow logic. A fine-grained PAT for one repo, with
Contents and Pull requests set to read and write, covers every call here.
"""
import base64
import io
import os
import tarfile
from pathlib import Path

import httpx

API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    """The five REST calls that open a PR, plus the lookups that keep a retry from repeating them."""

    def __init__(self, token: str | None = None, repo: str | None = None):
        self.repo = repo or os.environ["GITHUB_REPO"]
        self.http = httpx.Client(
            base_url=f"{API_URL}/repos/{self.repo}",
            headers={
                "Authorization": f"Bearer {token or os.environ['GITHUB_TOKEN']}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
            follow_redirects=True,
        )

    def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self.http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path}: HTTP {response.status_code} {response.text[:200]}")
        return response

    def download(self, ref: str, into: Path) -> Path:
        """Unpack the repo at ref, and return its root folder."""
        tarball = self.call("GET", f"/tarball/{ref}").content
        with tarfile.open(fileobj=io.BytesIO(tarball)) as archive:
            archive.extractall(into, filter="data")  # refuses absolute paths and links out of the folder
        [root] = [child for child in into.iterdir() if child.is_dir()]
        return root

    def find_pr(self, branch: str) -> dict | None:
        """The PR from a branch, open or closed. A retried run finds the one it already opened."""
        pulls = self.call("GET", "/pulls", params={"head": f"{self.repo.split('/')[0]}:{branch}", "state": "all"}).json()
        return pulls[0] if pulls else None

    def open_pr(self, branch: str, path: str, content: str, title: str, body: str) -> dict:
        main_sha = self.call("GET", "/git/ref/heads/main").json()["object"]["sha"]
        response = self.http.post("/git/refs", json={"ref": f"refs/heads/{branch}", "sha": main_sha})
        if response.status_code not in (201, 422):  # 422: the branch exists from a run that stopped part-way
            raise GitHubError(f"GitHub POST /git/refs: HTTP {response.status_code} {response.text[:200]}")
        blob_sha = self.call("GET", f"/contents/{path}", params={"ref": branch}).json()["sha"]
        self.call("PUT", f"/contents/{path}", json={
            "message": title,
            "content": base64.b64encode(content.encode()).decode(),
            "sha": blob_sha,
            "branch": branch,
        })
        return self.call("POST", "/pulls", json={"title": title, "head": branch, "base": "main", "body": body}).json()
