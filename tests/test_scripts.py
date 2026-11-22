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
"""Unit tests for digitalis-prebuilts configuration and scripts."""

import io
import json
import re
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_ninja_inputs  # noqa: E402
import fetch_aosp_libs  # noqa: E402
import lock  # noqa: E402
import package_bundle  # noqa: E402
import parse_issue_trigger  # noqa: E402
import verify_bundle  # noqa: E402


def fake_elf(machine):
    buf = bytearray(64)
    buf[0:4] = b"\x7fELF"
    buf[18:20] = machine.to_bytes(2, "little")
    return bytes(buf)


SAMPLE_CONFIG_MK = """
BERBERIS_DISTRIBUTION_ARTIFACTS_ARM64 := \\
    system/bin/arm64/linker64 \\
    system/bin/berberis_program_runner_arm64 \\
    system/etc/ld.config.arm64.txt \\
    system/lib64/libberberis_arm64.so \\
    system/lib64/libberberis_proxy_libvulkan.so \\
    system/lib64/arm64/libc.so \\
    system/lib64/arm64/libicuuc.so \\
    system/lib64/arm64/libsqlite.so
"""


class ConfigConsistencyTests(unittest.TestCase):
    def test_versions_json_valid(self):
        data = lock.load_lock()
        names = [v["android"] for v in data["versions"]]
        self.assertIn("android16", names)
        self.assertIn("android17", names)

    def test_prebuilts_bp_matches_framework_libs(self):
        bp = (REPO_ROOT / "device/prebuilts/Android.bp").read_text(encoding="utf-8")
        bp_names = set(re.findall(r'srcs:\s*\["x86_64/([^"]+)"\]', bp))
        self.assertEqual(bp_names, set(fetch_aosp_libs.FRAMEWORK_LIBS))

    def test_headers_include_dirs_match_headers_bp(self):
        inc_txt = (REPO_ROOT / "device/headers/include-dirs.txt").read_text(encoding="utf-8")
        listed = {
            line.strip()
            for line in inc_txt.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        bp = (REPO_ROOT / "device/headers/Android.bp").read_text(encoding="utf-8")
        exported = set(re.findall(r'"((?:art|frameworks|libnativehelper)/[^"]+)"', bp))
        self.assertEqual(listed, exported)

    def test_resolve_sparse_tokens(self):
        proj = {"path": "prebuilts/clang/host/linux-x86", "cone": ["soong", "{clang}"]}
        mode, entries = lock.resolve_sparse(proj, clang_versions=["clang-r547379"])
        self.assertEqual(mode, "cone")
        self.assertEqual(entries, ["soong", "clang-r547379"])


class PackagingAndVerificationTests(unittest.TestCase):
    def test_targets_exclude_image_guest_libs(self):
        with tempfile.TemporaryDirectory() as tmp:
            mk = Path(tmp) / "berberis_config.mk"
            mk.write_text(SAMPLE_CONFIG_MK, encoding="utf-8")
            targets = package_bundle.list_build_targets(mk, "out/target/product/digitalis_prebuilts")
            self.assertIn("out/target/product/digitalis_prebuilts/system/lib64/libberberis_arm64.so", targets)
            self.assertIn("out/target/product/digitalis_prebuilts/system/lib64/arm64/libc.so", targets)
            self.assertNotIn("out/target/product/digitalis_prebuilts/system/lib64/arm64/libicuuc.so", targets)
            self.assertNotIn("out/target/product/digitalis_prebuilts/system/lib64/arm64/libsqlite.so", targets)

    def test_package_and_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mk = tmp / "berberis_config.mk"
            mk.write_text(SAMPLE_CONFIG_MK, encoding="utf-8")

            prod = tmp / "product_out"
            built_files = {
                "system/bin/arm64/linker64": fake_elf(fetch_aosp_libs.EM_AARCH64),
                "system/bin/berberis_program_runner_arm64": fake_elf(fetch_aosp_libs.EM_X86_64),
                "system/etc/ld.config.arm64.txt": b"dir.system = /system/bin/arm64\n",
                "system/lib64/libberberis_arm64.so": fake_elf(fetch_aosp_libs.EM_X86_64),
                "system/lib64/libberberis_proxy_libvulkan.so": fake_elf(fetch_aosp_libs.EM_X86_64),
                "system/lib64/arm64/libc.so": fake_elf(fetch_aosp_libs.EM_AARCH64),
            }
            for rel, content in built_files.items():
                p = prod / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)

            guest = tmp / "guest_libs"
            guest.mkdir()
            (guest / "libicuuc.so").write_bytes(fake_elf(fetch_aosp_libs.EM_AARCH64))
            (guest / "libsqlite.so").write_bytes(fake_elf(fetch_aosp_libs.EM_AARCH64))

            sources = tmp / "sources.json"
            sources.write_text(json.dumps({
                "projects": {
                    "frameworks/libs/binary_translation": {"commit": "5d66dd11" + "0" * 32},
                    "digitalis": {"commit": "8ca2d135" + "0" * 32},
                }
            }), encoding="utf-8")

            dist = tmp / "dist"
            bundle_dir, tarball, sha_path = package_bundle.package_bundle(
                config_mk=mk,
                product_out=prod,
                guest_libs_dir=guest,
                out_dir=dist,
                android="android17",
                build_date="2026-11-22",
                username="digitalis-ci",
                sources_json=sources,
                license_file=REPO_ROOT / "LICENSE",
            )
            self.assertEqual(tarball.name, "digitalis-prebuilts-android17-2026-11-22-5d66dd11.tar.gz")
            self.assertTrue(sha_path.is_file())
            errors, count = verify_bundle.verify_bundle_dir(bundle_dir, mk, check_symbols=False)
            self.assertEqual(errors, [])
            self.assertEqual(count, 8)
            self.assertEqual(
                verify_bundle.main([str(tarball), "--config-mk", str(mk), "--skip-symbols"]),
                0,
            )

    def test_target_files_symlink_and_apex_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "target_files.zip"
            apex_buf = io.BytesIO()
            with zipfile.ZipFile(apex_buf, "w") as az:
                az.writestr("apex_payload.img", b"FAKE_PAYLOAD")

            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("SYSTEM/lib64/libvulkan.so", fake_elf(fetch_aosp_libs.EM_X86_64))
                link_info = zipfile.ZipInfo("SYSTEM/lib64/libnativehelper.so")
                link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(link_info, b"/apex/com.android.art/lib64/libnativehelper.so")
                zf.writestr("SYSTEM/apex/com.android.art.apex", apex_buf.getvalue())

            out_dir = Path(tmp) / "extracted"
            fetch_aosp_libs.extract_libraries(
                zpath,
                {"libvulkan.so": None, "libnativehelper.so": "com.android.art"},
                fetch_aosp_libs.EM_X86_64,
                out_dir,
                payload_reader=lambda _img, _path: fake_elf(fetch_aosp_libs.EM_X86_64),
            )
            self.assertTrue((out_dir / "libvulkan.so").is_file())
            self.assertTrue((out_dir / "libnativehelper.so").is_file())


