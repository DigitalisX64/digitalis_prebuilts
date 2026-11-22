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

# Minimal unbundled Soong product for compiling the Digitalis ARM64->x86_64
# translator, proxies, program runners, and native_bridge guest binaries.
$(call inherit-product, $(SRC_TARGET_DIR)/product/module_common.mk)
$(call inherit-product, $(SRC_TARGET_DIR)/product/core_64_bit_only.mk)

PRODUCT_SOONG_NAMESPACES += frameworks/libs/native_bridge_support/android_api/libc
BUILD_BERBERIS := true
BUILD_BERBERIS_ARM64_TO_X86_64 := true
$(call soong_config_set,berberis,translation_arch,arm64_to_x86_64)

PRODUCT_NAME := digitalis_prebuilts
PRODUCT_DEVICE := digitalis_prebuilts
PRODUCT_BRAND := Android
PRODUCT_MODEL := Digitalis prebuilts build
