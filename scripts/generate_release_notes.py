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
"""Generate concise GitHub Release notes comparing against the prior release of the same branch."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.github.com"
USER_AGENT = "digitalis-prebuilts-release-notes"

BRANCH_LINE_RE = re.compile(r"^\s*-\s*\*\*Branch:\*\*\s*`([^`]+)`", re.IGNORECASE | re.MULTILINE)
BODY_SHA_PATTERNS = {
    "binary_translation": re.compile(
        r"^\s*-\s*\*\*`binary_translation`:\*\*\s*`([0-9a-f]{7,40})`",
        re.IGNORECASE | re.MULTILINE,
    ),
    "digitalis": re.compile(
        r"^\s*-\s*\*\*`digitalis`:\*\*\s*`([0-9a-f]{7,40})`",
        re.IGNORECASE | re.MULTILINE,
    ),
    "manifest": re.compile(
        r"^\s*-\s*\*\*`manifest`:\*\*\s*`([0-9a-f]{7,40})`",
        re.IGNORECASE | re.MULTILINE,
    ),
}
TAG_BRANCH_RE = re.compile(r"^(.+)-\d{4}-\d{2}-\d{2}-[0-9a-f]{7,40}$")
GITHUB_URL_RE = re.compile(r"^https?://github\.com/([^/]+/[^/#?]+?)(?:\.git)?/?$")

TRACKED_REPOS = (
    (
        "binary_translation",
        "frameworks/libs/binary_translation",
        "https://github.com/DigitalisX64/platform_frameworks_libs_binary_translation",
    ),
    (
        "digitalis",
        "digitalis",
        "https://github.com/DigitalisX64/digitalis",
    ),
    (
        "manifest",
        "manifest",
        "https://github.com/DigitalisX64/manifest",
    ),
)


class _NoCrossHostAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization when GitHub redirects API asset downloads to blob storage."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        old_host = urllib.parse.urlparse(req.full_url).netloc.lower()
        new_host = urllib.parse.urlparse(newurl).netloc.lower()
        if old_host != new_host:
            for hdr in ("Authorization", "authorization"):
                new_req.headers.pop(hdr, None)
                new_req.unredirected_hdrs.pop(hdr, None)
        return new_req


class GitHubClient:
    """Minimal stdlib GitHub REST client for release discovery and commit diffs."""

    def __init__(self, repo="", token="", timeout=20):
        self.repo = repo.strip()
        self.token = token.strip()
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoCrossHostAuthRedirectHandler())

    def _request(self, url, accept="application/vnd.github+json", use_auth=True):
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if use_auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def _get_json(self, url, allow_unauth_fallback=False):
        try:
            data = self._request(url, use_auth=bool(self.token))
            return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if allow_unauth_fallback and self.token and exc.code in (401, 403, 404):
                data = self._request(url, use_auth=False)
                return json.loads(data.decode("utf-8"))
            raise

    def list_releases(self, max_pages=5):
        if not self.repo:
            return []
        releases = []
        for page in range(1, max_pages + 1):
            url = f"{API_BASE}/repos/{self.repo}/releases?per_page=100&page={page}"
            try:
                batch = self._get_json(url, allow_unauth_fallback=False)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"::warning::failed to list releases for {self.repo}: {exc}",
                    file=sys.stderr,
                )
                break
            if not isinstance(batch, list) or not batch:
                break
            releases.extend(batch)
            if len(batch) < 100:
                break
        return releases

    def download_release_asset(self, release, asset_name):
        for asset in release.get("assets") or ():
            if asset.get("name") != asset_name:
                continue
            api_url = asset.get("url")
            browser_url = asset.get("browser_download_url")
            if api_url:
                try:
                    return self._request(
                        api_url,
                        accept="application/octet-stream",
                        use_auth=bool(self.token),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"::warning::failed to download asset {asset_name} via API: {exc}",
                        file=sys.stderr,
                    )
            if browser_url:
                try:
                    return self._request(
                        browser_url,
                        accept="application/octet-stream",
                        use_auth=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"::warning::failed to download asset {asset_name}: {exc}",
                        file=sys.stderr,
                    )
        return None

    def compare_commits(self, slug, base, head):
        base_q = urllib.parse.quote(base, safe="")
        head_q = urllib.parse.quote(head, safe="")
        url = f"{API_BASE}/repos/{slug}/compare/{base_q}...{head_q}"
        try:
            return self._get_json(url, allow_unauth_fallback=(slug.lower() != self.repo.lower()))
        except Exception as exc:  # noqa: BLE001
            print(
                f"::warning::failed to compare {slug} {base[:8]}...{head[:8]}: {exc}",
                file=sys.stderr,
            )
            return None

    def get_commit(self, slug, ref):
        ref_q = urllib.parse.quote(ref, safe="")
        url = f"{API_BASE}/repos/{slug}/commits/{ref_q}"
        try:
            return self._get_json(url, allow_unauth_fallback=(slug.lower() != self.repo.lower()))
        except Exception as exc:  # noqa: BLE001
            print(
                f"::warning::failed to fetch commit {slug}@{ref[:8]}: {exc}",
                file=sys.stderr,
            )
            return None


def parse_manifest(text):
    """Parse key: value fields from MANIFEST.txt."""
    result = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def github_slug_from_url(url):
    match = GITHUB_URL_RE.match((url or "").strip())
    return match.group(1) if match else None


def extract_branch_from_release(release):
    """Identify the Digitalis branch label owned by a GitHub Release."""
    body = release.get("body") or ""
    match = BRANCH_LINE_RE.search(body)
    if match:
        return match.group(1).strip()
    tag = (release.get("tag_name") or "").strip()
    tag_match = TAG_BRANCH_RE.match(tag)
    if tag_match:
        return tag_match.group(1)
    return None


def find_previous_release(releases, branch, current_tag):
    """Return the most recent non-draft release for `branch` other than `current_tag`."""
    candidates = []
    for idx, rel in enumerate(releases or ()):
        if rel.get("draft"):
            continue
        tag = (rel.get("tag_name") or "").strip()
        if not tag or tag == current_tag:
            continue
        if extract_branch_from_release(rel) != branch:
            continue
        ts = rel.get("published_at") or rel.get("created_at") or ""
        candidates.append((ts, -idx, rel))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _normalize_commit(value):
    val = (value or "").strip().lower()
    if not val or val == "unknown":
        return ""
    return val


def commits_equal(base, head):
    """Compare two full or abbreviated hex commit SHAs for equality."""
    a = _normalize_commit(base)
    b = _normalize_commit(head)
    if not a or not b:
        return a == b
    if a == b:
        return True
    if min(len(a), len(b)) >= 7:
        return a.startswith(b) or b.startswith(a)
    return False


def extract_revisions(sources=None, manifest=None, release_body=""):
    """Build a normalized snapshot of repo commits and pinned AOSP metadata."""
    sources = sources if isinstance(sources, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    projects = sources.get("projects") if isinstance(sources.get("projects"), dict) else {}

    repos = {}
    for key, proj_path, default_url in TRACKED_REPOS:
        commit = ""
        url = default_url
        if key == "manifest":
            man = sources.get("manifest")
            if isinstance(man, dict):
                commit = _normalize_commit(man.get("commit"))
                url = man.get("url") or url
        else:
            proj = projects.get(proj_path)
            if isinstance(proj, dict):
                commit = _normalize_commit(proj.get("commit"))
                url = proj.get("url") or url

        if not commit:
            commit = _normalize_commit(manifest.get(f"{key}-sha"))
        if not commit and release_body:
            match = BODY_SHA_PATTERNS[key].search(release_body)
            if match:
                commit = _normalize_commit(match.group(1))

        repos[key] = {
            "label": proj_path,
            "url": url,
            "commit": commit,
        }

    images = {}
    raw_images = sources.get("aosp_images")
    if isinstance(raw_images, dict):
        for arch in ("x86_64", "arm64"):
            entry = raw_images.get(arch)
            if isinstance(entry, dict) and entry.get("build_id"):
                images[arch] = str(entry["build_id"])

    superproject = ""
    raw_sp = sources.get("superproject")
    if isinstance(raw_sp, dict):
        superproject = _normalize_commit(raw_sp.get("commit"))

    clang = list(sources.get("clang") or ())
    rust = list(sources.get("rust") or ())

    return {
        "repos": repos,
        "aosp_images": images,
        "superproject": superproject,
        "clang": clang,
        "rust": rust,
    }


def format_commit_subject(message, max_len=120):
    """Extract a concise single-line commit subject suitable for a Markdown bullet."""
    for raw_line in (message or "").splitlines():
        line = " ".join(raw_line.split())
        if line:
            if len(line) > max_len:
                return line[: max_len - 1].rstrip() + "…"
            return line
    return "(no commit message)"


def summarize_repo_diff(label, repo_url, base_commit, head_commit, compare_fn=None, max_commits=15):
    """Format short Markdown diff lines for one repository between `base_commit` and `head_commit`."""
    if not base_commit or not head_commit or commits_equal(base_commit, head_commit):
        return []

    base_short = base_commit[:8]
    head_short = head_commit[:8]
    slug = github_slug_from_url(repo_url)
    compare_url = (
        f"https://github.com/{slug}/compare/{base_short}...{head_short}" if slug else ""
    )

    if compare_url:
        heading = f"### `{label}` ([`{base_short}...{head_short}`]({compare_url}))"
    else:
        heading = f"### `{label}` (`{base_short}...{head_short}`)"

    compare_data = compare_fn(slug, base_commit, head_commit) if (slug and compare_fn) else None
    lines = [heading]

    if not isinstance(compare_data, dict):
        if compare_url:
            lines.append(
                f"- Updated `{base_short}` → `{head_short}` ([compare]({compare_url}))"
            )
        else:
            lines.append(f"- Updated `{base_short}` → `{head_short}`")
        return lines

    commits = list(reversed(compare_data.get("commits") or ()))
    total_commits = compare_data.get("total_commits")
    if not isinstance(total_commits, int) or total_commits < len(commits):
        total_commits = len(commits)

    if commits:
        shown = commits[:max_commits]
        for entry in shown:
            sha = (entry.get("sha") or "")[:8]
            msg = (entry.get("commit") or {}).get("message") or ""
            subject = format_commit_subject(msg)
            html_url = entry.get("html_url") or (
                f"https://github.com/{slug}/commit/{entry.get('sha')}"
                if slug and entry.get("sha")
                else ""
            )
            if sha and html_url:
                lines.append(f"- [`{sha}`]({html_url}) {subject}")
            elif sha:
                lines.append(f"- `{sha}` {subject}")
            else:
                lines.append(f"- {subject}")
        remaining = total_commits - len(shown)
        if remaining > 0:
            suffix = f" ([full diff]({compare_url}))" if compare_url else ""
            lines.append(f"- … and {remaining} more commit(s){suffix}")
    else:
        status = compare_data.get("status") or ""
        if status in ("behind", "diverged"):
            note = "non-fast-forward"
        else:
            note = "no new commits on compare range"
        if compare_url:
            lines.append(
                f"- Updated `{base_short}` → `{head_short}` ({note}; [compare]({compare_url}))"
            )
        else:
            lines.append(f"- Updated `{base_short}` → `{head_short}` ({note})")

    return lines


def _format_aosp_build_label(images):
    vals = [v for v in (images.get("x86_64"), images.get("arm64")) if v]
    if not vals:
        return ""
    unique = list(dict.fromkeys(vals))
    if len(unique) == 1:
        return unique[0]
    return f"x86_64={images.get('x86_64', '?')}, arm64={images.get('arm64', '?')}"


def summarize_platform_changes(prev_rev, curr_rev):
    """Format concise bullets for AOSP image, superproject, or toolchain updates."""
    bullets = []
    prev_img = _format_aosp_build_label(prev_rev.get("aosp_images") or {})
    curr_img = _format_aosp_build_label(curr_rev.get("aosp_images") or {})
    if prev_img and curr_img and prev_img != curr_img:
        bullets.append(f"- AOSP pinned images: `{prev_img}` → `{curr_img}`")

    prev_sp = prev_rev.get("superproject") or ""
    curr_sp = curr_rev.get("superproject") or ""
    if prev_sp and curr_sp and not commits_equal(prev_sp, curr_sp):
        bullets.append(f"- AOSP superproject: `{prev_sp[:8]}` → `{curr_sp[:8]}`")

    for tool_name, key in (("Clang", "clang"), ("Rust", "rust")):
        prev_vals = prev_rev.get(key) or []
        curr_vals = curr_rev.get(key) or []
        if prev_vals and curr_vals and prev_vals != curr_vals:
            bullets.append(
                f"- {tool_name} toolchain: `{', '.join(prev_vals)}` → `{', '.join(curr_vals)}`"
            )

    return bullets


def render_release_notes(
    *,
    tag,
    tarball_name,
    manifest,
    curr_sources=None,
    prev_release=None,
    prev_sources=None,
    prev_manifest=None,
    compare_fn=None,
    commit_info_fn=None,
    max_commits=15,
):
    """Render concise Markdown release notes for a Digitalis prebuilt release."""
    branch = manifest.get("branch") or manifest.get("aosp-release") or manifest.get("android-release") or "unknown"
    aosp = manifest.get("aosp-release") or manifest.get("android-release") or branch
    build_date = manifest.get("build-date") or "unknown"
    bt_sha = manifest.get("binary_translation-sha") or "unknown"
    dig_sha = manifest.get("digitalis-sha") or "unknown"
    manifest_sha = manifest.get("manifest-sha") or "unknown"
    count = manifest.get("artifact-count") or "unknown"

    curr_rev = extract_revisions(sources=curr_sources, manifest=manifest)
    lines = [
        f"# Digitalis Prebuilts ({tag})",
        "",
        (
            f"Binary-only ARM64-to-x86_64 Digitalis translator bundle for **{aosp}**, "
            "built via unbundled AOSP Soong CI and verified with host tests."
        ),
        "",
    ]

    if prev_release is None:
        lines.append("## Changes")
        lines.append("")
        bt_info = curr_rev["repos"]["binary_translation"]
        bt_commit = bt_info["commit"]
        bt_slug = github_slug_from_url(bt_info["url"])
        tip_detail = ""
        if bt_commit and bt_slug and commit_info_fn:
            commit_data = commit_info_fn(bt_slug, bt_commit)
            if isinstance(commit_data, dict):
                msg = (commit_data.get("commit") or {}).get("message") or ""
                if msg.strip():
                    tip_detail = (
                        f" (`binary_translation` @ `{bt_commit[:8]}`: "
                        f"{format_commit_subject(msg)})"
                    )
        lines.append(f"- Initial prebuilt release for `{branch}`{tip_detail}.")
    else:
        prev_tag = prev_release.get("tag_name") or "previous"
        prev_url = prev_release.get("html_url") or ""
        if prev_url:
            lines.append(f"## Changes since [`{prev_tag}`]({prev_url})")
        else:
            lines.append(f"## Changes since `{prev_tag}`")
        lines.append("")

        prev_rev = extract_revisions(
            sources=prev_sources,
            manifest=prev_manifest,
            release_body=prev_release.get("body") or "",
        )

        sections = []
        for key, _proj_path, _default_url in TRACKED_REPOS:
            prev_repo = prev_rev["repos"][key]
            curr_repo = curr_rev["repos"][key]
            section = summarize_repo_diff(
                label=curr_repo["label"],
                repo_url=curr_repo["url"],
                base_commit=prev_repo["commit"],
                head_commit=curr_repo["commit"],
                compare_fn=compare_fn,
                max_commits=max_commits,
            )
            if section:
                sections.append(section)

        platform_bullets = summarize_platform_changes(prev_rev, curr_rev)
        if platform_bullets:
            if sections:
                sections.append(["### AOSP & Toolchain", *platform_bullets])
            else:
                sections.append(platform_bullets)

        if sections:
            for idx, section in enumerate(sections):
                if idx > 0:
                    lines.append("")
                lines.extend(section)
        else:
            lines.append("- Rebuild with the same pinned source commits and AOSP inputs.")

    aosp_build = _format_aosp_build_label(curr_rev.get("aosp_images") or {})
    lines.extend(
        [
            "",
            "## Build Details",
            "",
            f"- **Branch:** `{branch}`",
            f"- **Build date:** `{build_date}`",
            f"- **Archive:** `{tarball_name}`",
            f"- **`binary_translation`:** `{bt_sha}`",
            f"- **`digitalis`:** `{dig_sha}`",
            f"- **`manifest`:** `{manifest_sha}`",
        ]
    )
    if aosp_build:
        lines.append(f"- **AOSP images:** `{aosp_build}`")
    lines.append(f"- **Artifacts:** {count}")
    lines.append("")
    return "\n".join(lines)


def _format_size(num_bytes):
    if not isinstance(num_bytes, int) or num_bytes < 0:
        return ""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MiB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    return f"{num_bytes} B"


def build_release_summary(
    *,
    tag,
    tarball_name,
    manifest,
    curr_sources=None,
    tarball_path=None,
    sha256_path=None,
    server_url="https://github.com",
    repo="",
):
    """Build a compact metadata dict describing a published GitHub Release."""
    branch = (
        manifest.get("branch")
        or manifest.get("aosp-release")
        or manifest.get("android-release")
        or "unknown"
    )
    aosp = manifest.get("aosp-release") or manifest.get("android-release") or branch
    curr_rev = extract_revisions(sources=curr_sources, manifest=manifest)

    tarball_sha256 = ""
    if sha256_path and Path(sha256_path).is_file():
        first = Path(sha256_path).read_text(encoding="utf-8").strip().split()
        if first:
            tarball_sha256 = first[0]

    tarball_size = ""
    if tarball_path and Path(tarball_path).is_file():
        tarball_size = _format_size(Path(tarball_path).stat().st_size)

    base_repo_url = f"{server_url.rstrip('/')}/{repo.strip('/')}" if repo else ""
    tag_q = urllib.parse.quote(tag, safe="")
    tar_q = urllib.parse.quote(tarball_name, safe="")
    sha_q = urllib.parse.quote(f"{tarball_name}.sha256", safe="")

    return {
        "tag": tag,
        "branch": branch,
        "aosp": aosp,
        "build_date": manifest.get("build-date") or "unknown",
        "release_url": f"{base_repo_url}/releases/tag/{tag_q}" if base_repo_url else "",
        "tarball_name": tarball_name,
        "tarball_url": f"{base_repo_url}/releases/download/{tag_q}/{tar_q}" if base_repo_url else "",
        "sha256_url": f"{base_repo_url}/releases/download/{tag_q}/{sha_q}" if base_repo_url else "",
        "manifest_url": f"{base_repo_url}/releases/download/{tag_q}/MANIFEST.txt" if base_repo_url else "",
        "sources_url": f"{base_repo_url}/releases/download/{tag_q}/SOURCES.json" if base_repo_url else "",
        "tarball_sha256": tarball_sha256,
        "tarball_size": tarball_size,
        "bt_sha": manifest.get("binary_translation-sha") or "unknown",
        "digitalis_sha": manifest.get("digitalis-sha") or "unknown",
        "manifest_sha": manifest.get("manifest-sha") or "unknown",
        "aosp_images": _format_aosp_build_label(curr_rev.get("aosp_images") or {}),
        "artifact_count": manifest.get("artifact-count") or "unknown",
    }


def render_issue_completion_comment(
    *,
    build_result,
    release_result,
    run_url,
    releases=(),
    targets=(),
    bt_ref="",
    use_branch_head=True,
    build_date="",
    will_close=False,
):
    """Render the final status comment posted on the triggering issue before closing."""
    status_line = f"Prebuilt build finished: **build={build_result}**"
    if release_result and release_result != "skipped":
        status_line += f", **release={release_result}**"

    lines = [status_line, ""]
    if targets:
        lines.append(f"- **Targets:** `{', '.join(targets)}`")
    if build_date:
        lines.append(f"- **Build date:** `{build_date}`")
    if release_result == "skipped":
        ref_note = bt_ref or ("branch HEAD (latest)" if use_branch_head else "pinned in config/versions.json")
        lines.append(f"- **Translator ref:** `{ref_note}`")
    lines.append(f"- **Workflow run & artifacts:** {run_url}")

    if release_result == "success" and releases:
        lines.extend(["", "### Published Releases"])
        for rel in releases:
            tag = rel.get("tag") or "release"
            rel_url = rel.get("release_url") or ""
            heading = f"#### [`{tag}`]({rel_url})" if rel_url else f"#### `{tag}`"
            branch = rel.get("branch") or "unknown"
            aosp = rel.get("aosp") or branch
            aosp_images = rel.get("aosp_images") or ""
            branch_detail = f"`{branch}`"
            if aosp != branch:
                branch_detail += f" (AOSP `{aosp}`)"
            if aosp_images:
                branch_detail += f", AOSP images `{aosp_images}`"

            tar_name = rel.get("tarball_name") or ""
            tar_url = rel.get("tarball_url") or ""
            tar_size = rel.get("tarball_size") or ""
            size_suffix = f" ({tar_size})" if tar_size else ""
            tar_link = f"[`{tar_name}`]({tar_url})" if (tar_name and tar_url) else f"`{tar_name}`"

            sha256 = rel.get("tarball_sha256") or ""
            sha_url = rel.get("sha256_url") or ""
            manifest_url = rel.get("manifest_url") or ""
            sources_url = rel.get("sources_url") or ""

            lines.extend(
                [
                    "",
                    heading,
                    f"- **Branch:** {branch_detail}",
                    f"- **Archive:** {tar_link}{size_suffix}",
                ]
            )
            if sha256:
                sha_suffix = f" ([.sha256]({sha_url}))" if sha_url else ""
                lines.append(f"- **SHA-256:** `{sha256}`{sha_suffix}")
            elif sha_url:
                lines.append(f"- **Checksum:** [.sha256]({sha_url})")

            meta_links = []
            if manifest_url:
                meta_links.append(f"[MANIFEST.txt]({manifest_url})")
            if sources_url:
                meta_links.append(f"[SOURCES.json]({sources_url})")
            if meta_links:
                lines.append(f"- **Metadata:** {' · '.join(meta_links)}")

            lines.append(
                f"- **Revisions:** `binary_translation` `{rel.get('bt_sha', 'unknown')}` · "
                f"`digitalis` `{rel.get('digitalis_sha', 'unknown')}` · "
                f"`manifest` `{rel.get('manifest_sha', 'unknown')}`"
            )
            lines.append(f"- **Artifacts:** {rel.get('artifact_count', 'unknown')}")

    if will_close:
        lines.extend(["", "Closing this issue automatically as completed."])
    lines.append("")
    return "\n".join(lines)


def _main_issue_comment(argv):
    ap = argparse.ArgumentParser(description="Render final issue status comment.")
    ap.add_argument("--build-result", required=True)
    ap.add_argument("--release-result", default="skipped")
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--releases-json", default="[]")
    ap.add_argument("--targets-json", default="[]")
    ap.add_argument("--bt-ref", default="")
    ap.add_argument("--use-branch-head", default="true")
    ap.add_argument("--build-date", default="")
    ap.add_argument("--will-close", action="store_true")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)

    try:
        releases = json.loads(args.releases_json or "[]")
    except json.JSONDecodeError:
        releases = []
    if not isinstance(releases, list):
        releases = []

    try:
        targets = json.loads(args.targets_json or "[]")
    except json.JSONDecodeError:
        targets = []
    if not isinstance(targets, list):
        targets = []

    comment = render_issue_completion_comment(
        build_result=args.build_result,
        release_result=args.release_result,
        run_url=args.run_url,
        releases=releases,
        targets=targets,
        bt_ref=args.bt_ref,
        use_branch_head=(args.use_branch_head.lower() == "true"),
        build_date=args.build_date,
        will_close=args.will_close,
    )
    if args.output:
        args.output.write_text(comment, encoding="utf-8")
    else:
        sys.stdout.write(comment)
    return 0


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "issue-comment":
        return _main_issue_comment(raw_argv[1:])

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", required=True, help="release tag being published")
    ap.add_argument("--tarball-name", required=True, help="prebuilt .tar.gz filename")
    ap.add_argument("--manifest", type=Path, required=True, help="path to MANIFEST.txt")
    ap.add_argument("--sources", type=Path, help="path to SOURCES.json")
    ap.add_argument("--tarball", type=Path, help="optional path to .tar.gz for size reporting")
    ap.add_argument("--sha256-file", type=Path, help="optional path to .tar.gz.sha256")
    ap.add_argument(
        "--server-url",
        default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        help="GitHub server base URL (default: $GITHUB_SERVER_URL)",
    )
    ap.add_argument(
        "--summary-json",
        type=Path,
        help="optional path to write compact release metadata JSON for issue reporting",
    )
    ap.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub owner/repo hosting prebuilt releases (default: $GITHUB_REPOSITORY)",
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
        help="GitHub API token (default: $GH_TOKEN or $GITHUB_TOKEN)",
    )
    ap.add_argument(
        "--max-commits",
        type=int,
        default=15,
        help="maximum commits listed per repository before truncating",
    )
    ap.add_argument("--output", type=Path, help="destination Markdown file (default: stdout)")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip GitHub API requests and emit standalone notes",
    )
    args = ap.parse_args(raw_argv)

    manifest = parse_manifest(args.manifest.read_text(encoding="utf-8"))
    curr_sources = None
    if args.sources and args.sources.is_file():
        curr_sources = json.loads(args.sources.read_text(encoding="utf-8"))

    branch = manifest.get("branch") or manifest.get("aosp-release") or manifest.get("android-release") or ""
    prev_release = None
    prev_sources = None
    prev_manifest = None
    compare_fn = None
    commit_info_fn = None

    if not args.offline:
        client = GitHubClient(repo=args.repo, token=args.token)
        compare_fn = client.compare_commits
        commit_info_fn = client.get_commit
        if args.repo and branch:
            releases = client.list_releases()
            prev_release = find_previous_release(releases, branch=branch, current_tag=args.tag)
            if prev_release is not None:
                raw_sources = client.download_release_asset(prev_release, "SOURCES.json")
                if raw_sources:
                    try:
                        prev_sources = json.loads(raw_sources.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        print(
                            f"::warning::failed to parse previous SOURCES.json: {exc}",
                            file=sys.stderr,
                        )
                if prev_sources is None:
                    raw_manifest = client.download_release_asset(prev_release, "MANIFEST.txt")
                    if raw_manifest:
                        prev_manifest = parse_manifest(
                            raw_manifest.decode("utf-8", errors="replace")
                        )

    notes = render_release_notes(
        tag=args.tag,
        tarball_name=args.tarball_name,
        manifest=manifest,
        curr_sources=curr_sources,
        prev_release=prev_release,
        prev_sources=prev_sources,
        prev_manifest=prev_manifest,
        compare_fn=compare_fn,
        commit_info_fn=commit_info_fn,
        max_commits=args.max_commits,
    )

    if args.summary_json:
        summary = build_release_summary(
            tag=args.tag,
            tarball_name=args.tarball_name,
            manifest=manifest,
            curr_sources=curr_sources,
            tarball_path=args.tarball,
            sha256_path=args.sha256_file,
            server_url=args.server_url,
            repo=args.repo,
        )
        args.summary_json.write_text(json.dumps(summary) + "\n", encoding="utf-8")

    if args.output:
        args.output.write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
