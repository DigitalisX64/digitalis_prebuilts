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

free_gib() {
  local kib
  kib="$(df -k --output=avail "$1" 2>/dev/null | tail -n 1 | tr -d ' ' || echo 0)"
  echo $(( kib / 1024 / 1024 ))
}

root_free_gb="$(free_gib /)"
mnt_free_gb=0
if mountpoint -q /mnt; then
  mnt_free_gb="$(free_gib /mnt)"
fi

if (( mnt_free_gb > root_free_gb )); then
  work_dir="/mnt/$WORK_NAME"
  swap_candidates=("$HOME" "/mnt")
else
  work_dir="$HOME/$WORK_NAME"
  swap_candidates=("/mnt" "$HOME")
fi

sudo mkdir -p "$work_dir"
sudo chown "$(id -u):$(id -g)" "$work_dir"

# Top up swap to ~12 GiB total without exhausting a smaller /mnt partition.
existing_swap_gb="$(free -g | awk '/^Swap:/{print $2}')"
target_add_gb=$(( 12 - ${existing_swap_gb:-0} ))
if (( target_add_gb > 0 )) && ! swapon --show=NAME --noheadings | grep -q digitalis-swap; then
  for cand in "${swap_candidates[@]}"; do
    [[ -d "$cand" ]] || continue
    if [[ "$cand" == "/mnt" ]] && ! mountpoint -q /mnt; then
      continue
    fi
    cand_free_gb="$(free_gib "$cand")"
    # Keep at least 2 GiB headroom on non-work partition, or 25 GiB on work partition.
    if [[ "$work_dir" == "$cand"/* ]]; then
      reserve_gb=25
    else
      reserve_gb=2
    fi
    usable_gb=$(( cand_free_gb - reserve_gb ))
    if (( usable_gb < 2 )); then
      continue
    fi
    swap_gb="$target_add_gb"
    if (( swap_gb > usable_gb )); then
      swap_gb="$usable_gb"
    fi
    swap_file="$cand/digitalis-swap"
    echo "configuring ${swap_gb}G swap at $swap_file (${cand_free_gb}G free)"
    if sudo fallocate -l "${swap_gb}G" "$swap_file" 2>/dev/null \
      || { sudo rm -f "$swap_file"; sudo dd if=/dev/zero of="$swap_file" bs=1M count=$(( swap_gb * 1024 )) status=none; }; then
      sudo chmod 600 "$swap_file"
      if sudo mkswap "$swap_file" >/dev/null && sudo swapon "$swap_file"; then
        break
      fi
    fi
    sudo rm -f "$swap_file"
    echo "::warning::failed to enable ${swap_gb}G swap at $swap_file; continuing without extra swap"
  done
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
