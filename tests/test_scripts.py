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
import generate_release_notes  # noqa: E402
import handle_issue_command  # noqa: E402
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
                "manifest": {"commit": "9cd17507" + "0" * 32},
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
            manifest_text = (bundle_dir / "MANIFEST.txt").read_text(encoding="utf-8")
            self.assertIn("release-id:             android17-2026-11-22-5d66dd11", manifest_text)
            self.assertIn("manifest-sha:           9cd17507", manifest_text)
            errors, count = verify_bundle.verify_bundle_dir(bundle_dir, mk, check_symbols=False)
            self.assertEqual(errors, [])
            self.assertEqual(count, 8)
            self.assertEqual(
                verify_bundle.main([str(tarball), "--config-mk", str(mk), "--skip-symbols"]),
                0,
            )

    def test_branch_label_formatting(self):
        self.assertEqual(package_bundle.build_branch_label("android17", ""), "android17")
        self.assertEqual(package_bundle.build_branch_label("android17", "android17"), "android17")
        self.assertEqual(package_bundle.build_branch_label("android17", "5d66dd11" * 5), "android17")
        self.assertEqual(
            package_bundle.build_branch_label("android17", "fix/guest-error"),
            "android17-fix-guest-error",
        )
        self.assertEqual(
            package_bundle.build_branch_label("android17", "android17-dev"),
            "android17-dev",
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

    def test_rebuild_and_release_comment_inherit_parent_issue_fields(self):
        parent_body = "android: android17\nbranch: fix/foo\nrelease: true\n"
        res = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="OWNER",
            title="[Build] custom fix",
            body=parent_body,
            comment_body="/rebuild",
        )
        self.assertTrue(res["should_build"])
        self.assertEqual(res["versions"], ["android17"])
        self.assertEqual(res["bt_ref"], "fix/foo")
        self.assertTrue(res["publish_release"])

        res_no_rel = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="OWNER",
            title="[Build] custom fix",
            body=parent_body,
            comment_body="/rebuild release=false",
        )
        self.assertTrue(res_no_rel["should_build"])
        self.assertEqual(res_no_rel["versions"], ["android17"])
        self.assertEqual(res_no_rel["bt_ref"], "fix/foo")
        self.assertFalse(res_no_rel["publish_release"])

        res_rel = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="OWNER",
            title="[Build] android16",
            body="android: android16\nrelease: false\n",
            comment_body="/release",
        )
        self.assertTrue(res_rel["should_build"])
        self.assertEqual(res_rel["versions"], ["android16"])
        self.assertTrue(res_rel["publish_release"])

    def test_multiline_build_comment_with_kv_template(self):
        res = parse_issue_trigger.parse_event(
            event_name="issue_comment",
            author_association="OWNER",
            title="Trigger issue",
            body="",
            comment_body="/build\nandroid: android17\nbranch: fix/my-change\nrelease: true\n",
        )
        self.assertTrue(res["should_build"])
        self.assertEqual(res["versions"], ["android17"])
        self.assertEqual(res["bt_ref"], "fix/my-change")
        self.assertTrue(res["publish_release"])

    def test_parse_control_commands(self):
        self.assertEqual(
            parse_issue_trigger.parse_control_command(
                author_association="NONE",
                comment_body="/cancel",
            )["action"],
            "none",
        )
        self.assertEqual(
            parse_issue_trigger.parse_control_command(
                author_association="OWNER",
                comment_body="Please do not /cancel this",
            )["action"],
            "none",
        )
        cmd = parse_issue_trigger.parse_control_command(
            author_association="OWNER",
            comment_body="/cancel\nWrong branch, will re-trigger with android17.",
        )
        self.assertEqual(cmd["action"], "cancel")
        self.assertFalse(cmd["cancel_all"])
        self.assertEqual(cmd["run_ids"], [])

        cmd_stop = parse_issue_trigger.parse_control_command(
            author_association="COLLABORATOR",
            comment_body="/stop 12345 https://github.com/DigitalisX64/digitalis-prebuilts/actions/runs/67890",
        )
        self.assertEqual(cmd_stop["action"], "cancel")
        self.assertEqual(cmd_stop["run_ids"], [12345, 67890])

        cmd_all = parse_issue_trigger.parse_control_command(
            author_association="MEMBER",
            comment_body="/abort all",
        )
        self.assertEqual(cmd_all["action"], "cancel")
        self.assertTrue(cmd_all["cancel_all"])

        self.assertEqual(
            parse_issue_trigger.parse_control_command(
                author_association="OWNER",
                comment_body="/status",
            )["action"],
            "status",
        )
        self.assertEqual(
            parse_issue_trigger.parse_control_command(
                author_association="OWNER",
                comment_body="/help",
            )["action"],
            "help",
        )


