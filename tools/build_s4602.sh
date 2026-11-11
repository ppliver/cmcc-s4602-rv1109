#!/usr/bin/env bash
# 在 Linux 构建机（用户环境：Ubuntu 24.04 / 192.168.219.177）上构建 S4602 固件。
# 状态：脚本骨架，未在构建机实跑验证；首次运行前请读完全部注释与 docs/BOOT_FEASIBILITY.md。
#
# 用法：
#   bash tools/build_s4602.sh --dry-run          # 只打印命令
#   SLOT=b bash tools/build_s4602.sh             # 刷 _b（当前活动槽，ubi.mtd=9）
#   SLOT=a bash tools/build_s4602.sh             # 刷 _a（需先解决选槽，ubi.mtd=8）
#
# 2026-11-11 关键修订（见 docs/BOOT_FEASIBILITY.md）：
#   1) 官方 zboot.img 默认走 scripts/mkimg 的 make_boot_img → Android boot.img 格式，
#      原厂 bootcmd 是 boot_fit，只认 FIT。直接写进 boot 分区会 "Not fit magic"。
#      必须把原厂 boot_a.img 当作 ITB 模板传 BOOT_IMG，触发 repack_itb 分支
#      （mkimage -E -p 0x800），产出与原厂同构的 FIT。
#   2) U-Boot 会强制注入 root=/dev/ubiblock0_0 rootfstype=squashfs（现场 7 次启动实证，
#      setenv bootargs 无效）。OpenIPC rootfs 是 UBIFS → 必须开 CONFIG_CMDLINE_FORCE，
#      并把 mtdparts 一并写死（CONFIG_MTD_CMDLINE_PARTS=y 依赖它）。
#   3) SC5338 驱动在 OpenIPC 内核树里不存在。本脚本只解决「能引导、能挂载」，
#      出图仍需单独移植驱动 —— 见文档 L3。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW="$ROOT/src/openipc-firmware"
LNX="$ROOT/src/openipc-linux"
DTS="$ROOT/board/s4602/generated/rv1109-cmcc-s4602-spi-nand.dts"
FIT_TEMPLATE="$ROOT/移动千里眼S4602原厂固件备份/boot_a.img"
VENDOR_DTB="$ROOT/docs/evidence/vendor-kernel.dtb"
KCFG_REL="br-ext-chip-rockchip/board/rv11xx/rv1126.generic.config"
SLOT="${SLOT:-b}"                 # a → ubi.mtd=8 / boot_a ; b → ubi.mtd=9 / boot_b
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

case "$SLOT" in
    a) UBI_MTD=8; KOFF="0x00600000" ;;   # boot_a
    b) UBI_MTD=9; KOFF="0x00A00000" ;;   # boot_b
    *) echo "SLOT 只能是 a 或 b" >&2; exit 2 ;;
esac

# 原厂 14 分区串。CONFIG_MTD_CMDLINE_PARTS=y 依赖它，FORCE 之后 U-Boot 不再注入。
MTDPARTS="spi-nand0:0x20000@0x0(gpt),0x100000@0x300000(uboot_a),0x100000@0x400000(uboot_b),0x80000@0x500000(devinfo_a),0x80000@0x580000(devinfo_b),0x400000@0x600000(boot_a),0x400000@0xa00000(boot_b),0x80000@0xe00000(misc),0x2c00000@0xe80000(system_a),0x2c00000@0x3a80000(system_b),0x5a00000@0x6680000(model),0xf00000@0xc080000(log),0x1800000@0xcf80000(data),0x17e0000@0xe780000(data_bk)"
CMDLINE="earlycon=uart8250,mmio32,0xff570000 console=ttyFIQ0,1500000 ubi.mtd=${UBI_MTD} root=ubi0:rootfs rootfstype=ubifs mtdparts=${MTDPARTS}"

run() {
    echo "+ $*"
    [[ "$DRY" -eq 1 ]] || eval "$@"
}

echo "== 0. 前置检查 =="
[[ -f "$DTS" ]] || { echo "缺少 $DTS，先跑: python3 tools/make_dts.py --sensor sc5338" >&2; exit 1; }
[[ -f "$FIT_TEMPLATE" ]] || { echo "缺少 FIT 模板 $FIT_TEMPLATE（原厂 boot_a.img）" >&2; exit 1; }
[[ -f "$VENDOR_DTB" ]] || { echo "缺少 $VENDOR_DTB，先跑: python3 tools/inspect_vendor_backup.py --extract" >&2; exit 1; }
if [[ "$DRY" -eq 0 ]]; then
    for tool in git tar make gcc python3 mkimage fdtget fdtput dtc file; do
        command -v "$tool" >/dev/null || { echo "缺少 $tool（repack_itb 需要 mkimage/fdtget/fdtput/dtc/file）" >&2; exit 1; }
    done
    # file 必须把模板判成 Device Tree Blob，否则 mkimg 会走 Android boot.img 分支
    if ! file -b "$FIT_TEMPLATE" | grep -q 'Device Tree Blob'; then
        echo "模板不是 FIT，$FIT_TEMPLATE 不符合 mkimg 的 repack_itb 条件" >&2; exit 1
    fi
else
    echo "  (dry-run) 跳过构建工具与 file 判定检查"
fi
echo "  模板: $FIT_TEMPLATE (FIT) ; SLOT=$SLOT → ubi.mtd=$UBI_MTD"

echo "== 1. 取 OpenIPC firmware 源码 (master) =="
if [[ ! -d "$FW" ]]; then
    run "git clone --depth 1 https://github.com/OpenIPC/firmware.git '$FW'"
else
    echo "已存在: $FW"
fi

