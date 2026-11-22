# digitalis prebuilts

Automated GitHub Actions CI that builds and publishes binary-only
[Digitalis](https://github.com/DigitalisX64) ARM64-to-x86_64 translator prebuilt
bundles for **Android 16 (`android16`)** and **Android 17 (`android17`)** on
standard GitHub-hosted `ubuntu-24.04` runners.

> [!TIP]
> **Quick Trigger (Maintainers Only):**
> Open a [Build Digitalis Prebuilts issue](../../issues/new?template=build-prebuilts.yml),
> paste the trigger template below into any issue, or control everything with
> `/build`, `/release`, `/rebuild`, `/cancel`, and `/status` comments on the
> trigger issue.

## Triggering & Controlling Builds via Issues and Comments

### Copy-paste Issue Template

Create a new issue (or use the [**Build Digitalis Prebuilts** form](../../issues/new?template=build-prebuilts.yml))
with a body like:

```yaml
android: android17
branch: android17
release: true
```

Or build a custom branch / commit in `platform_frameworks_libs_binary_translation`
without publishing a release:

```yaml
android: android17
branch: fix/my-change
release: false
```

Or build both `android17` and `android16` and publish one release per branch:

```yaml
android: all
release: true
```

### Comment Commands (Everything Works via Comments)

Once you have any issue (including a dedicated trigger issue), repository
owners, members, and collaborators can trigger, re-run, inspect, and cancel
builds purely with comments:

| Comment command | Effect |
| --- | --- |
| `/build android17` | Build latest `android17` branch HEAD (workflow artifact only) |
| `/build android16` | Build latest `android16` branch HEAD (workflow artifact only) |
| `/build all` | Build both `android17` and `android16` |
| `/build android17 release` (or `/release android17`) | Build and publish a GitHub Release (`android17-<YYYY-MM-DD>-<bt_sha>`) |
| `/build all release` (or `/release all`) | Build both branches and publish one GitHub Release per branch |
| `/build android17 branch=<ref> release` | Build a custom branch, tag, or SHA in `platform_frameworks_libs_binary_translation` |
| `/build android17 release tag=<tag>` | Publish a single-branch release with a custom tag |
| `/build android17 pinned` | Build the commit pinned in `config/versions.json` instead of live branch HEAD |
| `/rebuild` (or bare `/build`) | Re-run the build inheriting the parent issue's target, branch, and release settings |
| `/cancel` (aliases: `/stop`, `/abort`) | Immediately cancel active or queued build runs triggered by this issue |
| `/cancel <run_id>` | Cancel a specific workflow run by ID (or Actions run URL) |
| `/cancel all` | Cancel all active `Build Digitalis Prebuilts` runs in this repository |
| `/status` | Reply with active and recent build runs for this issue |
| `/help` | Reply with the comment command cheat sheet |

You can also combine `/build` with multi-line key-value options in a comment:

```text
/build
android: android17
branch: fix/my-change
release: true
```

> [!NOTE]
> `/cancel` runs in a separate lightweight workflow
> ([`issue-commands.yml`](.github/workflows/issue-commands.yml)) so it executes
> immediately and is never queued behind a long-running build. Only comments
> from `OWNER`, `MEMBER`, or `COLLABORATOR` accounts are accepted.

## Downloading prebuilts

Each built branch publishes its own GitHub Release tagged
`<branch>-<YYYY-MM-DD>-<bt_sha>` (for example `android17-2026-11-22-5d66dd11` or
`android16-2026-11-22-246557a3`) with automatically generated short release notes
summarizing changes since the previous release on the same branch, containing
`digitalis-prebuilts-<branch>-<YYYY-MM-DD>-<bt_sha>.tar.gz`, its `.sha256`,
`MANIFEST.txt`, and `SOURCES.json` on the [Releases](../../releases) page.

Each archive unpacks to `digitalis-prebuilts/` containing:

- `system/lib64/libberberis_arm64.so`, `libberberis_exec_region.so`, and all 21
  `libberberis_proxy_*.so` host x86_64 libraries
- `system/bin/berberis_program_runner*_arm64` host runners
- `system/bin/arm64/{app_process64,linker64}` and `system/lib64/arm64/*.so`
  guest ARM64 runtime libraries
- `system/etc/ld.config.arm64.txt`, `system/etc/init/berberis.rc`, and
  `system/etc/binfmt_misc/arm64_{dyn,exe}`
- `digitalis-prebuilts.mk` for single-line AOSP product integration
- `MANIFEST.txt`, `SOURCES.json`, `SHA256SUMS`, `README.md`, and `LICENSE`

Match the bundle's Android version (`android16` / API 36 or `android17` / API 37)
to your AOSP tree.

## Integrating into an x86_64 AOSP product

1. Extract the tarball into your AOSP checkout (for example `vendor/digitalis/prebuilts/`).
2. Declare the ARM64 native-bridge ABI in your `BoardConfig.mk`:

   ```makefile
   TARGET_NATIVE_BRIDGE_ARCH := arm64
   TARGET_NATIVE_BRIDGE_ARCH_VARIANT := armv8-a
   TARGET_NATIVE_BRIDGE_CPU_VARIANT := generic
   TARGET_NATIVE_BRIDGE_ABI := arm64-v8a
   ```

3. Inherit `digitalis-prebuilts.mk` from your x86_64 product makefile (after
   `runtime_libart.mk`):

   ```makefile
   $(call inherit-product, vendor/digitalis/prebuilts/digitalis-prebuilts.mk)
   ```

See `integrating-digitalis.md` in
[DigitalisX64/digitalis](https://github.com/DigitalisX64/digitalis) for the full
verification checklist.

## How the CI build fits on GitHub-hosted runners

A full Digitalis AOSP checkout and build needs ~300 GB of disk. Instead of
syncing all of AOSP, `.github/workflows/build-prebuilts.yml` performs an
unbundled Soong-only build (`SOONG_ONLY=true`, `TARGET_BUILD_UNBUNDLED=true`,
`ALLOW_MISSING_DEPENDENCIES=true`) from scratch with zero Actions cache usage:

1. Reclaims unused runner SDKs and configures a 12 GB swap file (`scripts/free-runner-disk.sh`).
2. Fetches the pinned x86_64 and arm64 AOSP `target_files` builds from
   `ci.android.com` (verified by size and SHA-256), extracts the 19 x86_64
   framework `.so` libraries linked by the proxies and the 7 unproxied arm64
   guest libraries (`libicu*`, `libcrypto`, `libssl`, `libsqlite`), and deletes
   the archives (`scripts/fetch_aosp_libs.py`).
3. Shallow/sparse-fetches only the ~52–54 pinned AOSP and Digitalis projects
   listed in `config/versions.json` (`scripts/fetch_source.py`).
4. Stages the minimal `device/digitalis/digitalis_prebuilts` product and builds
   every remaining artifact from `BERBERIS_DISTRIBUTION_ARTIFACTS_ARM64` plus
   `berberis_arm64_host_tests` and `berberis_native_bridge_load_error_tests`
   (`scripts/build.sh`).
5. Packages and verifies the canonical tarball (`scripts/package_bundle.py`,
   `scripts/verify_bundle.py`, and upstream `verify-digitalis-prebuilts.sh`),
   then runs both host test suites (`scripts/run-host-tests.sh`).

## Other trigger methods

In addition to [Issues and Comment Commands](#triggering--controlling-builds-via-issues-and-comments):

- **Manual run (workflow artifact only):** run **Actions → Build Digitalis Prebuilts → Run workflow**
  (choose `all`, `android17`, or `android16`, plus optional `bt_ref` / `use_branch_head`).
- **Manual run + GitHub Release:** set `publish_release: true` to publish one
  GitHub Release per branch (`<branch>-<YYYY-MM-DD>-<bt_sha>`, or an explicit
  `release_tag` for single-branch runs).
- **Tagged trigger:** push a `v*` tag (for example `git tag v2026.11.22 && git push origin v2026.11.22`)
  to build both `android16` and `android17` and publish each branch's Release
  (`android17-<YYYY-MM-DD>-<bt_sha>` and `android16-<YYYY-MM-DD>-<bt_sha>`).

## Updating pinned commits

```bash
# Move DigitalisX64 repos to the latest android16/android17 branch HEADs:
python3 scripts/update_lock.py --digitalis-only

# Refresh all manifest and AOSP superproject commits:
python3 scripts/update_lock.py

# Validate config/versions.json and run local unit tests:
python3 scripts/lock.py check
python3 -m unittest discover -s tests -v
```

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
