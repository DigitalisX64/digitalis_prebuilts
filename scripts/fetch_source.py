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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--android", required=True, help="android16 or android17")
    ap.add_argument("--top", type=Path, required=True, help="destination AOSP tree directory")
    ap.add_argument("--lock", type=Path, default=lock.DEFAULT_LOCK)
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args(argv)

    version = lock.get_version(lock.load_lock(args.lock), args.android)
    tree_top = args.top.resolve()
    tree_top.mkdir(parents=True, exist_ok=True)

    projects = version["projects"]
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
        "manifest": version["manifest"],
        "superproject": version["superproject"],
        "clang": clang_versions,
        "rust": rust_versions,
        "projects": {
            p["path"]: {"url": p["url"], "commit": p["commit"]} for p in projects
        },
    }
    (tree_top / ".digitalis-sources.json").write_text(
        json.dumps(sources, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
