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
"""Check that every leaf input in ninja's build graph exists on disk."""

import argparse
import os
import re
import subprocess
import sys

NODE_RE = re.compile(r'^"(0x[0-9a-f]+)" \[label="((?:[^"\\]|\\.)*)"(.*)\]$')
EDGE_RE = re.compile(r'^"(0x[0-9a-f]+)" -> "(0x[0-9a-f]+)"')


def find_leaf_inputs(dot_lines):
    files = {}
    produced = set()
    for raw in dot_lines:
        line = raw.strip()
        m = NODE_RE.match(line)
        if m:
            node_id, label, attrs = m.groups()
            if "shape=ellipse" not in attrs:
                files[node_id] = label.replace('\\"', '"').replace("\\\\", "\\")
            continue
        m = EDGE_RE.match(line)
        if m:
            produced.add(m.group(2))
    return sorted(label for node_id, label in files.items() if node_id not in produced)


def find_missing_inputs(tree_top, paths):
    return [
        p for p in paths
        if not os.path.exists(p if os.path.isabs(p) else os.path.join(tree_top, p))
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", required=True)
    ap.add_argument("--ninja", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--report", help="optional path to save missing-input summary")
    ap.add_argument("targets", nargs="+")
    args = ap.parse_args(argv)

    proc = subprocess.run(
        [args.ninja, "-f", args.file, "-t", "graph", *args.targets],
        cwd=args.top,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print("failed to query ninja graph", file=sys.stderr)
        return 1

    leaves = find_leaf_inputs(proc.stdout.splitlines())
    missing = find_missing_inputs(args.top, leaves)
    lines = [f"checked {len(leaves)} leaf inputs; {len(missing)} missing"]
    lines.extend(f"missing input: {p}" for p in missing)
    if missing:
        counts = {}
        for p in missing:
            key = "/".join(p.split("/")[:2])
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{k} ({n})" for k, n in sorted(counts.items()))
        lines.append(f"by directory: {summary}")

    report_text = "\n".join(lines) + "\n"
    sys.stdout.write(report_text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report_text)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
