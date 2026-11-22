#!/usr/bin/env python3
#
# Copyright (C) 2026 utzcoz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Execute maintainer control commands (`/cancel`, `/status`, `/help`) from issue comments."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_issue_trigger  # noqa: E402

API_BASE = "https://api.github.com"
USER_AGENT = "digitalis-prebuilts-issue-commands"
BUILD_WORKFLOW_FILE = "build-prebuilts.yml"
ACTIVE_RUN_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}
RUN_URL_RE = re.compile(r"/actions/runs/(\d+)")

HELP_TEXT = """Available prebuilt CI comment commands (maintainers only):

| Command | Description |
| --- | --- |
| `/build android17` | Build latest `android17` branch HEAD (workflow artifact only) |
| `/build android16` | Build latest `android16` branch HEAD |
| `/build all` | Build both `android17` and `android16` |
| `/build android17 release` (or `/release android17`) | Build and publish a GitHub Release (`<branch>-<YYYY-MM-DD>-<sha>`) |
| `/build all release` (or `/release all`) | Build both branches and publish one GitHub Release per branch |
| `/build android17 branch=<ref> release` | Build a custom branch, tag, or SHA in `platform_frameworks_libs_binary_translation` |
| `/build android17 release tag=<tag>` | Publish a single-branch release with a custom tag |
| `/build android17 pinned` | Build the commit pinned in `config/versions.json` instead of branch HEAD |
| `/rebuild` (or bare `/build`) | Re-run the build using this issue's target/branch/release settings |
| `/cancel` (aliases: `/stop`, `/abort`) | Immediately cancel active/queued build runs triggered by this issue |
| `/cancel <run_id>` | Cancel a specific workflow run ID |
| `/cancel all` | Cancel all active `Build Digitalis Prebuilts` runs in this repository |
| `/status` | Show active and recent build runs for this issue |
| `/help` | Show this command reference |
"""


class GitHubActionsClient:
    """Minimal stdlib GitHub REST client for issue-comment workflow control."""

    def __init__(self, repo, token, timeout=20):
        self.repo = repo.strip()
        self.token = token.strip()
        self.timeout = timeout

    def _request(self, method, path, body=None):
        url = f"{API_BASE}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))

    def list_workflow_runs(self, workflow_file=BUILD_WORKFLOW_FILE, per_page=100):
        data = self._request(
            "GET",
            f"/repos/{self.repo}/actions/workflows/{workflow_file}/runs?per_page={per_page}",
        )
        return list((data or {}).get("workflow_runs") or ())

    def list_issue_comments(self, issue_number, max_pages=3):
        comments = []
        for page in range(1, max_pages + 1):
            batch = self._request(
                "GET",
                f"/repos/{self.repo}/issues/{issue_number}/comments?per_page=100&page={page}",
            )
            if not isinstance(batch, list) or not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
        return comments

    def cancel_workflow_run(self, run_id):
        try:
            self._request("POST", f"/repos/{self.repo}/actions/runs/{run_id}/cancel")
            return True, "cancellation requested"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code == 409:
                return False, "already completed or cancelling"
            return False, f"HTTP {exc.code}: {detail[:200]}"

    def post_issue_comment(self, issue_number, body):
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            body={"body": body},
        )


def extract_run_ids_from_comments(comments):
    """Collect workflow run IDs referenced in comments on an issue."""
    ids = set()
    for comment in comments or ():
        for match in RUN_URL_RE.finditer(comment.get("body") or ""):
            ids.add(int(match.group(1)))
    return ids


def run_matches_issue(run, issue_number, comment_run_ids=()):
    """Return True if a workflow run was triggered by or linked to `issue_number`."""
    run_id = run.get("id")
    if run_id is not None and int(run_id) in comment_run_ids:
        return True
    pattern = re.compile(rf"\(issue\s+#{int(issue_number)}\b", re.IGNORECASE)
    for field in ("display_title", "name"):
        if pattern.search(run.get(field) or ""):
            return True
    return False


def select_runs_to_cancel(runs, *, issue_number, comment_run_ids=(), cancel_all=False, explicit_ids=()):
    """Select active workflow runs targeted by a `/cancel` command."""
    explicit_set = {int(x) for x in (explicit_ids or ())}
    selected = []
    seen = set()
    for run in runs or ():
        run_id = run.get("id")
        if run_id is None:
            continue
        run_id = int(run_id)
        if run_id in seen:
            continue
        status = (run.get("status") or "").lower()
        if status not in ACTIVE_RUN_STATUSES:
            continue
        if explicit_set:
            if run_id in explicit_set:
                selected.append(run)
                seen.add(run_id)
        elif cancel_all or run_matches_issue(run, issue_number, comment_run_ids):
            selected.append(run)
            seen.add(run_id)
    return selected


