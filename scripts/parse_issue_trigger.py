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
"""Parse a GitHub issue or issue comment to determine prebuilt CI actions.

Supports:
1. GitHub Issue Forms (.github/ISSUE_TEMPLATE/build-prebuilts.yml)
2. Key-value lines (e.g. `android: android17`, `branch: fix/foo`, `release: true`, `tag: v1.0`)
3. Build slash commands (`/build`, `/rebuild`, `/release`)
4. Control slash commands (`/cancel`, `/stop`, `/abort`, `/status`, `/help`)
5. Short free-form maintainer requests (e.g. title `Build android17` or `Build android16 and android17`)

Only authorized repository roles (OWNER, MEMBER, COLLABORATOR) are allowed to
trigger or cancel builds so public issue spam cannot consume runner minutes or
publish releases.
"""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock  # noqa: E402

ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")
BUILD_INTENT_RE = re.compile(r"\b(build|rebuild|package|compile|release|prebuilts?)\b", re.IGNORECASE)
SLASH_CMD_RE = re.compile(r"(?im)^[ \t]*/(build|rebuild|release)(?:[ \t]+([^\r\n]*))?[ \t]*$")
CONTROL_CMD_RE = re.compile(r"(?im)^[ \t]*/(cancel|stop|abort|status|help)(?:[ \t]+([^\r\n]*))?[ \t]*$")


def _parse_issue_form_sections(body):
    """Extract `### Heading` -> trimmed content pairs from a GitHub Issue Form."""
    sections = {}
    current = None
    buf = []
    for line in (body or "").splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return {k: ("" if v == "_No response_" else v) for k, v in sections.items()}


def _parse_kv_lines(text):
    """Extract simple `key: value` or `key=value` lines."""
    kv = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        key = m.group(1).lower().replace("-", "_")
        val = m.group(2).strip().strip("`\"'")
        if key in {
            "android",
            "target",
            "version",
            "versions",
            "branch",
            "ref",
            "bt_ref",
            "bt_branch",
            "translator_branch",
            "translator_ref",
            "digitalis_ref",
            "digitalis_branch",
            "release",
            "publish",
            "publish_release",
            "tag",
            "release_tag",
            "pinned",
        }:
            kv[key] = val
    return kv


def _parse_slash_command(text):
    m = SLASH_CMD_RE.search(text or "")
    if not m:
        return None
    cmd = m.group(1).lower()
    arg_str = (m.group(2) or "").strip()
    tokens = shlex.split(arg_str) if arg_str else []
    parsed = {
        "command": cmd,
        "targets": [],
        "bt_ref": "",
        "digitalis_ref": "",
        "publish_release": True if cmd == "release" else None,
        "release_tag": "",
        "pinned": None,
    }
    for tok in tokens:
        low = tok.lower()
        if low in ("all", "both"):
            parsed["targets"].append("all")
        elif re.fullmatch(r"android[0-9]+", low):
            parsed["targets"].append(low)
        elif low in ("release", "--release", "publish"):
            parsed["publish_release"] = True
        elif low in ("no-release", "--no-release"):
            parsed["publish_release"] = False
        elif low in ("pinned", "--pinned"):
            parsed["pinned"] = True
        elif low in ("head", "latest", "--head"):
            parsed["pinned"] = False
        elif "=" in tok:
            k, v = tok.split("=", 1)
            k = k.lower().replace("-", "_")
            v = v.strip().strip("`\"'")
            if k in ("android", "target", "version"):
                parsed["targets"].extend(re.split(r"[,\s]+", v.lower()))
            elif k in ("branch", "ref", "bt_ref", "bt_branch", "translator"):
                parsed["bt_ref"] = v
            elif k in ("digitalis", "digitalis_ref", "digitalis_branch"):
                parsed["digitalis_ref"] = v
            elif k in ("tag", "release_tag"):
                parsed["release_tag"] = v
                parsed["publish_release"] = True
            elif k in ("release", "publish", "publish_release"):
                parsed["publish_release"] = _bool_val(v)
            elif k == "pinned":
                parsed["pinned"] = _bool_val(v)
        elif not parsed["bt_ref"]:
            parsed["bt_ref"] = tok
    return parsed


def parse_control_command(*, author_association, comment_body=""):
    """Parse a control slash command (`/cancel`, `/stop`, `/abort`, `/status`, `/help`)."""
    m = CONTROL_CMD_RE.search(comment_body or "")
    if not m:
        return {"action": "none", "reason": "comment does not contain a control command"}
    if author_association not in ALLOWED_ASSOCIATIONS:
        return {
            "action": "none",
            "reason": f"author association {author_association!r} is not authorized",
        }

    raw_cmd = m.group(1).lower()
    arg_str = (m.group(2) or "").strip()
    tokens = shlex.split(arg_str) if arg_str else []

    if raw_cmd in ("cancel", "stop", "abort"):
        cancel_all = False
        run_ids = []
        for tok in tokens:
            low = tok.lower()
            if low in ("all", "--all"):
                cancel_all = True
            elif tok.isdigit():
                run_ids.append(int(tok))
            else:
                url_match = re.search(r"/actions/runs/(\d+)", tok)
                if url_match:
                    run_ids.append(int(url_match.group(1)))
        return {
            "action": "cancel",
            "cancel_all": cancel_all,
            "run_ids": run_ids,
            "reason": f"matched /{raw_cmd} command",
        }

    if raw_cmd == "status":
        return {"action": "status", "reason": "matched /status command"}

    return {"action": "help", "reason": "matched /help command"}


