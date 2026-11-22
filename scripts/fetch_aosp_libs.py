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
"""Download pinned AOSP target_files archives and extract needed .so libraries.

Extracts:
- 19 x86_64 host framework libraries that libberberis_proxy_* link against.
- 7 unproxied arm64 guest libraries (ICU, BoringSSL, SQLite) from the matching
  AOSP arm64 release build.

Each target_files archive is verified against its pinned size and SHA-256 and
deleted immediately after extraction to keep peak runner disk usage low.
"""

import argparse
import hashlib
import io
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lock  # noqa: E402

EM_X86_64 = 0x3E
EM_AARCH64 = 0xB7
EROFS_MAGIC = 0xE0F5E1E2
EXT4_MAGIC = 0xEF53

# x86_64 framework libraries linked by libberberis_proxy_*. Must match
# device/prebuilts/Android.bp.
FRAMEWORK_LIBS = {
    "libEGL.so": None,
    "libGLESv1_CM.so": None,
    "libGLESv2.so": None,
    "libGLESv3.so": None,
    "libOpenMAXAL.so": None,
    "libOpenSLES.so": None,
    "libaaudio.so": None,
    "libamidi.so": None,
    "libandroid.so": None,
    "libandroid_runtime.so": None,
    "libbinder_ndk.so": None,
    "libcamera2ndk.so": None,
    "libjnigraphics.so": None,
    "libmediandk.so": None,
    "libnativehelper.so": "com.android.art",
    "libnativewindow.so": None,
    "libneuralnetworks.so": "com.android.neuralnetworks",
    "libvulkan.so": None,
    "libwebviewchromium_plat_support.so": None,
}

# Pure AOSP arm64 guest libraries not replaced by NativeBridge proxies.
GUEST_LIBS_FROM_IMAGE = {
    "libandroidicu.so": "com.android.i18n",
    "libicu.so": "com.android.i18n",
    "libicui18n.so": "com.android.i18n",
    "libicuuc.so": "com.android.i18n",
    "libcrypto.so": "com.android.conscrypt",
    "libssl.so": "com.android.conscrypt",
    "libsqlite.so": None,
}


def elf_machine(data):
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return None
    return int.from_bytes(data[18:20], "little")


def _extract_apex_payload(apex_bytes):
    with zipfile.ZipFile(io.BytesIO(apex_bytes)) as zf:
        names = zf.namelist()
        if "original_apex" in names:
            return _extract_apex_payload(zf.read("original_apex"))
        return zf.read("apex_payload.img")


def _payload_fs_type(image):
    if len(image) >= 1028 and int.from_bytes(image[1024:1028], "little") == EROFS_MAGIC:
        return "erofs"
    if len(image) >= 1082 and int.from_bytes(image[1080:1082], "little") == EXT4_MAGIC:
        return "ext4"
    return None


def read_from_apex_payload(image, rel_path):
    fs_type = _payload_fs_type(image)
    with tempfile.TemporaryDirectory() as tmp:
        payload_file = Path(tmp) / "payload.img"
        payload_file.write_bytes(image)
        if fs_type == "ext4":
            out_file = Path(tmp) / "extracted"
            subprocess.run(
                ["debugfs", "-R", f"dump /{rel_path} {out_file}", str(payload_file)],
                check=True,
                capture_output=True,
            )
            if not out_file.is_file() or out_file.stat().st_size == 0:
                raise FileNotFoundError(f"/{rel_path} not found in ext4 APEX payload")
            return out_file.read_bytes()
        if fs_type == "erofs":
            tree = Path(tmp) / "tree"
            tree.mkdir()
            subprocess.run(
                ["fsck.erofs", f"--extract={tree}", str(payload_file)],
                check=True,
                capture_output=True,
            )
            target = tree / rel_path
            if not target.is_file():
                raise FileNotFoundError(f"{rel_path} not found in erofs APEX payload")
            return target.read_bytes()
    raise SystemExit("unsupported APEX payload filesystem (expected ext4 or erofs)")


