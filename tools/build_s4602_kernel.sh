#!/usr/bin/env bash
# ============================================================================
# 本地（WSL / Linux 构建机）交叉编译 S4602 用的 OpenIPC 内核（RV1109 / SPI-NAND）
# 本脚本是 .github/workflows/build.yml 的本地等价物：配置来源、工具链、打包方式
# 与 CI 完全一致，便于离线复现 / 排错。
#
# 假设设备正常：跳过砖机恢复，只产出可引导内核 + 复用官方 nightly rootfs。
# 出图仍需 SC5338 驱动（见 docs/SC5338.md），本脚本只保证「能引导、能进 SSH」。
#
# 前置：OpenIPC 官方工具链 arm-openipc-linux-gnueabihf 已下载并置于 PATH，例如：
#   wget -q https://github.com/openipc/firmware/releases/download/toolchain/toolchain.rockchip-rv11xx.tgz -O /tmp/t.tgz
#   sudo tar xzf /tmp/t.tgz -C /opt/openipc-toolchain
#   export PATH=/opt/openipc-toolchain/arm-openipc-linux-gnueabihf/bin:$PATH
#
# 用法：
#   bash tools/build_s4602_kernel.sh                 # 默认在 /opt/linux 编译
#   LINUX=/path/to/linux bash tools/build_s4602_kernel.sh
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRAG="$REPO/board/s4602/s4602_kernel_fragment.config"
VENDOR_DTB="$REPO/docs/evidence/vendor-kernel.dtb"
FIT_SH="$REPO/tools/make_fit.sh"

LINUX="${LINUX:-/opt/linux}"
OUT="${OUT:-$LINUX/../out2}"
CROSS="${CROSS:-arm-openipc-linux-gnueabihf-}"
JOBS="$(nproc)"

echo "==> LINUX=$LINUX  CROSS=$CROSS  FRAG=$FRAG"

# ---- 0) 检出内核分支 rockchip-rv11xx（与 nightly rootfs 同源，模块 ABI 一致） ----
if [ ! -d "$LINUX/.git" ]; then
  echo "==> 克隆 OpenIPC/linux@rockchip-rv11xx -> $LINUX"
  git clone --depth 1 --branch rockchip-rv11xx https://github.com/OpenIPC/linux.git "$LINUX"
fi
cd "$LINUX"

# ---- 1) 基线配置 + S4602 增量片段（与 CI 完全相同） ----
cp arch/arm/configs/rv1126.generic.config .config
MC=scripts/kconfig/merge_config.sh
[ -f "$MC" ] || MC=scripts/merge_config.sh
bash "$MC" -m .config "$FRAG"
make ARCH=arm olddefconfig

echo "==> 关键配置自检："
grep -E '^CONFIG_(CMDLINE_FORCE|CMDLINE|MTD_CMDLINE_PARTS|MTD_UBI|RK_SFC_NAND|STMMAC_ETH|VIDEO_ROCKCHIP_ISP)=' .config | sed 's/^/    /'

# ---- 2) 编译 zImage + 模块 ----
make ARCH=arm CROSS_COMPILE="$CROSS" -j"$JOBS" zImage modules

# ---- 3) 产出：zImage + vendor DTB + FIT ----
mkdir -p "$OUT"
cp arch/arm/boot/zImage "$OUT/zImage"
cp "$VENDOR_DTB" "$OUT/vendor-kernel.dtb"
make ARCH=arm CROSS_COMPILE="$CROSS" INSTALL_MOD_PATH="$OUT/modules" modules_install
bash "$FIT_SH" "$OUT/zImage" "$OUT/vendor-kernel.dtb" "$OUT/zboot.img"

echo "==> 产物："
ls -l "$OUT"/zImage "$OUT"/vendor-kernel.dtb "$OUT"/zboot.img
echo "    modules: $(du -sh "$OUT/modules" 2>/dev/null | cut -f1)"
echo "DONE_KERNEL_BUILD"
