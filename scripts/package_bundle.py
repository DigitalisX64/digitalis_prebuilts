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
"""Parse berberis_config.mk and package the canonical Digitalis prebuilt bundle."""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_aosp_libs  # noqa: E402

ARTIFACTS_VAR = "BERBERIS_DISTRIBUTION_ARTIFACTS_ARM64"
GUEST_PREFIXES = ("system/lib64/arm64/", "system/bin/arm64/")

BUNDLE_README = """# Digitalis prebuilts — ARM64-to-x86_64 translator (binary-only)

This bundle lets an **x86_64 Android (AOSP) product** run **arm64-v8a-only** apps by
dropping in the prebuilt Digitalis translator — no need to build Berberis from source.
It contains binaries, configs, and one integration makefile (`digitalis-prebuilts.mk`).

See `MANIFEST.txt` for this bundle's Android version, build date, builder identity,
source revisions, and artifact count, `SOURCES.json` for every pinned upstream commit,
and `SHA256SUMS` for per-file integrity.

## Contents

```text
system/lib64/libberberis_arm64.so                 # NativeBridge translator (x86-64 host)
system/lib64/libberberis_exec_region.so
system/lib64/libberberis_proxy_*.so               # NDK proxy libraries (x86-64 host)
system/bin/berberis_program_runner_arm64          # arm64 program runners
system/bin/berberis_program_runner_binfmt_misc_arm64
system/lib64/arm64/*.so                           # arm64 guest libraries
system/bin/arm64/{app_process64,linker64}         # arm64 guest app_process + linker
system/etc/ld.config.arm64.txt                    # arm64 linker namespace config
system/etc/init/berberis.rc                       # binfmt_misc init rules
system/etc/binfmt_misc/arm64_{dyn,exe}
digitalis-prebuilts.mk                            # AOSP product integration makefile
```

## Integrate into an x86_64 AOSP product

1. Copy this directory into your AOSP tree (for example `vendor/digitalis/prebuilts/`).
2. Ensure your board configuration declares the ARM64 native-bridge ABI:

   ```makefile
   TARGET_NATIVE_BRIDGE_ARCH := arm64
   TARGET_NATIVE_BRIDGE_ARCH_VARIANT := armv8-a
   TARGET_NATIVE_BRIDGE_CPU_VARIANT := generic
   TARGET_NATIVE_BRIDGE_ABI := arm64-v8a
   ```

3. Inherit `digitalis-prebuilts.mk` from your x86_64 product makefile (after
   `runtime_libart.mk` so the `ro.dalvik.vm.native.bridge` property override wins):

   ```makefile
   $(call inherit-product, vendor/digitalis/prebuilts/digitalis-prebuilts.mk)
   ```

4. Build your product as usual (`m`). Match the bundle's Android release (`MANIFEST.txt`)
   to your AOSP tree; mixing bundles across Android versions is unsupported.

## Verify

```bash
sha256sum -c SHA256SUMS
readelf -h system/lib64/libberberis_arm64.so | grep Machine   # -> X86-64
readelf -h system/lib64/arm64/libc.so        | grep Machine   # -> AArch64
```

Digitalis is licensed under Apache-2.0 (see `LICENSE`).
"""


def parse_make_list(text, var_name):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(var_name)}\s*:=\s*(.*)$", line)
        if not m:
            continue
        words = []
        rest = m.group(1)
        while True:
            cont = rest.endswith("\\")
            words.extend(rest.rstrip("\\").split())
            if not cont or i + 1 >= len(lines):
                break
            i += 1
            rest = lines[i].strip()
        if words:
            return words
    raise SystemExit(f"variable {var_name} not found")


def list_artifacts(config_mk):
    items = parse_make_list(Path(config_mk).read_text(encoding="utf-8"), ARTIFACTS_VAR)
    bad = [a for a in items if not a.startswith("system/") or ".." in a.split("/")]
    if bad:
        raise SystemExit(f"invalid paths in {ARTIFACTS_VAR}: {bad}")
    return items


def image_guest_lib_name(rel_path):
    if rel_path.startswith("system/lib64/arm64/"):
        name = rel_path[len("system/lib64/arm64/"):]
        if name in fetch_aosp_libs.GUEST_LIBS_FROM_IMAGE:
            return name
    return None


def list_build_targets(config_mk, product_out):
    prefix = product_out.rstrip("/")
    return [f"{prefix}/{rel}" for rel in list_artifacts(config_mk) if not image_guest_lib_name(rel)]


def check_elf_arch(rel_path, data):
    if rel_path.startswith("system/etc/"):
        return
    machine = fetch_aosp_libs.elf_machine(data)
    if rel_path.startswith(GUEST_PREFIXES):
        if machine != fetch_aosp_libs.EM_AARCH64:
            raise SystemExit(f"{rel_path}: expected AArch64 ELF, got {machine!r}")
    elif rel_path.endswith(".so") or rel_path.startswith("system/bin/"):
        if machine != fetch_aosp_libs.EM_X86_64:
            raise SystemExit(f"{rel_path}: expected x86-64 ELF, got {machine!r}")


