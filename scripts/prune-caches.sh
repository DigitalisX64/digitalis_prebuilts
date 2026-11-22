#!/usr/bin/env bash
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
# Delete older Actions caches with the same key prefix on the current ref after
# saving a newer cache, keeping total repository cache usage under GitHub's 10 GB
# limit.
#
# Usage: scripts/prune-caches.sh <ref> <prefix> <kept-key>

set -euo pipefail

ref="${1:?usage: prune-caches.sh <ref> <prefix> <kept-key>}"
prefix="${2:?missing key prefix}"
kept="${3:?missing kept key}"

if [[ "$kept" != "$prefix"* ]]; then
  echo "kept key ($kept) must start with prefix ($prefix)" >&2
  exit 2
fi

caches="$(gh cache list --ref "$ref" --key "$prefix" --limit 100 --json id,key,createdAt)"
if ! jq -e --arg kept "$kept" 'any(.[]; .key == $kept)' <<<"$caches" >/dev/null; then
  echo "kept key $kept not found in cache; leaving existing entries untouched"
  exit 0
fi

jq -r --arg kept "$kept" '
  (map(select(.key == $kept)) | map(.createdAt) | min) as $saved
  | .[] | select(.key != $kept and .createdAt < $saved)
  | "\(.id)\t\(.key)"
' <<<"$caches" | while IFS=$'\t' read -r id key; do
  if gh cache delete "$id" >/dev/null 2>&1; then
    echo "pruned cache $id ($key)"
  else
    echo "cache $id already removed"
  fi
done
