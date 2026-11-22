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
# Run Digitalis host unit and differential-fuzz test suites built in <aosp-tree>.
#
# Usage: scripts/run-host-tests.sh <aosp-tree> <report-dir>

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <aosp-tree> <report-dir>" >&2
  exit 2
fi

top="$(cd "$1" && pwd)"
mkdir -p "$2"
report_dir="$(cd "$2" && pwd)"

run_suite() {
  local name="$1"
  local timeout_limit="$2"
  shift 2
  local bin
  bin="$(find "$top/out/host/linux-x86/nativetest64" -type f -name "$name" -perm -u+x -print -quit 2>/dev/null || true)"
  if [[ -z "$bin" ]]; then
    bin="$(find "$top/out/host" -type f -name "$name" -perm -u+x -print -quit 2>/dev/null || true)"
  fi
  if [[ -z "$bin" ]]; then
    echo "missing host test binary: $name" >&2
    return 1
  fi
  echo "=== Running $name ($bin) ==="
  local status=0
  (
    cd "$(dirname "$bin")"
    timeout "$timeout_limit" "$bin" \
      --gtest_color=no \
      --gtest_output="xml:${report_dir}/${name}.xml" \
      "$@" \
      > "${report_dir}/${name}.txt" 2>&1
  ) || status=$?
  grep -E "^\[  (PASSED|FAILED|SKIPPED) |^\[==========\]|^\[  FAILED  \]" \
    "${report_dir}/${name}.txt" | tail -n 60 || true
  if (( status == 124 )); then
    echo "$name timed out after $timeout_limit" >&2
  fi
  return "$status"
}

overall=0
run_suite berberis_native_bridge_load_error_tests 5m || overall=$?
run_suite berberis_arm64_host_tests 60m --gtest_filter='-DigitalisBench.*' || overall=$?
exit "$overall"
