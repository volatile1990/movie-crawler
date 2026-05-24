from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import urlparse

import requests

from constants import RETRYABLE_HTTP_STATUSES
from runtime import sleep_before_retry, stop_requested


class GithubRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GithubPublisher:
    def __init__(self, options: dict[str, Any]) -> None:
        self.token = str(options.get("github_token") or "").strip()
        self.owner, self.repo = parse_github_repo(str(options["github_repo"]))
        self.branch = str(options.get("github_branch") or "").strip()
        self.timeout = int(options["request_timeout_seconds"])
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    def publish(self, files: dict[str, bytes], message: str) -> dict[str, Any]:
        if not self.token:
            return {"published": False, "reason": "github_token is empty"}

        for attempt in range(2):
            try:
                return self.publish_once(files, message)
            except GithubRequestError as exc:
                if exc.status_code == 409 and attempt == 0:
                    logging.warning("GitHub branch changed while publishing; retrying with latest head.")
                    continue
                raise
        return {"published": False, "reason": "publish retry was exhausted"}

    def publish_once(self, files: dict[str, bytes], message: str) -> dict[str, Any]:
        branch = self.branch or self.default_branch()
        ref = self.request("GET", f"{self.api_base}/git/ref/heads/{branch}")
        head_sha = ref["object"]["sha"]
        commit = self.request("GET", f"{self.api_base}/git/commits/{head_sha}")
        base_tree_sha = commit["tree"]["sha"]

        tree_entries = []
        for path, content in sorted(files.items()):
            blob = self.request(
                "POST",
                f"{self.api_base}/git/blobs",
                {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = self.request(
            "POST",
            f"{self.api_base}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        new_commit = self.request(
            "POST",
            f"{self.api_base}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [head_sha]},
        )
        self.request(
            "PATCH",
            f"{self.api_base}/git/refs/heads/{branch}",
            {"sha": new_commit["sha"], "force": False},
        )
        return {"published": True, "commit_sha": new_commit["sha"], "branch": branch}

    def default_branch(self) -> str:
        repo = self.request("GET", self.api_base)
        return repo.get("default_branch") or "main"

    def request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for attempt in range(3):
            if stop_requested():
                raise GithubRequestError("GitHub request cancelled because shutdown was requested")
            try:
                response = requests.request(method, url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < 2:
                    logging.warning("GitHub request failed, retrying: %s", exc)
                    sleep_before_retry(attempt)
                    continue
                raise GithubRequestError(f"GitHub request failed: {exc}") from exc

            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < 2:
                logging.warning("GitHub returned HTTP %s, retrying.", response.status_code)
                sleep_before_retry(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                raise GithubRequestError(
                    f"GitHub returned HTTP {response.status_code}: {response.text[:500]}",
                    response.status_code,
                )
            if not response.content:
                return {}
            try:
                data = response.json()
            except ValueError as exc:
                raise GithubRequestError(f"GitHub returned invalid JSON: {exc}") from exc
            return data if isinstance(data, dict) else {}
        raise GithubRequestError("GitHub request retry was exhausted")


def parse_github_repo(value: str) -> tuple[str, str]:
    repo = value.strip()
    if repo.startswith("http://") or repo.startswith("https://"):
        path = urlparse(repo).path.strip("/")
    else:
        path = repo
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Invalid github_repo value: {value}")
    return parts[-2], parts[-1]