def _normalize_targets(raw_targets, available):
    out = []
    for item in raw_targets:
        tok = item.strip().lower()
        if not tok:
            continue
        if tok in ("all", "both"):
            return list(available)
        if tok in available and tok not in out:
            out.append(tok)
        elif tok.isdigit() and f"android{tok}" in available:
            name = f"android{tok}"
            if name not in out:
                out.append(name)
    return out


def _extract_mentions(text, available):
    found = []
    low = (text or "").lower()
    if re.search(r"\b(all|both)\b", low) and re.search(
        r"\b(android|versions?|branches?|targets?)\b", low
    ):
        return list(available)
    for ver in available:
        num = ver[len("android"):]
        if re.search(rf"\b(?:android[\s_-]?{num}|aosp[\s_-]?{num}|port{num})\b", low):
            found.append(ver)
    return found


def _validate_ref(ref, label):
    if not ref:
        return ""
    if ".." in ref or ref.startswith("-") or not SAFE_REF_RE.match(ref):
        raise ValueError(f"invalid {label}: {ref!r}")
    return ref


def _bool_val(val):
    return str(val).strip().lower() in ("1", "true", "yes", "on", "y")


def _extract_fields_from_sections_and_kv(sections, kv):
    raw_targets = []
    bt_ref = ""
    digitalis_ref = ""
    publish_release = None
    release_tag = ""
    pinned = None

    for key in ("target android", "android version", "target"):
        if sections.get(key):
            raw_targets.extend(re.split(r"[,\s]+", sections[key]))
    for key in ("translator branch or commit (optional)", "translator branch or commit", "branch"):
        if sections.get(key) and not bt_ref:
            bt_ref = sections[key].splitlines()[0].strip().strip("`\"'")
    for key in ("digitalis repo branch or commit (optional)", "digitalis branch"):
        if sections.get(key) and not digitalis_ref:
            digitalis_ref = sections[key].splitlines()[0].strip().strip("`\"'")
    for key in ("publish github release", "release"):
        if sections.get(key):
            if re.search(r"\[[xX]\]", sections[key]) or _bool_val(sections[key]):
                publish_release = True
    for key in ("release tag (optional)", "release tag"):
        if sections.get(key) and not release_tag:
            release_tag = sections[key].splitlines()[0].strip().strip("`\"'")
            if release_tag:
                publish_release = True

    for key in ("android", "target", "version", "versions"):
        if key in kv:
            raw_targets.extend(re.split(r"[,\s]+", kv[key]))
    for key in ("bt_ref", "bt_branch", "translator_ref", "translator_branch", "ref", "branch"):
        if key in kv and not bt_ref:
            bt_ref = kv[key]
    for key in ("digitalis_ref", "digitalis_branch"):
        if key in kv and not digitalis_ref:
            digitalis_ref = kv[key]
    for key in ("publish_release", "publish", "release"):
        if key in kv:
            publish_release = _bool_val(kv[key])
    for key in ("release_tag", "tag"):
        if key in kv and not release_tag:
            release_tag = kv[key]
            publish_release = True
    if "pinned" in kv:
        pinned = _bool_val(kv["pinned"])

    return {
        "targets": raw_targets,
        "bt_ref": bt_ref,
        "digitalis_ref": digitalis_ref,
        "publish_release": publish_release,
        "release_tag": release_tag,
        "pinned": pinned,
    }