class IssueCommandExecutionTests(unittest.TestCase):
    def test_select_runs_to_cancel_isolates_issue_numbers(self):
        runs = [
            {
                "id": 101,
                "status": "in_progress",
                "display_title": "Build prebuilts (issue #5)",
                "html_url": "https://github.com/org/repo/actions/runs/101",
            },
            {
                "id": 102,
                "status": "queued",
                "display_title": "Build prebuilts (issue #50)",
                "html_url": "https://github.com/org/repo/actions/runs/102",
            },
            {
                "id": 103,
                "status": "completed",
                "display_title": "Build prebuilts (issue #5)",
                "html_url": "https://github.com/org/repo/actions/runs/103",
            },
            {
                "id": 104,
                "status": "queued",
                "display_title": "Legacy run title",
                "html_url": "https://github.com/org/repo/actions/runs/104",
            },
        ]
        comment_ids = handle_issue_command.extract_run_ids_from_comments(
            [{"body": "Workflow run: https://github.com/org/repo/actions/runs/104"}]
        )
        matched = handle_issue_command.select_runs_to_cancel(
            runs, issue_number=5, comment_run_ids=comment_ids
        )
        self.assertEqual([r["id"] for r in matched], [101, 104])

        matched_all = handle_issue_command.select_runs_to_cancel(
            runs, issue_number=5, cancel_all=True
        )
        self.assertEqual([r["id"] for r in matched_all], [101, 102, 104])

        matched_explicit = handle_issue_command.select_runs_to_cancel(
            runs, issue_number=5, explicit_ids=[102]
        )
        self.assertEqual([r["id"] for r in matched_explicit], [102])

    def test_execute_cancel_and_status_post_expected_comments(self):
        class FakeClient:
            def __init__(self):
                self.cancelled = []
                self.posted = []

            def list_workflow_runs(self):
                return [
                    {
                        "id": 201,
                        "status": "in_progress",
                        "display_title": "Build prebuilts (issue #7)",
                        "html_url": "https://github.com/org/repo/actions/runs/201",
                    },
                    {
                        "id": 200,
                        "status": "completed",
                        "conclusion": "success",
                        "display_title": "Build prebuilts (issue #7)",
                        "html_url": "https://github.com/org/repo/actions/runs/200",
                    },
                ]

            def list_issue_comments(self, _issue_number):
                return []

            def cancel_workflow_run(self, run_id):
                self.cancelled.append(run_id)
                return True, "cancellation requested"

            def post_issue_comment(self, issue_number, body):
                self.posted.append((issue_number, body))

        client = FakeClient()
        handle_issue_command.execute_command(
            {"action": "cancel", "cancel_all": False, "run_ids": []},
            issue_number=7,
            client=client,
        )
        self.assertEqual(client.cancelled, [201])
        self.assertIn("[#201]", client.posted[-1][1])
        self.assertIn("cancelling", client.posted[-1][1])

        handle_issue_command.execute_command(
            {"action": "status"},
            issue_number=7,
            client=client,
        )
        self.assertIn("Active build run(s) for issue #7", client.posted[-1][1])
        self.assertIn("[#200]", client.posted[-1][1])

        handle_issue_command.execute_command(
            {"action": "help"},
            issue_number=7,
            client=client,
        )
        self.assertIn("/cancel", client.posted[-1][1])


