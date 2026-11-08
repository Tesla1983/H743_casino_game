#!/usr/bin/env bash
# flash_dongle.sh —— 烧录 RNG 最小固件（SWD 调试器）
#
# 用法（在本仓库根目录）：
#   bash firmware/rng_dongle/flash_dongle.sh              # 有本地构建产物就烧它，否则烧 prebuilt
#   bash firmware/rng_dongle/flash_dongle.sh --prebuilt   # 强制烧 firmware/prebuilt/rng_dongle-1.0.bin
#   bash firmware/rng_dongle/flash_dongle.sh --restore     # 回刷完整 GUI 固件镜像
#
# 为什么必须跟一次 reset：
#   probe-rs download 结束后内核停在 halt 状态，不 reset 的话固件根本不会跑，
#   表现为"烧录成功但串口无响应"。
set -euo pipefail

CHIP="STM32H743VITx"
# 不绑定具体调试器型号：默认由 probe-rs 自动选择；多调试器并存时用
# PROBE_SELECTOR 环境变量指定（格式 VID:PID[:序列号]）。
PROBE_ARGS=()
if [[ -n "${PROBE_SELECTOR:-}" ]]; then PROBE_ARGS=(--probe "${PROBE_SELECTOR}"); fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"        # 仓库根目录
BACKUP="${ROOT}/firmware/backup/full-firmware-backup.bin"   # 回刷用的完整 GUI 固件备份（需自行放置）
PREBUILT="${ROOT}/firmware/prebuilt/rng_dongle-1.0.bin"
PREBUILT_ELF="${ROOT}/firmware/prebuilt/rng_dongle-1.0.elf"
LOCAL_ELF="${ROOT}/build-dongle/rng_dongle.elf"

# probe-rs 由 cargo 安装：其 bin 目录需已在 PATH 中；若不便修改 PATH，
# 可用 CARGO_BIN 环境变量指定该目录。
if [[ -n "${CARGO_BIN:-}" ]]; then export PATH="${CARGO_BIN}:${PATH}"; fi

# 预编译优先用 ELF：probe-rs 默认按 ELF 解析，喂裸 .bin 会报
#   "Failed to read or decode the ELF file / Unknown file magic"。
prebuilt_img() {
    if [[ -f "${PREBUILT_ELF}" ]]; then echo "${PREBUILT_ELF}"; else echo "${PREBUILT}"; fi
}

case "${1:-}" in
    --restore)
        IMG="${BACKUP}"
        [[ -f "${IMG}" ]] || { echo "[FAIL] 找不到回刷镜像：${IMG}" >&2; exit 1; }
        echo "[info] 回刷完整 GUI 固件（约 1.69 MB，预计 ~225 s）"
        ;;
    --prebuilt)
        IMG="$(prebuilt_img)"
        [[ -f "${IMG}" ]] || { echo "[FAIL] 找不到预编译镜像：${IMG}" >&2; exit 1; }
        echo "[info] 烧录预编译的最小固件（不构建）"
        ;;
    *)
        if [[ -f "${LOCAL_ELF}" ]]; then
            IMG="${LOCAL_ELF}"
            echo "[info] 烧录本地构建产物（build-dongle/rng_dongle.elf）"
        else
            IMG="$(prebuilt_img)"
            [[ -f "${IMG}" ]] || { echo "[FAIL] 既没构建产物也没 prebuilt；先 cmake --build build-dongle" >&2; exit 1; }
            echo "[info] 没有本地构建产物，改烧预编译镜像"
        fi
        ;;
esac

# 裸 .bin 必须显式声明格式与装载地址（否则 probe-rs 当成 ELF 解析）
DL_ARGS=()
case "${IMG}" in
    *.bin) DL_ARGS+=(--binary-format bin --base-address 0x08000000) ;;
esac

echo "[1/2] download ${IMG}"
probe-rs download "${PROBE_ARGS[@]}" --chip "${CHIP}" "${DL_ARGS[@]}" "${IMG}"

echo "[2/2] reset（不 reset 固件不会开始跑）"
probe-rs reset "${PROBE_ARGS[@]}" --chip "${CHIP}"

echo "[OK] 完成。验收：在串口上依次发送 \$?PING / \$?VER / \$?STAT / \$?RND,<n> 检查应答"
