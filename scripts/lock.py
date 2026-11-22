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
"""Read, validate, and query config/versions.json."""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = REPO_ROOT / "config" / "versions.json"

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REL_PATH_RE = re.compile(r"^[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*$")
CLANG_DEFAULT_RE = re.compile(r'ClangDefaultVersion\s*=\s*"(clang-[A-Za-z0-9.]+)"')
CLANG_FLAG_RE = re.compile(r'string_value:\s*"(clang-[A-Za-z0-9.]+)"')
RUST_DEFAULT_RE = re.compile(r'RustDefaultVersion\s*=\s*"([0-9][A-Za-z0-9.]*)"')

TRANSLATOR_PATH = "frameworks/libs/binary_translation"
DIGITALIS_PATH = "digitalis"
CLANG_TOKEN = "{clang}"
RUST_TOKEN = "{rust}"


class LockError(ValueError):
    """Raised when config/versions.json fails schema validation."""


def load_lock(path=DEFAULT_LOCK):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_lock(data)
    return data


def _check_project(android, project):
    path = project.get("path", "")
    ctx = f"{android} {path or '(missing path)'}"
    if not REL_PATH_RE.match(path) or ".." in path.split("/"):
        raise LockError(f"{ctx}: path must be relative without '..'")
    if not COMMIT_RE.match(project.get("commit", "")):
        raise LockError(f"{ctx}: commit must be a 40-char lowercase hex SHA")
    if not project.get("url", "").startswith("https://"):
        raise LockError(f"{ctx}: url must start with https://")
    if "cone" in project and "patterns" in project:
        raise LockError(f"{ctx}: specify at most one of 'cone' or 'patterns'")
    for key in ("cone", "patterns"):
        if key in project:
            entries = project[key]
            if (
                not isinstance(entries, list)
                or not entries
                or not all(isinstance(e, str) and e and ".." not in e.split("/") for e in entries)
            ):
                raise LockError(f"{ctx}: {key} must be a non-empty list of safe paths/patterns")
    for name in project.get("exclude", []):
        if not isinstance(name, str) or not name or name.startswith("/") or ".." in name.split("/"):
            raise LockError(f"{ctx}: invalid exclude entry {name!r}")
    for key in ("linkfiles", "copyfiles"):
        for entry in project.get(key, []):
            for side in ("src", "dest"):
                val = entry.get(side, "")
                if not val or val.startswith("/") or ".." in val.split("/"):
                    raise LockError(f"{ctx}: invalid {key[:-1]} {side} {val!r}")


def _check_aosp_images(android, images):
    if not isinstance(images, dict):
        raise LockError(f"{android}: missing aosp_images object")
    for arch in ("x86_64", "arm64"):
        img = images.get(arch)
        if not isinstance(img, dict):
            raise LockError(f"{android}: aosp_images missing '{arch}'")
        for field in ("build_id", "target", "file"):
            if not isinstance(img.get(field), str) or not img[field]:
                raise LockError(f"{android} aosp_images.{arch}: missing '{field}'")
        if not isinstance(img.get("size"), int) or img["size"] <= 0:
            raise LockError(f"{android} aosp_images.{arch}: size must be a positive int")
        if not SHA256_RE.match(img.get("sha256", "")):
            raise LockError(f"{android} aosp_images.{arch}: invalid sha256")


def validate_lock(data):
    versions = data.get("versions")
    if not isinstance(versions, list) or not versions:
        raise LockError("versions must be a non-empty list")
    seen = set()
    for version in versions:
        android = version.get("android")
        if not isinstance(android, str) or not re.fullmatch(r"android[0-9]+", android):
            raise LockError(f"invalid android branch name: {android!r}")
        if android in seen:
            raise LockError(f"duplicate android entry: {android}")
        seen.add(android)
        if version.get("android_major") != int(android[len("android"):]):
            raise LockError(f"{android}: android_major mismatch")
        if not isinstance(version.get("api"), int) or version["api"] <= 0:
            raise LockError(f"{android}: api must be a positive int")
        _check_aosp_images(android, version.get("aosp_images"))
        for pin_name in ("manifest", "superproject"):
            pin = version.get(pin_name) or {}
            if not COMMIT_RE.match(pin.get("commit", "")):
                raise LockError(f"{android}: {pin_name}.commit must be a 40-char SHA")
        projects = version.get("projects")
        if not isinstance(projects, list) or not projects:
            raise LockError(f"{android}: projects must be a non-empty list")
        paths = set()
        for proj in projects:
            _check_project(android, proj)
            if proj["path"] in paths:
                raise LockError(f"{android}: duplicate project path {proj['path']}")
            paths.add(proj["path"])
        if TRANSLATOR_PATH not in paths:
            raise LockError(f"{android}: missing translator project {TRANSLATOR_PATH}")


