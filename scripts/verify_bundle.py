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
"""Verify a Digitalis prebuilt bundle directory or .tar.gz archive."""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_aosp_libs  # noqa: E402
import package_bundle  # noqa: E402


def find_readelf(tree_top=None):
    if tree_top:
        llvm_readelf = (
            Path(tree_top)
            / "prebuilts/clang/host/linux-x86/llvm-binutils-stable/llvm-readelf"
        )
        if llvm_readelf.is_file():
            return str(llvm_readelf)
    for tool in ("llvm-readelf", "readelf"):
        found = shutil.which(tool)
        if found:
            return found
    return None


def verify_bundle_dir(bundle_dir, config_mk, tree_top=None, check_symbols=True):
    bundle_dir = Path(bundle_dir)
    errors = []

    def fail(msg):
        errors.append(msg)

    artifacts = package_bundle.list_artifacts(config_mk)
    for rel in artifacts:
        f = bundle_dir / rel
        if not f.is_file():
            fail(f"missing artifact: {rel}")
            continue
        data = f.read_bytes()
        if not data:
            fail(f"zero-size artifact: {rel}")
            continue
        if rel.startswith("system/etc/"):
            continue
        machine = fetch_aosp_libs.elf_machine(data)
        if rel.startswith(package_bundle.GUEST_PREFIXES):
            if machine != fetch_aosp_libs.EM_AARCH64:
                fail(f"{rel}: expected AArch64 (0xb7), got {machine!r}")
        elif rel.startswith(("system/lib64/", "system/bin/")):
            if machine != fetch_aosp_libs.EM_X86_64:
                fail(f"{rel}: expected x86-64 (0x3e), got {machine!r}")

    nb_so = bundle_dir / "system/lib64/libberberis_arm64.so"
    if check_symbols and nb_so.is_file():
        readelf = find_readelf(tree_top)
        if not readelf:
            fail("neither llvm-readelf nor readelf found to verify NativeBridgeItf")
        else:
            proc = subprocess.run(
                [readelf, "--dyn-syms", str(nb_so)],
                capture_output=True,
                text=True,
                check=False,
            )
            if "NativeBridgeItf" not in proc.stdout:
                fail("libberberis_arm64.so does not export NativeBridgeItf")

    mk_file = bundle_dir / "digitalis-prebuilts.mk"
    if not mk_file.is_file():
        fail("digitalis-prebuilts.mk missing")
    else:
        mk_text = mk_file.read_text(encoding="utf-8")
        if "ro.dalvik.vm.native.bridge=libberberis_arm64.so" not in mk_text:
            fail("digitalis-prebuilts.mk missing native-bridge property")
        for rel in artifacts:
            if f"$(LOCAL_PATH)/{rel}:{rel}" not in mk_text:
                fail(f"digitalis-prebuilts.mk missing PRODUCT_COPY_FILES for {rel}")
            if f"PRODUCT_ARTIFACT_PATH_REQUIREMENT_ALLOWED_LIST += {rel}" not in mk_text:
                fail(f"digitalis-prebuilts.mk missing allowlist entry for {rel}")

    sums_file = bundle_dir / "SHA256SUMS"
    if not sums_file.is_file():
        fail("SHA256SUMS missing")
    else:
        recorded = {}
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, rel = line.split("  ", 1)
            recorded[rel] = digest
            target = bundle_dir / rel
            if not target.is_file():
                fail(f"SHA256SUMS lists missing file: {rel}")
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != digest:
                fail(f"SHA256SUMS mismatch for {rel}")
        if set(recorded) != set(artifacts):
            fail("SHA256SUMS entries do not match BERBERIS_DISTRIBUTION_ARTIFACTS_ARM64")

    manifest_file = bundle_dir / "MANIFEST.txt"
    if not manifest_file.is_file():
        fail("MANIFEST.txt missing")
    else:
        manifest_text = manifest_file.read_text(encoding="utf-8")
        for key in ("android-release:", "build-date:", "build-username:", "binary_translation-sha:", "artifact-count:"):
            if key not in manifest_text:
                fail(f"MANIFEST.txt missing {key}")

    if not (bundle_dir / "README.md").is_file():
        fail("README.md missing")

    return errors, len(artifacts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", type=Path, help="bundle directory or .tar.gz archive")
    ap.add_argument("--config-mk", type=Path, required=True)
    ap.add_argument("--top", type=Path, help="optional AOSP tree top for llvm-readelf")
    ap.add_argument("--skip-symbols", action="store_true")
    args = ap.parse_args(argv)

    if args.bundle.is_dir():
        errors, count = verify_bundle_dir(
            args.bundle, args.config_mk, tree_top=args.top, check_symbols=not args.skip_symbols
        )
    else:
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(args.bundle, "r:gz") as tf:
                tf.extractall(tmp, filter="data")
            extracted = Path(tmp) / "digitalis-prebuilts"
            errors, count = verify_bundle_dir(
                extracted, args.config_mk, tree_top=args.top, check_symbols=not args.skip_symbols
            )

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1
    print(f"PASS: verified Digitalis prebuilt bundle ({count} artifacts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