echo "== 2. 取内核分支 rockchip-rv11xx 并注入 S4602 DTS + FIT 模板 =="
# 板级事实来源是 docs/evidence/vendor-kernel.dtb（从原厂 boot_a.img FIT 提取的真实 S4602 DTB），
# 不是官方 Longse 评估板 DTS。生成 DTS 前请逐项核对：sensor、phy、GPIO、chosen、mtdparts。
if [[ ! -d "$LNX" ]]; then
    run "git clone --depth 1 -b rockchip-rv11xx https://github.com/OpenIPC/linux.git '$LNX'"
fi
run "cp '$DTS' '$LNX/arch/arm/boot/dts/'"
# 关键：把原厂 FIT 放进内核树，mkimg 的 `${BOOT_IMG}` 是相对 objtree 的路径。
run "cp '$FIT_TEMPLATE' '$LNX/boot-template.img'"
# 重要：内核 Makefile 是否按 <dts 名>.img 生成 zboot.img 需在构建机上确认。
# 若报 "No rule to make target ...img"，复用官方 target 名、只替换 DTS 内容更省事。

echo "== 3. 改内核配置：CMDLINE_FORCE + 内置 cmdline（避开 U-Boot 注入） =="
KCFG="$FW/$KCFG_REL"
if [[ "$DRY" -eq 0 ]]; then
    python3 - "$KCFG" "$CMDLINE" <<'PY'
import re, sys
p, cmd = sys.argv[1], sys.argv[2]
s = open(p, encoding="utf-8").read()
s = re.sub(r'^# CONFIG_CMDLINE_FORCE is not set$', 'CONFIG_CMDLINE_FORCE=y', s, flags=re.M)
s = re.sub(r'^CONFIG_CMDLINE_EXTEND=y$', '# CONFIG_CMDLINE_EXTEND is not set', s, flags=re.M)
s = re.sub(r'^CONFIG_CMDLINE=".*"$', 'CONFIG_CMDLINE="%s"' % cmd, s, flags=re.M)
open(p, "w", encoding="utf-8").write(s)
for line in s.splitlines():
    if line.startswith("CONFIG_CMDLINE"):
        print("  " + line[:150])
PY
else
    echo "+ (dry-run) patch $KCFG with CONFIG_CMDLINE_FORCE=y and built-in cmdline"
fi
echo "  ⚠ 构建后必须实测 'Kernel command line' 确认 FORCE 生效、ubi attach mtd$UBI_MTD 成功。"

echo "== 4. 把内核源码打成 Buildroot 可消费的 tarball =="
TARBALL="$ROOT/src/openipc-linux-s4602.tar.gz"
STAGE="$ROOT/src/.stage"
run "rm -rf '$STAGE' && mkdir -p '$STAGE'"
run "cp -a '$LNX' '$STAGE/linux-rockchip-rv11xx-s4602'"
run "rm -rf '$STAGE/linux-rockchip-rv11xx-s4602/.git'"
run "tar czf '$TARBALL' -C '$STAGE' linux-rockchip-rv11xx-s4602"

echo "== 5. 安装 S4602 defconfig，并把内核源改指本地 tarball =="
CFG="$FW/br-ext-chip-rockchip/configs/s4602_lite_defconfig"
run "cp '$ROOT/board/s4602/s4602_defconfig' '$CFG'"
run "sed -i \"s|BR2_LINUX_KERNEL_CUSTOM_TARBALL_LOCATION=.*|BR2_LINUX_KERNEL_CUSTOM_TARBALL_LOCATION=\\\"file://$TARBALL\\\"|\" '$CFG'"

echo "== 6. 构建（BOOT_IMG 触发 mkimg 的 repack_itb 分支 → 产出 FIT） =="
# Makefile: BOARD=<defconfig 名去掉 _defconfig>
run "cd '$FW' && BOOT_IMG=boot-template.img make BOARD=s4602_lite"

echo "== 7. 产物自检 =="
ZBOOT="$FW/output/images/zboot.img"
if [[ "$DRY" -eq 0 ]]; then
    [[ -f "$ZBOOT" ]] || { echo "没有产出 $ZBOOT" >&2; exit 1; }
    MAGIC="$(od -An -tx1 -N4 "$ZBOOT" | tr -d ' \n')"
    SIZE="$(stat -c %s "$ZBOOT")"
    echo "  zboot.img: magic=$MAGIC size=$SIZE bytes"
    [[ "$MAGIC" == "d00dfeed" ]] || {
        echo "ERROR: 不是 FIT（应为 d00dfeed）。检查 BOOT_IMG 是否生效 / file 判定 / mkimg 分支" >&2
        echo "       若 mkimg 报 signature 相关错误，手工删掉 out/boot.its 的" >&2
        echo "       /configurations/conf/signature 节点后重打（本机 FIT: no signed）。" >&2
        exit 1
    }
    [[ "$SIZE" -le 4194304 ]] || { echo "ERROR: zboot.img 超过 boot 分区 4 MiB" >&2; exit 1; }
    echo "  FIT magic + 容量检查通过"
else
    echo "+ (dry-run) 校验 zboot.img magic=d00dfeed 且 size <= 0x400000"
fi

echo
echo "产物目录: $FW/output/images/"
echo "预期文件: zboot.img (FIT), rootfs.ubi"
echo "写入分区（SLOT=$SLOT）：boot_$SLOT @ $KOFF (4 MiB) + system_$SLOT (44 MiB)"
echo "注意: 官方 nightly 的 NAND 包只含 rootfs.ubi；zboot.img 必须单独写到原厂 boot 分区。"
echo "      L3 出图仍缺 SC5338 驱动，见 docs/BOOT_FEASIBILITY.md 第 3 节。"
echo "构建完成后用 tools/verify_image.py 校验 UBI，再进入刷写阶段。"
