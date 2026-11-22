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
# Build Digitalis prebuilt artifacts and host tests inside a reduced AOSP tree.
#
# Usage: scripts/build.sh <aosp-tree> <framework-libs-dir>

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <aosp-tree> <framework-libs-dir>" >&2
  exit 2
fi

top="$(cd "$1" && pwd)"
framework_libs="$(cd "$2" && pwd)"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
product="digitalis_prebuilts"
device_dir="$top/device/digitalis/$product"

rm -rf "$device_dir"
mkdir -p "$device_dir"
cp -r "$repo_root/device/." "$device_dir/"
mkdir -p "$device_dir/prebuilts/x86_64"
cp "$framework_libs"/*.so "$device_dir/prebuilts/x86_64/"

while IFS= read -r rel_dir; do
  case "$rel_dir" in
    ""|\#*) continue ;;
  esac
  if [[ ! -d "$top/$rel_dir" ]]; then
    echo "missing header directory in reduced tree: $rel_dir" >&2
    exit 1
  fi
  mkdir -p "$device_dir/headers/$rel_dir"
  cp -r "$top/$rel_dir/." "$device_dir/headers/$rel_dir/"
done < "$repo_root/device/headers/include-dirs.txt"

cd "$top"

targets_file="$top/.digitalis-targets"
python3 "$script_dir/package_bundle.py" targets \
  --config-mk frameworks/libs/binary_translation/berberis_config.mk \
  --product-out "out/target/product/$product" > "$targets_file"

mapfile -t build_targets < "$targets_file"
host_test_targets=(
  berberis_arm64_host_tests
  berberis_native_bridge_load_error_tests
)
echo "building ${#build_targets[@]} distribution files and ${#host_test_targets[@]} host test targets"

export ALLOW_MISSING_DEPENDENCIES=true
export TARGET_BUILD_UNBUNDLED=true
export SOONG_ONLY=true
export SOONG_NINJA=ninja
export SKIP_ABI_CHECKS=true
export UNSAFE_DISABLE_HIDDENAPI_FLAGS=true
export DISABLE_ARTIFACT_PATH_REQUIREMENTS=true
export BUILD_USERNAME="${BUILD_USERNAME:-digitalis-ci}"
export BUILD_HOSTNAME="${BUILD_HOSTNAME:-github-actions}"

set +u
# shellcheck disable=SC1091
source build/envsetup.sh
lunch "${product}-trunk_staging-userdebug"
set -u

status=0
m --skip-ninja "${build_targets[@]}" "${host_test_targets[@]}" || status=$?
if (( status == 0 )); then
  python3 "$script_dir/check_ninja_inputs.py" \
    --top "$top" \
    --ninja "$top/prebuilts/build-tools/linux-x86/bin/ninja" \
    --file "$top/out/combined-${product}.ninja" \
    --report "$top/out/missing-inputs.txt" \
    "${build_targets[@]}" "${host_test_targets[@]}" \
    || status=$?
fi
if (( status == 0 )); then
  m -k -j"$(nproc)" "${build_targets[@]}" "${host_test_targets[@]}" || status=$?
fi

if (( status != 0 )); then
  echo "::group::Build failure summary"
  for log in out/error.log out/soong.log; do
    [[ -f "$log" ]] || continue
    grep -hoE "(depends on undefined module|missing dependencies?:|module source path|unrecognized module type|no such file or directory|Cannot find|not found)[^\"]{0,240}" "$log" \
      | sort | uniq -c | sort -rn | head -n 80 || true
  done
  echo "::endgroup::"
  tail -n 120 out/error.log 2>/dev/null || true
  echo "::group::soong.log tail"
  tail -n 40 out/soong.log 2>/dev/null || true
  echo "::endgroup::"
fi

exit "$status"
