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
# Reclaim disk space on a GitHub-hosted Ubuntu runner, configure swap for
# parallel Clang compiles, allow unprivileged user namespaces for Soong's
# sandbox on Ubuntu 24.04, and export DIGITALIS_WORK via GITHUB_ENV.

set -euo pipefail

WORK_NAME="${1:-digitalis-prebuilts}"

echo "::group::Disk before cleanup"
df -h / /mnt 2>/dev/null || df -h /
echo "::endgroup::"

sudo rm -rf \
  /usr/share/dotnet \
  /usr/local/lib/android \
  /opt/ghc \
  /usr/local/.ghcup \
  /opt/hostedtoolcache \
  /usr/local/share/boost \
  /usr/share/swift \
  /usr/local/share/powershell \
  /usr/local/share/chromium \
  /usr/local/lib/node_modules \
  /opt/az \
  /opt/microsoft \
  /opt/google \
  /usr/lib/google-cloud-sdk \
  /usr/share/miniconda \
  /usr/local/julia* \
  /usr/lib/jvm \
  /opt/pipx \
  /usr/local/aws-cli \
  /usr/local/aws-sam-cli \
  /usr/share/az_* \
  || true

if command -v docker >/dev/null 2>&1; then
  sudo docker system prune -af >/dev/null 2>&1 || true
fi
sudo apt-get clean

free_kib() {
  df -k --output=avail "$1" 2>/dev/null | tail -n 1 | tr -d ' ' || echo 0
}

root_free="$(free_kib /)"
mnt_free=0
if mountpoint -q /mnt; then
  mnt_free="$(free_kib /mnt)"
fi

if (( mnt_free > root_free )); then
  work_dir="/mnt/$WORK_NAME"
  swap_dir="$HOME"
else
  work_dir="$HOME/$WORK_NAME"
  swap_dir="/mnt"
  (( mnt_free > 0 )) || swap_dir="$HOME"
fi

sudo mkdir -p "$work_dir"
sudo chown "$(id -u):$(id -g)" "$work_dir"

if ! swapon --show=NAME --noheadings | grep -q digitalis-swap; then
  sudo fallocate -l 12G "$swap_dir/digitalis-swap" 2>/dev/null \
    || sudo dd if=/dev/zero of="$swap_dir/digitalis-swap" bs=1M count=12288 status=none
  sudo chmod 600 "$swap_dir/digitalis-swap"
  sudo mkswap "$swap_dir/digitalis-swap" >/dev/null
  sudo swapon "$swap_dir/digitalis-swap"
fi

# Soong's nsjail/genrule sandboxes use unprivileged user namespaces, which
# Ubuntu 24.04 restricts by default via AppArmor.
sudo sysctl -q -w kernel.apparmor_restrict_unprivileged_userns=0 || true

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "DIGITALIS_WORK=$work_dir" >> "$GITHUB_ENV"
else
  echo "DIGITALIS_WORK=$work_dir"
fi

echo "::group::Disk and memory after cleanup"
df -h / /mnt 2>/dev/null || df -h /
free -h
echo "::endgroup::"