class TargetFilesReader:
    """Extracts shared libraries from SYSTEM/lib64 or SYSTEM/apex inside target_files.zip."""

    def __init__(self, zip_path, payload_reader=read_from_apex_payload):
        self._zf = zipfile.ZipFile(zip_path)
        self._names = set(self._zf.namelist())
        self._payload_reader = payload_reader
        self._apex_cache = {}

    def close(self):
        self._zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _apex_payload(self, apex_name):
        if apex_name not in self._apex_cache:
            for ext in (".apex", ".capex"):
                member = f"SYSTEM/apex/{apex_name}{ext}"
                if member in self._names:
                    self._apex_cache[apex_name] = _extract_apex_payload(self._zf.read(member))
                    break
            else:
                raise SystemExit(f"APEX {apex_name} not found in {self._zf.filename}")
        return self._apex_cache[apex_name]

    def read_library(self, lib_name, fallback_apex=None):
        member = f"SYSTEM/lib64/{lib_name}"
        if member in self._names:
            info = self._zf.getinfo(member)
            if not stat.S_ISLNK(info.external_attr >> 16):
                return self._zf.read(member)
            link_target = self._zf.read(member).decode("utf-8")
            m = re.match(r"^/apex/([^/]+)/(.+)$", link_target)
            if not m:
                raise SystemExit(f"{member} -> {link_target}: unexpected symlink target")
            return self._payload_reader(self._apex_payload(m.group(1)), m.group(2))
        if fallback_apex is None:
            raise SystemExit(f"{member} not found in {self._zf.filename}")
        return self._payload_reader(self._apex_payload(fallback_apex), f"lib64/{lib_name}")


def extract_libraries(zip_path, lib_map, expected_machine, out_dir, payload_reader=read_from_apex_payload):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with TargetFilesReader(zip_path, payload_reader=payload_reader) as reader:
        for lib_name, apex in lib_map.items():
            data = reader.read_library(lib_name, apex)
            got = elf_machine(data)
            if got != expected_machine:
                raise SystemExit(
                    f"{lib_name}: expected ELF machine 0x{expected_machine:x}, got {got!r}"
                )
            (out / lib_name).write_bytes(data)
    print(f"extracted {len(lib_map)} libraries to {out}")


def download_verified(image_spec, dest_path):
    url = (
        f"https://ci.android.com/builds/submitted/{image_spec['build_id']}"
        f"/{image_spec['target']}/latest/raw/{image_spec['file']}"
    )
    print(f"downloading {image_spec['file']} (build {image_spec['build_id']})", flush=True)
    subprocess.run(
        ["curl", "-fsSL", "--retry", "8", "--retry-all-errors", "-o", str(dest_path), url],
        check=True,
    )
    actual_size = dest_path.stat().st_size
    if actual_size != image_spec["size"]:
        raise SystemExit(
            f"{image_spec['file']}: size mismatch (got {actual_size}, expected {image_spec['size']})"
        )
    digest = hashlib.sha256()
    with dest_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha = digest.hexdigest()
    if actual_sha != image_spec["sha256"]:
        raise SystemExit(
            f"{image_spec['file']}: sha256 mismatch (got {actual_sha}, expected {image_spec['sha256']})"
        )
    print(f"verified {image_spec['file']} ({actual_sha[:12]})", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--android", required=True, help="android16 or android17")
    ap.add_argument("--lock", type=Path, default=lock.DEFAULT_LOCK)
    ap.add_argument("--framework-out", type=Path, required=True)
    ap.add_argument("--guest-out", type=Path, required=True)
    ap.add_argument("--keep-archives", action="store_true")
    ap.add_argument("--work-dir", type=Path, help="directory for temporary archive downloads")
    args = ap.parse_args(argv)

    version = lock.get_version(lock.load_lock(args.lock), args.android)
    images = version["aosp_images"]

    tmp_ctx = tempfile.TemporaryDirectory(dir=args.work_dir)
    try:
        tmp_dir = Path(tmp_ctx.name)
        for arch, lib_map, machine, out_dir in (
            ("x86_64", FRAMEWORK_LIBS, EM_X86_64, args.framework_out),
            ("arm64", GUEST_LIBS_FROM_IMAGE, EM_AARCH64, args.guest_out),
        ):
            spec = images[arch]
            archive = tmp_dir / spec["file"]
            download_verified(spec, archive)
            extract_libraries(archive, lib_map, machine, out_dir)
            if not args.keep_archives:
                archive.unlink()
    finally:
        if not args.keep_archives:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    main()
