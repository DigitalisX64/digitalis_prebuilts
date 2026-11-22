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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", required=True, help="release tag being published")
    ap.add_argument("--tarball-name", required=True, help="prebuilt .tar.gz filename")
    ap.add_argument("--manifest", type=Path, required=True, help="path to MANIFEST.txt")
    ap.add_argument("--sources", type=Path, help="path to SOURCES.json")
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
    args = ap.parse_args(argv)

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

    if args.output:
        args.output.write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