def get_version(lock_data, android):
    for entry in lock_data["versions"]:
        if entry["android"] == android:
            return entry
    avail = ", ".join(v["android"] for v in lock_data["versions"])
    raise LockError(f"unknown AOSP branch {android!r}; available: {avail}")


def get_project(version, path):
    for proj in version["projects"]:
        if proj["path"] == path:
            return proj
    raise LockError(f"{version['android']}: project {path} not in lock")


def resolve_sparse(project, clang_versions=(), rust_versions=()):
    mode = "cone" if "cone" in project else "patterns" if "patterns" in project else None
    if mode is None:
        return None, []
    resolved = []
    for item in project[mode]:
        if CLANG_TOKEN in item:
            if not clang_versions:
                raise LockError(f"{project['path']}: clang version unknown for {item}")
            resolved.extend(item.replace(CLANG_TOKEN, v) for v in clang_versions)
        elif RUST_TOKEN in item:
            if not rust_versions:
                raise LockError(f"{project['path']}: rust version unknown for {item}")
            resolved.extend(item.replace(RUST_TOKEN, v) for v in rust_versions)
        else:
            resolved.append(item)
    return mode, list(dict.fromkeys(resolved))


def detect_clang_versions(tree_top, release="trunk_staging"):
    top = Path(tree_top)
    flag_file = top / "build/release/flag_values" / release / "RELEASE_BUILD_CLANG_VERSION.textproto"
    if flag_file.is_file():
        m = CLANG_FLAG_RE.search(flag_file.read_text(encoding="utf-8"))
        if m:
            return [m.group(1)]
    global_go = top / "build/soong/cc/config/global.go"
    m = CLANG_DEFAULT_RE.search(global_go.read_text(encoding="utf-8"))
    if not m:
        raise LockError("could not detect ClangDefaultVersion from build/soong/cc/config/global.go")
    return [m.group(1)]


def detect_rust_versions(tree_top):
    global_go = Path(tree_top) / "build/soong/rust/config/global.go"
    if not global_go.is_file():
        return []
    m = RUST_DEFAULT_RE.search(global_go.read_text(encoding="utf-8"))
    return [m.group(1)] if m else []


def main(argv):
    args = argv[1:]
    lock_path = DEFAULT_LOCK
    if len(args) >= 2 and args[0] == "--lock":
        lock_path = Path(args[1])
        args = args[2:]

    data = load_lock(lock_path)
    if args == ["check"]:
        print(f"OK: {len(data['versions'])} versions valid in {lock_path}")
    elif args == ["versions"]:
        print(json.dumps([v["android"] for v in data["versions"]]))
    elif len(args) == 2 and args[0] == "info":
        v = get_version(data, args[1])
        bt = get_project(v, TRANSLATOR_PATH)["commit"]
        dig = get_project(v, DIGITALIS_PATH)["commit"] if any(
            p["path"] == DIGITALIS_PATH for p in v["projects"]
        ) else "unknown"
        print(json.dumps({
            "android": v["android"],
            "android_major": v["android_major"],
            "api": v["api"],
            "bt_sha": bt,
            "bt_short": bt[:8],
            "digitalis_sha": dig,
            "digitalis_short": dig[:8],
        }))
    else:
        raise SystemExit(
            "usage: lock.py [--lock PATH] check | versions | info <android16|android17>"
        )


if __name__ == "__main__":
    main(sys.argv)