def parse_event(
    *,
    event_name,
    author_association,
    title="",
    body="",
    comment_body="",
    labels=(),
    available_versions=("android17", "android16"),
):
    """Analyze an issue or issue_comment event and return a structured trigger plan."""
    available = list(available_versions)

    if author_association not in ALLOWED_ASSOCIATIONS:
        return {
            "should_build": False,
            "reason": f"author association {author_association!r} is not authorized",
        }

    is_comment = event_name == "issue_comment"
    active_text = comment_body if is_comment else body

    slash = _parse_slash_command(active_text)
    if is_comment and slash is None:
        return {
            "should_build": False,
            "reason": "comment does not contain a /build, /rebuild, or /release command",
        }

    sections = {} if is_comment else _parse_issue_form_sections(body)
    kv = _parse_kv_lines(active_text)

    has_form = any(
        k in sections
        for k in ("target android", "android version", "translator branch or commit (optional)")
    )
    has_build_label = any(lbl.lower() in ("build", "build-prebuilts") for lbl in labels)
    has_build_intent = (
        slash is not None
        or has_form
        or has_build_label
        or bool(kv)
        or bool(BUILD_INTENT_RE.search(f"{title}\n{body}"))
    )
    if not has_build_intent:
        return {"should_build": False, "reason": "issue does not request a prebuilt build"}

    raw_targets = []
    bt_ref = ""
    digitalis_ref = ""
    publish_release = None
    release_tag = ""
    pinned = None

    if slash is not None:
        raw_targets.extend(slash["targets"])
        bt_ref = slash["bt_ref"]
        digitalis_ref = slash["digitalis_ref"]
        publish_release = slash["publish_release"]
        release_tag = slash["release_tag"]
        pinned = slash["pinned"]

    parsed_fields = _extract_fields_from_sections_and_kv(sections, kv)
    raw_targets.extend(parsed_fields["targets"])
    if not bt_ref:
        bt_ref = parsed_fields["bt_ref"]
    if not digitalis_ref:
        digitalis_ref = parsed_fields["digitalis_ref"]
    if publish_release is None:
        publish_release = parsed_fields["publish_release"]
    if not release_tag and parsed_fields["release_tag"]:
        release_tag = parsed_fields["release_tag"]
        if publish_release is None:
            publish_release = True
    if pinned is None:
        pinned = parsed_fields["pinned"]

    # When commenting bare `/build`, `/rebuild`, or `/release` (without overriding targets or
    # branch in the comment), inherit settings from the parent issue's form/key-value fields.
    if is_comment and not raw_targets and not bt_ref:
        parent_fields = _extract_fields_from_sections_and_kv(
            _parse_issue_form_sections(body),
            _parse_kv_lines(body),
        )
        raw_targets.extend(parent_fields["targets"])
        if not bt_ref:
            bt_ref = parent_fields["bt_ref"]
        if not digitalis_ref:
            digitalis_ref = parent_fields["digitalis_ref"]
        if publish_release is None:
            publish_release = parent_fields["publish_release"]
        if not release_tag and parent_fields["release_tag"] and publish_release is not False:
            release_tag = parent_fields["release_tag"]
        if pinned is None:
            pinned = parent_fields["pinned"]

    publish_release = bool(publish_release)
    pinned = bool(pinned)

    # If `branch` was given as `android16` / `android17` / `all`, also treat it as target Android.
    if bt_ref and not raw_targets:
        low_ref = bt_ref.lower()
        if low_ref in ("all", "both") or low_ref in available:
            raw_targets.append(low_ref)
            if low_ref in ("all", "both"):
                bt_ref = ""

    versions = _normalize_targets(raw_targets, available)
    if not versions:
        # Check free-form mentions in title/body (or issue title+body when commenting `/build`).
        search_text = f"{title}\n{body}" if not is_comment else f"{comment_body}\n{title}\n{body}"
        versions = _extract_mentions(search_text, available)
    if not versions and slash is not None and not raw_targets and not bt_ref:
        versions = list(available)

    if not versions:
        return {
            "should_build": False,
            "reason": (
                "no target Android version identified; specify android17, android16, or all"
            ),
        }

    # For a single version with a custom bt_ref (e.g. branch=fix/foo), keep bt_ref.
    # For multiple versions, a single non-matching custom bt_ref would fail on the
    # other AOSP version, so reject unless bt_ref is empty.
    bt_ref = _validate_ref(bt_ref, "branch/ref")
    digitalis_ref = _validate_ref(digitalis_ref, "digitalis_ref")
    release_tag = _validate_ref(release_tag, "release_tag")

    if len(versions) > 1 and bt_ref:
        return {
            "should_build": False,
            "reason": (
                f"custom translator ref {bt_ref!r} cannot be applied to multiple Android versions "
                f"({', '.join(versions)}); specify a single Android version"
            ),
        }

    # Unless `pinned` was explicitly requested, issue-triggered builds track the
    # live HEAD of each target Android branch (or the requested custom bt_ref).
    use_branch_head = not pinned

    return {
        "should_build": True,
        "versions": versions,
        "bt_ref": bt_ref,
        "digitalis_ref": digitalis_ref,
        "use_branch_head": use_branch_head,
        "publish_release": publish_release,
        "release_tag": release_tag,
        "reason": "matched authorized build request",
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event-name", required=True, choices=("issues", "issue_comment"))
    ap.add_argument("--event-path", type=Path, required=True, help="GitHub GITHUB_EVENT_PATH JSON")
    ap.add_argument("--lock", type=Path, default=lock.DEFAULT_LOCK)
    args = ap.parse_args(argv)

    data = lock.load_lock(args.lock)
    available = [v["android"] for v in data["versions"]]
    payload = json.loads(args.event_path.read_text(encoding="utf-8"))

    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    if args.event_name == "issue_comment":
        assoc = comment.get("author_association", "")
    else:
        assoc = issue.get("author_association", "")

    result = parse_event(
        event_name=args.event_name,
        author_association=assoc,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        comment_body=comment.get("body") or "",
        labels=[lbl.get("name", "") for lbl in issue.get("labels") or []],
        available_versions=available,
    )
    result["issue_number"] = issue.get("number")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