def format_cancel_reply(results, *, cancel_all=False, explicit_ids=()):
    """Render the Markdown confirmation comment for a `/cancel` command."""
    if not results:
        if explicit_ids:
            ids_str = ", ".join(f"`{rid}`" for rid in explicit_ids)
            return f"No active `Build Digitalis Prebuilts` run matched ID(s): {ids_str}."
        scope = "in this repository" if cancel_all else "for this issue"
        return f"No active `Build Digitalis Prebuilts` runs found {scope}."

    lines = ["Cancellation requested for active prebuilt build run(s):"]
    for run, ok, detail in results:
        rid = run["id"]
        url = run.get("html_url") or ""
        title = run.get("display_title") or run.get("name") or "Build Digitalis Prebuilts"
        prev_status = run.get("status") or "active"
        link = f"[#{rid}]({url})" if url else f"`#{rid}`"
        outcome = "cancelling" if ok else f"skipped ({detail})"
        lines.append(f"- {link} (`{title}`, was `{prev_status}` → {outcome})")
    return "\n".join(lines) + "\n"


def format_status_reply(runs, *, issue_number, comment_run_ids=(), max_recent=5):
    """Render the Markdown status summary for `/status`."""
    matched = [r for r in (runs or ()) if run_matches_issue(r, issue_number, comment_run_ids)]
    if not matched:
        return "No `Build Digitalis Prebuilts` runs found for this issue yet.\n"

    active = [r for r in matched if (r.get("status") or "").lower() in ACTIVE_RUN_STATUSES]
    completed = [r for r in matched if (r.get("status") or "").lower() not in ACTIVE_RUN_STATUSES]

    lines = []
    if active:
        lines.append(f"**Active build run(s) for issue #{issue_number}:**")
        for run in active:
            rid = run["id"]
            url = run.get("html_url") or ""
            title = run.get("display_title") or run.get("name") or "Build"
            status = run.get("status") or "active"
            link = f"[#{rid}]({url})" if url else f"`#{rid}`"
            lines.append(f"- {link} (`{title}`) — `{status}`")
    else:
        lines.append(f"No active build runs for issue #{issue_number}.")

    if completed:
        lines.append("")
        lines.append("**Recent completed run(s):**")
        for run in completed[:max_recent]:
            rid = run["id"]
            url = run.get("html_url") or ""
            title = run.get("display_title") or run.get("name") or "Build"
            conclusion = run.get("conclusion") or run.get("status") or "completed"
            link = f"[#{rid}]({url})" if url else f"`#{rid}`"
            lines.append(f"- {link} (`{title}`) — `{conclusion}`")

    return "\n".join(lines) + "\n"


def execute_command(cmd, *, issue_number, client):
    """Execute a parsed control command and post a reply on `issue_number`."""
    action = cmd.get("action")
    if action == "none":
        return None

    if action == "help":
        client.post_issue_comment(issue_number, HELP_TEXT)
        return HELP_TEXT

    runs = client.list_workflow_runs()
    comment_run_ids = set()
    if not cmd.get("cancel_all") and not cmd.get("run_ids"):
        comment_run_ids = extract_run_ids_from_comments(client.list_issue_comments(issue_number))

    if action == "status":
        reply = format_status_reply(runs, issue_number=issue_number, comment_run_ids=comment_run_ids)
        client.post_issue_comment(issue_number, reply)
        return reply

    if action == "cancel":
        targets = select_runs_to_cancel(
            runs,
            issue_number=issue_number,
            comment_run_ids=comment_run_ids,
            cancel_all=bool(cmd.get("cancel_all")),
            explicit_ids=cmd.get("run_ids") or (),
        )
        results = []
        for run in targets:
            ok, detail = client.cancel_workflow_run(run["id"])
            results.append((run, ok, detail))
        reply = format_cancel_reply(
            results,
            cancel_all=bool(cmd.get("cancel_all")),
            explicit_ids=cmd.get("run_ids") or (),
        )
        client.post_issue_comment(issue_number, reply)
        return reply

    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event-path", type=Path, required=True, help="GitHub GITHUB_EVENT_PATH JSON")
    ap.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub owner/repo (default: $GITHUB_REPOSITORY)",
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
        help="GitHub API token (default: $GH_TOKEN or $GITHUB_TOKEN)",
    )
    args = ap.parse_args(argv)

    payload = json.loads(args.event_path.read_text(encoding="utf-8"))
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    issue_number = issue.get("number")
    if not issue_number:
        print("skipping: event has no issue number")
        return 0

    cmd = parse_issue_trigger.parse_control_command(
        author_association=comment.get("author_association", ""),
        comment_body=comment.get("body") or "",
    )
    print(f"parsed control command: {json.dumps(cmd)}")
    if cmd["action"] == "none":
        return 0

    if not args.repo:
        raise SystemExit("missing --repo / GITHUB_REPOSITORY")
    client = GitHubActionsClient(repo=args.repo, token=args.token)
    reply = execute_command(cmd, issue_number=int(issue_number), client=client)
    if reply:
        print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
