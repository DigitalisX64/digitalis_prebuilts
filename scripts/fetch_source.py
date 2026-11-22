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
"""Fetch the pinned reduced AOSP + Digitalis source tree for unbundled builds."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock  # noqa: E402

BOOTSTRAP_PATHS = ("build/soong", "build/release")


def run_git(*args, cwd):
    subprocess.run(["git", "-c", "advice.detachedHead=false", *args], cwd=cwd, check=True)


def resolve_remote_ref(url, ref):
    """Resolve a branch, tag, or 40-char commit SHA against a remote repository."""
    if lock.COMMIT_RE.match(ref):
        return ref
    for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        proc = subprocess.run(
            ["git", "ls-remote", url, candidate],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if lines:
            # Prefer peeled tag commit (^{}) if present.
            for line in lines:
                if line.endswith("^{}"):
                    return line.split()[0]
            return lines[0].split()[0]
    raise SystemExit(f"ref {ref!r} not found in {url}")


def fetch_project(tree_top, project, clang_versions, rust_versions):
    dest = tree_top / project["path"]
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    mode, entries = lock.resolve_sparse(project, clang_versions, rust_versions)
    max_attempts = 6
    for attempt in range(max_attempts):
        try:
            git_dir = dest / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)
            run_git("init", "-q", cwd=dest)
            run_git("remote", "add", "origin", project["url"], cwd=dest)
            fetch_args = ["fetch", "-q", "--depth=1", "--no-tags"]
            if mode is not None:
                run_git("config", "core.sparseCheckout", "true", cwd=dest)
                run_git(
                    "sparse-checkout",
                    "set",
                    "--cone" if mode == "cone" else "--no-cone",
                    "--",
                    *entries,
                    cwd=dest,
                )
                fetch_args.append("--filter=blob:none")
            run_git(*fetch_args, "origin", project["commit"], cwd=dest)
            run_git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=dest)
            break
        except subprocess.CalledProcessError:
            if attempt == max_attempts - 1:
                raise
            # android.googlesource.com rate-limits bursts with HTTP 429.
            delay = min(30 * (2 ** attempt), 300)
            print(f"{project['path']}: fetch failed, retrying in {delay}s", flush=True)
            time.sleep(delay)

    shutil.rmtree(dest / ".git")
    for rel in project.get("exclude", []):
        target = dest / rel
        if not target.is_file():
            raise SystemExit(f"{project['path']}: excluded file {rel} missing after checkout")
        target.unlink()
    detail = f" ({mode}: {' '.join(entries)})" if mode else ""
    print(f"{project['path']} @ {project['commit'][:12]}{detail}", flush=True)


def materialize_links(tree_top, project):
    for entry in project.get("linkfiles", []):
        src = tree_top / project["path"] / entry["src"]
        dst = tree_top / entry["dest"]
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(os.path.relpath(src, dst.parent))
    for entry in project.get("copyfiles", []):
        src = tree_top / project["path"] / entry["src"]
        dst = tree_top / entry["dest"]
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def apply_ref_overrides(projects, android, bt_ref="", digitalis_ref="", use_branch_head=False):
    """Return a copy of `projects` with optional live branch/SHA overrides applied."""
    updated = [dict(p) for p in projects]
    effective_bt_ref = bt_ref or (android if use_branch_head else "")
    effective_dig_ref = digitalis_ref or (android if use_branch_head else "")
    for proj in updated:
        if proj["path"] == lock.TRANSLATOR_PATH and effective_bt_ref:
            sha = resolve_remote_ref(proj["url"], effective_bt_ref)
            print(
                f"override {proj['path']}: {effective_bt_ref} -> {sha[:12]}",
                flush=True,
            )
            proj["commit"] = sha
            proj["resolved_ref"] = effective_bt_ref
        elif proj["path"] == lock.DIGITALIS_PATH and effective_dig_ref:
            sha = resolve_remote_ref(proj["url"], effective_dig_ref)
            print(
                f"override {proj['path']}: {effective_dig_ref} -> {sha[:12]}",
                flush=True,
            )
            proj["commit"] = sha
            proj["resolved_ref"] = effective_dig_ref
    return updated


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--android", required=True, help="android16 or android17")
    ap.add_argument("--top", type=Path, required=True, help="destination AOSP tree directory")
    ap.add_argument("--lock", type=Path, default=lock.DEFAULT_LOCK)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument(
        "--bt-ref",
        default="",
        help="optional branch, tag, or commit SHA override for frameworks/libs/binary_translation",
    )
    ap.add_argument(
        "--digitalis-ref",
        default="",
        help="optional branch, tag, or commit SHA override for digitalis",
    )
    ap.add_argument(
        "--use-branch-head",
        action="store_true",
        help="resolve DigitalisX64 repos from the live HEAD of the target android branch",
    )
    args = ap.parse_args(argv)

    version = lock.get_version(lock.load_lock(args.lock), args.android)
    tree_top = args.top.resolve()
    tree_top.mkdir(parents=True, exist_ok=True)

    manifest_info = dict(version["manifest"])
    if args.use_branch_head:
        manifest_sha = resolve_remote_ref(manifest_info["url"], manifest_info["branch"])
        print(
            f"override manifest: {manifest_info['branch']} -> {manifest_sha[:12]}",
            flush=True,
        )
        manifest_info["commit"] = manifest_sha

    projects = apply_ref_overrides(
        version["projects"],
        android=args.android,
        bt_ref=args.bt_ref,
        digitalis_ref=args.digitalis_ref,
        use_branch_head=args.use_branch_head,
    )
    bootstrap = [p for p in projects if p["path"] in BOOTSTRAP_PATHS]
    for proj in bootstrap:
        fetch_project(tree_top, proj, (), ())

    clang_versions = lock.detect_clang_versions(tree_top)
    rust_versions = lock.detect_rust_versions(tree_top)
    print(
        f"detected toolchains: clang={','.join(clang_versions)} "
        f"rust={','.join(rust_versions) or 'none'}",
        flush=True,
    )

    remaining = [p for p in projects if p["path"] not in BOOTSTRAP_PATHS]
    # Start large prebuilt repos first so they overlap with smaller repos.
    remaining.sort(key=lambda p: not p["path"].startswith("prebuilts/"))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [
            pool.submit(fetch_project, tree_top, proj, clang_versions, rust_versions)
            for proj in remaining
        ]
        for fut in futures:
            fut.result()

    for proj in projects:
        materialize_links(tree_top, proj)

    sources = {
        "android": version["android"],
        "android_major": version["android_major"],
        "api": version["api"],
        "aosp_branch": version["aosp_branch"],
        "aosp_images": version["aosp_images"],
        "manifest": manifest_info,
        "superproject": version["superproject"],
        "clang": clang_versions,
        "rust": rust_versions,
        "projects": {
            p["path"]: {
                "url": p["url"],
                "commit": p["commit"],
                **({"ref": p["resolved_ref"]} if "resolved_ref" in p else {}),
            }
            for p in projects
        },
    }
    (tree_top / ".digitalis-sources.json").write_text(
        json.dumps(sources, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