class ReleaseNotesTests(unittest.TestCase):
    def _sample_manifest(self, branch="android17", bt="5d66dd11", dig="8ca2d135", man="9cd17507"):
        return {
            "release-id": f"{branch}-2026-11-22-{bt}",
            "branch": branch,
            "android-release": "android17",
            "build-date": "2026-11-22",
            "binary_translation-sha": bt,
            "digitalis-sha": dig,
            "manifest-sha": man,
            "artifact-count": "74",
        }

    def _sample_sources(self, bt="5d66dd11" + "0" * 32, dig="8ca2d135" + "0" * 32, man="9cd17507" + "0" * 32, img="16373615"):
        return {
            "android": "android17",
            "aosp_images": {
                "x86_64": {"build_id": img},
                "arm64": {"build_id": img},
            },
            "manifest": {
                "url": "https://github.com/DigitalisX64/manifest",
                "commit": man,
            },
            "superproject": {"commit": "d6985d7e" + "0" * 32},
            "clang": ["clang-r547379"],
            "rust": ["1.82.0"],
            "projects": {
                "frameworks/libs/binary_translation": {
                    "url": "https://github.com/DigitalisX64/platform_frameworks_libs_binary_translation",
                    "commit": bt,
                },
                "digitalis": {
                    "url": "https://github.com/DigitalisX64/digitalis",
                    "commit": dig,
                },
            },
        }

    def test_find_previous_release_filters_branch_drafts_and_current_tag(self):
        releases = [
            {
                "tag_name": "android17-2026-11-22-5d66dd11",
                "draft": False,
                "published_at": "2026-11-22T10:00:00Z",
                "body": "- **Branch:** `android17`\n",
            },
            {
                "tag_name": "android17-2026-11-21-ffffffff",
                "draft": True,
                "published_at": "2026-11-21T12:00:00Z",
                "body": "- **Branch:** `android17`\n",
            },
            {
                "tag_name": "android17-fix-foo-2026-11-21-eeeeeeee",
                "draft": False,
                "published_at": "2026-11-21T11:00:00Z",
                "body": "- **Branch:** `android17-fix-foo`\n",
            },
            {
                "tag_name": "android16-2026-11-21-246557a3",
                "draft": False,
                "published_at": "2026-11-21T10:00:00Z",
                "body": "- **Branch:** `android16`\n",
            },
            {
                "tag_name": "v2026.11.20",
                "draft": False,
                "published_at": "2026-11-20T10:00:00Z",
                "body": "- **Branch:** `android17`\n",
            },
            {
                "tag_name": "android17-2026-11-18-11111111",
                "draft": False,
                "published_at": "2026-11-18T10:00:00Z",
                "body": "",
            },
        ]
        prev = generate_release_notes.find_previous_release(
            releases, branch="android17", current_tag="android17-2026-11-22-5d66dd11"
        )
        self.assertIsNotNone(prev)
        self.assertEqual(prev["tag_name"], "v2026.11.20")

        prev_16 = generate_release_notes.find_previous_release(
            releases, branch="android16", current_tag="android16-2026-11-22-99999999"
        )
        self.assertEqual(prev_16["tag_name"], "android16-2026-11-21-246557a3")

    def test_render_initial_release_notes(self):
        notes = generate_release_notes.render_release_notes(
            tag="android17-2026-11-22-5d66dd11",
            tarball_name="digitalis-prebuilts-android17-2026-11-22-5d66dd11.tar.gz",
            manifest=self._sample_manifest(),
            curr_sources=self._sample_sources(),
            prev_release=None,
            commit_info_fn=lambda _slug, _ref: {
                "commit": {"message": "Keep the guest linker error\n\nLong body"}
            },
        )
        self.assertIn("## Changes", notes)
        self.assertIn("Initial prebuilt release for `android17`", notes)
        self.assertIn("Keep the guest linker error", notes)
        self.assertIn("- **Branch:** `android17`", notes)
        self.assertIn("- **AOSP images:** `16373615`", notes)

    def test_render_diff_release_notes_with_truncation_and_platform_update(self):
        prev_release = {
            "tag_name": "android17-2026-11-20-e680eae6",
            "html_url": "https://github.com/DigitalisX64/digitalis-prebuilts/releases/tag/android17-2026-11-20-e680eae6",
            "body": "- **Branch:** `android17`\n",
        }
        prev_sources = self._sample_sources(
            bt="e680eae6" + "0" * 32,
            dig="8ca2d135" + "0" * 32,
            man="9cd17507" + "0" * 32,
            img="16000000",
        )
        curr_sources = self._sample_sources()

        def fake_compare(slug, base, head):
            self.assertEqual(slug, "DigitalisX64/platform_frameworks_libs_binary_translation")
            self.assertTrue(base.startswith("e680eae6"))
            self.assertTrue(head.startswith("5d66dd11"))
            return {
                "status": "ahead",
                "total_commits": 3,
                "commits": [
                    {
                        "sha": "11111111" + "0" * 32,
                        "html_url": f"https://github.com/{slug}/commit/11111111",
                        "commit": {"message": "First older change"},
                    },
                    {
                        "sha": "22222222" + "0" * 32,
                        "html_url": f"https://github.com/{slug}/commit/22222222",
                        "commit": {"message": "Second change"},
                    },
                    {
                        "sha": "5d66dd11" + "0" * 32,
                        "html_url": f"https://github.com/{slug}/commit/5d66dd11",
                        "commit": {"message": "Keep the guest linker error\n\nDetails"},
                    },
                ],
            }

        notes = generate_release_notes.render_release_notes(
            tag="android17-2026-11-22-5d66dd11",
            tarball_name="digitalis-prebuilts-android17-2026-11-22-5d66dd11.tar.gz",
            manifest=self._sample_manifest(),
            curr_sources=curr_sources,
            prev_release=prev_release,
            prev_sources=prev_sources,
            compare_fn=fake_compare,
            max_commits=2,
        )
        self.assertIn("Changes since [`android17-2026-11-20-e680eae6`]", notes)
        self.assertIn("### `frameworks/libs/binary_translation`", notes)
        # Newest commits come first; 3rd commit is summarized in overflow line.
        self.assertLess(notes.index("Keep the guest linker error"), notes.index("Second change"))
        self.assertNotIn("First older change", notes)
        self.assertIn("and 1 more commit(s)", notes)
        self.assertNotIn("### `digitalis`", notes)
        self.assertIn("AOSP pinned images: `16000000` → `16373615`", notes)

    def test_render_notes_fallback_to_body_shas_and_rebuild_message(self):
        prev_release = {
            "tag_name": "android17-2026-11-20-5d66dd11",
            "html_url": "https://github.com/DigitalisX64/digitalis-prebuilts/releases/tag/android17-2026-11-20-5d66dd11",
            "body": (
                "- **Branch:** `android17`\n"
                "- **`binary_translation`:** `5d66dd11`\n"
                "- **`digitalis`:** `8ca2d135`\n"
                "- **`manifest`:** `9cd17507`\n"
            ),
        }
        notes = generate_release_notes.render_release_notes(
            tag="android17-2026-11-22-5d66dd11",
            tarball_name="digitalis-prebuilts-android17-2026-11-22-5d66dd11.tar.gz",
            manifest=self._sample_manifest(),
            curr_sources=self._sample_sources(),
            prev_release=prev_release,
            prev_sources=None,
            compare_fn=lambda *_args: self.fail("compare should not be called when commits match"),
        )
        self.assertIn("Rebuild with the same pinned source commits", notes)

    def test_render_notes_degrades_gracefully_on_compare_failure(self):
        prev_release = {
            "tag_name": "android17-2026-11-20-e680eae6",
            "body": "- **Branch:** `android17`\n- **`binary_translation`:** `e680eae6`\n",
        }
        notes = generate_release_notes.render_release_notes(
            tag="android17-2026-11-22-5d66dd11",
            tarball_name="digitalis-prebuilts-android17-2026-11-22-5d66dd11.tar.gz",
            manifest=self._sample_manifest(),
            curr_sources=self._sample_sources(),
            prev_release=prev_release,
            compare_fn=lambda *_args: None,
        )
        self.assertIn("Updated `e680eae6` → `5d66dd11`", notes)
        self.assertIn("/compare/e680eae6...5d66dd11", notes)


if __name__ == "__main__":
    unittest.main()