class NinjaInputCheckTests(unittest.TestCase):
    def test_leaf_detection(self):
        dot = [
            '"0x1" [label="src/a.cc"]',
            '"0x2" [label="out/a.o"]',
            '"0x3" [label="clang", shape=ellipse]',
            '"0x1" -> "0x3"',
            '"0x3" -> "0x2"',
        ]
        self.assertEqual(check_ninja_inputs.find_leaf_inputs(dot), ["src/a.cc"])


class IssueTriggerParsingTests(unittest.TestCase):
    def test_rejects_unauthorized_author(self):
        res = parse_issue_trigger.parse_event(
            event_name="issues",
            author_association="NONE",
            title="[Build] android17",
            body="branch: android17",
        )
        self.assertFalse(res["should_build"])
        self.assertIn("not authorized", res["reason"])

    def test_parses_issue_form(self):
        body = """### Target Android

android17

### Translator branch or commit (optional)

fix/my-branch

### Publish GitHub Release

- [x] Publish a GitHub Release with the built tarballs

### Release tag (optional)

v2026.11.22
"""
        res = parse_issue_trigger.parse_event(
            event_name="issues",
            author_association="OWNER",
            title="[Build] test fix",
            body=body,
        )
        self.assertTrue(res["should_build"])
        self.assertEqual(res["versions"], ["android17"])
        self.assertEqual(res["bt_ref"], "fix/my-branch")
        self.assertTrue(res["publish_release"])
        self.assertEqual(res["release_tag"], "v2026.11.22")
        self.assertTrue(res["use_branch_head"])

    def test_parses_key_value_branch_as_target(self):
        res = parse_issue_trigger.parse_event(
            event_name="issues",
            author_association="OWNER",
            title="Build prebuilts",
            body="branch: android16\nrelease: true\n",
        )
        self.assertTrue(res["should_build"])
        self.assertEqual(res["versions"], ["android16"])
        self.assertEqual(res["bt_ref"], "android16")
        self.assertTrue(res["publish_release"])

    def test_parses_comment_slash_command(self):
        res = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="COLLABORATOR",
            title="Some tracking issue",
            body="notes",
            comment_body="/build android17 branch=port17 release tag=v2026.11.23",
        )
        self.assertTrue(res["should_build"])
        self.assertEqual(res["versions"], ["android17"])
        self.assertEqual(res["bt_ref"], "port17")
        self.assertTrue(res["publish_release"])
        self.assertEqual(res["release_tag"], "v2026.11.23")

    def test_comment_without_slash_command_is_ignored(self):
        res = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="OWNER",
            title="Build android17",
            body="branch: android17",
            comment_body="Thanks, checking the logs now.",
        )
        self.assertFalse(res["should_build"])

    def test_non_build_issue_is_ignored(self):
        res = parse_issue_trigger.parse_event(
            event_name="issues",
            author_association="OWNER",
            title="Question about documentation",
            body="How do I check SHA256SUMS?",
        )
        self.assertFalse(res["should_build"])

    def test_freeform_build_both_versions(self):
        res = parse_issue_trigger.parse_event(
            event_name="issues",
            author_association="OWNER",
            title="Build prebuilts for android16 and android17",
            body="",
        )
        self.assertTrue(res["should_build"])
        self.assertEqual(res["versions"], ["android17", "android16"])
        self.assertEqual(res["bt_ref"], "")
        self.assertTrue(res["use_branch_head"])

    def test_rejects_single_custom_ref_with_multiple_versions(self):
        res = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="OWNER",
            comment_body="/build all branch=fix/only-17",
        )
        self.assertFalse(res["should_build"])
        self.assertIn("multiple Android versions", res["reason"])


if __name__ == "__main__":
    unittest.main()
