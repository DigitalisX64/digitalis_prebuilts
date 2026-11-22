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
"""Refresh pinned commits in config/versions.json.

Examples:
    scripts/update_lock.py --digitalis-only
    scripts/update_lock.py --android android17
    scripts/update_lock.py --missing
"""

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock  # noqa: E402

MANIFEST_REPO = "https://github.com/DigitalisX64/manifest"
MANIFEST_RAW = "https://raw.githubusercontent.com/DigitalisX64/manifest/{commit}/{file}"
DIGITALIS_GITHUB_PREFIX = "https://github.com/DigitalisX64/"


def run_git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def resolve_branch_head(url, branch):
    out = run_git("ls-remote", url, f"refs/heads/{branch}")
    if not out.strip():
        raise SystemExit(f"branch {branch!r} not found at {url}")
    return out.split()[0]


def to_https(fetch):
    if fetch.startswith("git@github.com:"):
        fetch = "https://github.com/" + fetch[len("git@github.com:"):]
    elif fetch.startswith("ssh://git@github.com/"):
        fetch = "https://github.com/" + fetch[len("ssh://git@github.com:"):]
    return fetch.rstrip("/") + "/"


def parse_manifest(commit):
    remotes, projects, superproject, default = {}, {}, None, {}
    for fname in ("default.xml", "digitalis.xml"):
        req = urllib.request.Request(
            MANIFEST_RAW.format(commit=commit, file=fname),
            headers={"User-Agent": "digitalis-prebuilts"},
        )
        with urllib.request.urlopen(req) as resp:
            root = ET.fromstring(resp.read())
        for remote in root.findall("remote"):
            remotes[remote.get("name")] = {
                "fetch": to_https(remote.get("fetch")),
                "revision": remote.get("revision"),
            }
        if root.find("default") is not None:
            default = root.find("default").attrib
        if root.find("superproject") is not None:
            superproject = root.find("superproject").attrib
        for rem in root.findall("remove-project"):
            projects.pop(rem.get("path") or rem.get("name"), None)
        for elem in root.findall("project"):
            rname = elem.get("remote") or default.get("remote")
            path = elem.get("path") or elem.get("name")
            projects[path] = {
                "name": elem.get("name"),
                "remote": rname,
                "revision": (
                    elem.get("revision")
                    or remotes[rname].get("revision")
                    or default.get("revision")
                ),
                "linkfiles": [dict(x.attrib) for x in elem.findall("linkfile")],
                "copyfiles": [dict(x.attrib) for x in elem.findall("copyfile")],
            }
    return remotes, projects, superproject, default


def read_superproject_gitlinks(url, ref):
    with tempfile.TemporaryDirectory() as tmp:
        run_git("init", "-q", tmp)
        wanted = ref if lock.COMMIT_RE.match(ref) else f"refs/heads/{ref}"
        run_git("fetch", "-q", "--depth=1", "--filter=blob:none", url, wanted, cwd=tmp)
        head = run_git("rev-parse", "FETCH_HEAD", cwd=tmp).strip()
        links = {}
        for line in run_git("ls-tree", "-r", "FETCH_HEAD", cwd=tmp).splitlines():
            meta, path = line.split("\t", 1)
            _, kind, sha = meta.split()
            if kind == "commit":
                links[path] = sha
    return head, links


def refresh_version(version, missing_only, digitalis_only):
    android = version["android"]
    manifest = version.setdefault("manifest", {"url": MANIFEST_REPO, "branch": android})
    if not missing_only or not manifest.get("commit"):
        manifest["commit"] = resolve_branch_head(manifest["url"], manifest["branch"])

    remotes, projects, superproject, default = parse_manifest(manifest["commit"])
    aosp_url = remotes[superproject.get("remote") or default["remote"]]["fetch"]
    sp_branch = superproject.get("revision") or default["revision"]

    sp_links = {}
    if not digitalis_only:
        sp_pin = version.setdefault("superproject", {})
        if not missing_only or not sp_pin.get("commit"):
            head, sp_links = read_superproject_gitlinks(aosp_url + superproject["name"], sp_branch)
            sp_pin.update({"url": aosp_url + superproject["name"], "branch": sp_branch, "commit": head})
        else:
            _, sp_links = read_superproject_gitlinks(aosp_url + superproject["name"], sp_pin["commit"])
        version["aosp_branch"] = sp_branch

    for entry in version["projects"]:
        if missing_only and entry.get("commit"):
            continue
        found = projects.get(entry["path"])
        if found is None:
            raise SystemExit(f"{android}: project {entry['path']} not in manifest")
        url = remotes[found["remote"]]["fetch"] + found["name"]
        if digitalis_only and not url.startswith(DIGITALIS_GITHUB_PREFIX):
            continue
        entry["name"] = found["name"]
        entry["url"] = url
        if url.startswith(aosp_url) and entry["path"] in sp_links:
            entry["commit"] = sp_links[entry["path"]]
        else:
            entry["commit"] = resolve_branch_head(url, found["revision"])
        for key in ("linkfiles", "copyfiles"):
            if found[key]:
                entry[key] = [{"src": x["src"], "dest": x["dest"]} for x in found[key]]
            else:
                entry.pop(key, None)
        print(f"{android}: {entry['path']} -> {entry['commit'][:12]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", type=Path, default=lock.DEFAULT_LOCK)
    ap.add_argument("--android", action="append", help="restrict to android16/android17")
    ap.add_argument("--missing", action="store_true", help="only fill entries missing a commit")
    ap.add_argument(
        "--digitalis-only",
        action="store_true",
        help="refresh only DigitalisX64 GitHub repos, keeping AOSP pins unchanged",
    )
    args = ap.parse_args(argv)
    data = json.loads(args.lock.read_text(encoding="utf-8"))
    for version in data["versions"]:
        if args.android and version["android"] not in args.android:
            continue
        refresh_version(version, args.missing, args.digitalis_only)
    lock.validate_lock(data)
    args.lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
