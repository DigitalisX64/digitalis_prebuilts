# digitalis-prebuilts

Automated GitHub Actions CI that builds and publishes binary-only
[Digitalis](https://github.com/DigitalisX64) ARM64-to-x86_64 translator prebuilt
bundles for **Android 16 (`android16`)** and **Android 17 (`android17`)** on
standard GitHub-hosted `ubuntu-24.04` runners.

## Downloading prebuilts

Download `digitalis-prebuilts-android17-<date>-<sha>.tar.gz` or
`digitalis-prebuilts-android16-<date>-<sha>.tar.gz` (plus its `.sha256`) from
the [Releases](../../releases) page.

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
`ALLOW_MISSING_DEPENDENCIES=true`):

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

## Triggering a build or release

- **Manual run (workflow artifact only):** run **Actions → Build Digitalis Prebuilts → Run workflow**
  (choose `all`, `android17`, or `android16`).
- **Manual run + GitHub Release:** set `publish_release: true` (and optional `release_tag`).
- **Tagged release:** push a `v*` tag (for example `git tag v2026.11.22 && git push origin v2026.11.22`)
  to build both `android16` and `android17` and publish the GitHub Release automatically.

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
