#!/usr/bin/env bash
# ============================================================================
# 打包 S4602 的 Rockchip FIT 启动镜像 (zboot.img)
# 输入：自编译的 zImage + 原厂 vendor-kernel.dtb
# 输出：boot 分区可用的 FIT（被原厂 U-Boot 的 boot_fit 识别）
#
# 设计（见 docs/BOOT_FEASIBILITY.md）：
#   - 不写 resource 镜像（原厂 boot_a.img 的 resource 抽不出来且内核启动不需要，
#     boot_fit 只认 kernel+fdt 配置即可；若 boot_fit 报错再补回）。
#   - load/entry 沿用原厂 FIT 的哨兵值 0xffffff01 / 0xffffff00 —— 该组合已在
#     本硬件上验证可 boot；U-Boot 会按默认地址放置。
#   - -E -p 0x800 与原厂镜像数据对齐一致（fdt 数据从 0x800 起）。
# ============================================================================
set -euo pipefail
if [ $# -lt 3 ]; then echo "usage: $0 <zImage> <dtb> <out.zboot.img>" >&2; exit 2; fi

ZIMAGE="$(realpath "$1")"
DTB="$(realpath "$2")"
OUT="$3"
[ -f "$ZIMAGE" ] || { echo "!! zImage 不存在: $1" >&2; exit 1; }
[ -f "$DTB" ] || { echo "!! dtb 不存在: $2" >&2; exit 1; }

TMP="$(mktemp -d)"
ITS="$TMP/s4602.its"
cat > "$ITS" <<EOF
/dts-v1/;

/ {
	description = "OpenIPC for CMCC S4602 (RV1109)";
	images {
		kernel@1 {
			description = "Linux kernel";
			data = /incbin/("$ZIMAGE");
			type = "kernel";
			arch = "arm";
			os = "linux";
			compression = "none";
			load = <0xffffff01>;
			entry = <0xffffff01>;
		};
		fdt@1 {
			description = "S4602 device tree (SC5338)";
			data = /incbin/("$DTB");
			type = "flat_dt";
			arch = "arm";
			compression = "none";
			load = <0xffffff00>;
		};
	};
	configurations {
		default = "conf@1";
		conf@1 {
			description = "S4602";
			kernel = "kernel@1";
			fdt = "fdt@1";
		};
	};
};
EOF

mkimage -f "$ITS" -E -p 0x800 "$OUT"
echo "==> 已生成 $OUT"
mkimage -l "$OUT"