def render_prebuilts_mk(artifacts):
    lines = [
        "# Auto-generated by digitalis-prebuilts/scripts/package-bundle.py — do not edit.",
        "#",
        "# Binary-only Digitalis (ARM64->x86_64) translator for an x86_64 AOSP product.",
        "# Drop this directory into your tree (e.g. vendor/digitalis/prebuilts/) and add",
        "# to your x86_64 product makefile:",
        "#",
        "#   $(call inherit-product, vendor/digitalis/prebuilts/digitalis-prebuilts.mk)",
        "#",
        "",
        "LOCAL_PATH := $(call my-dir)",
        "",
        "# Native-bridge enablement (mirrors frameworks/libs/binary_translation/enable_arm64_to_x86_64.mk).",
        "PRODUCT_SYSTEM_PROPERTIES += \\",
        "    ro.dalvik.vm.native.bridge=libberberis_arm64.so \\",
        "    ro.dalvik.vm.isa.arm64=x86_64 \\",
        "    ro.enable.native.bridge.exec=1",
        "",
        "# Copy every prebuilt artifact into the system image at its canonical path.",
    ]
    lines.extend(f"PRODUCT_COPY_FILES += $(LOCAL_PATH)/{rel}:{rel}" for rel in artifacts)
    lines.append("")
    lines.append("# Allow these artifacts to live on /system.")
    lines.extend(f"PRODUCT_ARTIFACT_PATH_REQUIREMENT_ALLOWED_LIST += {rel}" for rel in artifacts)
    return "\n".join(lines) + "\n"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def package_bundle(
    config_mk,
    product_out,
    guest_libs_dir,
    out_dir,
    android,
    build_date,
    username,
    sources_json=None,
    license_file=None,
):
    artifacts = list_artifacts(config_mk)
    product_out = Path(product_out)
    guest_libs_dir = Path(guest_libs_dir)
    out_dir = Path(out_dir)
    bundle_dir = out_dir / "digitalis-prebuilts"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    missing = []
    checksum_lines = []
    from_build = 0
    from_image = 0
    for rel in artifacts:
        img_name = image_guest_lib_name(rel)
        src = guest_libs_dir / img_name if img_name else product_out / rel
        if not src.is_file():
            missing.append(f"{rel} ({'arm64 image' if img_name else 'build'})")
            continue
        data = src.read_bytes()
        if not data:
            missing.append(f"{rel} (empty)")
            continue
        check_elf_arch(rel, data)
        dst = bundle_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        if rel.startswith("system/bin/"):
            dst.chmod(0o755)
        checksum_lines.append((rel, sha256_bytes(data)))
        if img_name:
            from_image += 1
        else:
            from_build += 1

    if missing:
        raise SystemExit("missing artifacts:\n  " + "\n  ".join(missing))

    (bundle_dir / "digitalis-prebuilts.mk").write_text(
        render_prebuilts_mk(artifacts), encoding="utf-8"
    )
    checksum_lines.sort(key=lambda item: item[0])
    (bundle_dir / "SHA256SUMS").write_text(
        "".join(f"{digest}  {rel}\n" for rel, digest in checksum_lines),
        encoding="utf-8",
    )
    (bundle_dir / "README.md").write_text(BUNDLE_README, encoding="utf-8")

    bt_sha = "unknown"
    digitalis_sha = "unknown"
    if sources_json and Path(sources_json).is_file():
        src_data = json.loads(Path(sources_json).read_text(encoding="utf-8"))
        projs = src_data.get("projects", {})
        if "frameworks/libs/binary_translation" in projs:
            bt_sha = projs["frameworks/libs/binary_translation"]["commit"][:8]
        if "digitalis" in projs:
            digitalis_sha = projs["digitalis"]["commit"][:8]
        shutil.copy2(sources_json, bundle_dir / "SOURCES.json")

    if license_file and Path(license_file).is_file():
        shutil.copy2(license_file, bundle_dir / "LICENSE")

    manifest_lines = [
        "Digitalis binary distribution (ARM64 -> x86_64 translator)",
        f"android-release:        {android}",
        f"build-date:             {build_date}",
        f"build-username:         {username}",
        "target-product:         digitalis_prebuilts",
        f"binary_translation-sha: {bt_sha}",
        f"digitalis-sha:          {digitalis_sha}",
        f"artifact-count:         {len(artifacts)}",
        f"artifacts-from-build:   {from_build}",
        f"artifacts-from-image:   {from_image}",
    ]
    (bundle_dir / "MANIFEST.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    tarball_name = f"digitalis-prebuilts-{android}-{build_date}-{bt_sha}.tar.gz"
    tarball_path = out_dir / tarball_name
    with tarfile.open(tarball_path, "w:gz") as tf:
        tf.add(bundle_dir, arcname="digitalis-prebuilts")

    tar_sha = sha256_file(tarball_path)
    sha_path = out_dir / f"{tarball_name}.sha256"
    sha_path.write_text(f"{tar_sha}  {tarball_name}\n", encoding="utf-8")

    print(
        f"packaged {tarball_path} ({tarball_path.stat().st_size} bytes; "
        f"{from_build} built + {from_image} image guest libs)"
    )
    return bundle_dir, tarball_path, sha_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("targets", help="print Soong install paths to build")
    t.add_argument("--config-mk", type=Path, required=True)
    t.add_argument("--product-out", required=True)

    p = sub.add_parser("package", help="collect and tar.gz the canonical prebuilt bundle")
    p.add_argument("--config-mk", type=Path, required=True)
    p.add_argument("--product-out", type=Path, required=True)
    p.add_argument("--guest-libs", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--android", required=True)
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--username", default="digitalis-ci")
    p.add_argument("--sources", type=Path)
    p.add_argument("--license", type=Path)

    args = ap.parse_args(argv)
    if args.cmd == "targets":
        print("\n".join(list_build_targets(args.config_mk, args.product_out)))
    else:
        package_bundle(
            config_mk=args.config_mk,
            product_out=args.product_out,
            guest_libs_dir=args.guest_libs,
            out_dir=args.out,
            android=args.android,
            build_date=args.date,
            username=args.username,
            sources_json=args.sources,
            license_file=args.license,
        )


if __name__ == "__main__":
    main()
