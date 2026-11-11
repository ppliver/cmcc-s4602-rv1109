# 取证记录（可复现）

时间：2026-11-11，环境：Windows / Git Bash，网络访问 GitHub 使用 `curl --ssl-no-revoke`。

## 官方固件

```bash
curl -sSL --ssl-no-revoke -o firmware/openipc.rv1109-nand-lite.tgz \
  https://github.com/OpenIPC/firmware/releases/download/nightly-20261110-9dc02de/openipc.rv1109-nand-lite.tgz
tar xzf firmware/openipc.rv1109-nand-lite.tgz -C firmware/
md5sum firmware/rootfs.ubi.rv1109
# 5c1849f7683f7f33bf800d2cf966249a
tar tzvf firmware/openipc.rv1109-nand-lite.tgz
# rootfs.ubi.rv1109.md5sum (52 B)
# rootfs.ubi.rv1109        (7,864,320 B)  <- 没有内核镜像
```

release asset 清单（rv11xx 系列）：

| asset | size |
|---|---|
| openipc.rv1103-nand-lite.tgz | 8,479,925 |
| openipc.rv1103-nor-lite.tgz | 8,471,405 |
| openipc.rv1106-nand-lite.tgz | 8,479,945 |
| openipc.rv1106-nor-lite.tgz | 8,471,426 |
| **openipc.rv1109-nand-lite.tgz** | **4,668,203** |
| openipc.rv1126-nand-lite.tgz | 4,668,062 |

注意：rv1109 / rv1126 **没有 nor 包**，因此 `zboot.img` 无法从 NOR 包取得。

## UBI 结构解析

```bash
python tools/verify_image.py firmware/rootfs.ubi.rv1109
# PEB 131072, LEB 126976, min I/O 2048, 60 PEB
# vol 0 rootfs (UBIFS, reserved 265 PEB, 58 image LEB)
# vol 1 rootfs_data (autoresize)
```

`ubireader_display_info` 交叉验证：`Image Sequence Num 241411549`，卷名 rootfs / rootfs_data，
PEB 范围 2–59。卷内为 UBIFS（magic `31 18 10 06`），未发现 uImage / FIT / zImage 头 →
内核不在这个 rootfs 卷里。

## 源码

```bash
curl -sSL --ssl-no-revoke -o openipc-firmware.tar.gz \
  https://codeload.github.com/OpenIPC/firmware/tar.gz/refs/heads/master
# 解压到 src/firmware-master（Windows 下 symlink 无法创建，不影响文本取证）

curl -s --ssl-no-revoke \
  "https://api.github.com/repos/OpenIPC/linux/git/trees/rockchip-rv11xx?recursive=1" \
  -o docs/evidence/linux-tree-rv11xx.json
```

关键文件：

- `br-ext-chip-rockchip/configs/rv1109_lite_defconfig`
- `general/external.mk`（NAND FIT / `/boot` 内核落位说明）
- `general/scripts/ubifs/ubinize.cfg` 与 `ubinize_rockchip.cfg`
- `general/scripts/rootfs_script.sh`
- `Makefile` 第 160–250、385–450 行（repack 打包）
- `general/overlay/usr/sbin/sysupgrade`（rockchip 分支期待 `zboot.img.<soc>`）

## 官方 DTS 基线

```bash
curl -sSL --ssl-no-revoke \
  https://raw.githubusercontent.com/OpenIPC/linux/rockchip-rv11xx/arch/arm/boot/dts/rv1109-38x38-v10-spi-nand-imx307.dts \
  -o docs/evidence/rv1109-38x38-v10-spi-nand-imx307.dts   # 3759 行，dtc 反编译单文件
curl -sSL --ssl-no-revoke \
  https://raw.githubusercontent.com/OpenIPC/linux/rockchip-rv11xx/arch/arm/boot/dts/rv1109-38-v10-spi-nand.dts \
  -o docs/evidence/rv1109-38-v10-spi-nand.dts             # 750 行，源码版含 chosen bootargs
```

`rv1109-38-v10-spi-nand.dts` 的 chosen bootargs（用于 `uboot/03-bootargs.txt`）：

```
earlycon=uart8250,mmio32,0xff570000 console=ttyFIQ0 ubi.mtd=3 \
root=ubi0:rootfs rootfstype=ubifs snd_aloop.index=7
```

## 官方页面原文（RV1109 SoC 页）

> OpenIPC publishes firmware for the Rockchip RV1109, but not a bootloader for it.
> ... The camera keeps its own bootloader, so install this by flashing the kernel
> and root filesystem from it.

中文页同时列出已知主板 RL200 / RL400（Longse），两者「U-Boot 控制台：缺」、
「引脚图：缺」、「闪存转储：缺」。

其他产物：

- `linux-tree-master.json`：OpenIPC/firmware 的 master 目录树（用于确认 `br-ext-chip-rockchip/` 内容）
- `linux-tree-rv11xx.json`：OpenIPC/linux `rockchip-rv11xx` 分支目录树（60 个 rv1109/rv1126 DTS）
- `vol0-ubifs-extract.bin`：从 rootfs.ubi 提取的 vol0 数据（UBIFS，用于确认无内核镜像头）

## 本地自检结果（2026-11-11）

| 检查 | 结果 |
|---|---|
| `python -m py_compile tools/*.py` | PASS |
| `verify_image.py --md5 5c1849…` | PASS：60 PEB / 2 layout，PEB 131072、LEB 126976、min I/O 2048，卷 rootfs(58 LEB) + rootfs_data |
| `make_dts.py` | 生成 3768 行派生 DTS；sensor 未确认时保留官方 IMX307 并在头部写警告 |
| `probe_board.py --dry-run` | 输出 10 条只读命令，不连设备 |
| `make_flash_plan.py --check`（真实 board.yaml） | 按预期硬拒绝，列出 13 项缺失前置，exit 1 |
| `make_flash_plan.py`（临时自造完整参数） | PASS：生成 tftpboot / nand erase / nand write 命令，大小 `0x780000` 与镜像一致；临时文件已删 |
| `bash -n tools/build_s4602.sh` | PASS |

自检中修掉两个真实缺陷：`verify_image.py` 的 VID 头扫描步长跳过了 2048（改为读 EC 头的
`vid_hdr_offset`/`data_offset` 字段）、PEB 计数把压缩数据里偶然的 `UBI#` 也算进去（改为按物理块边界扫描）。

## 补证：U-Boot 是否要编译（2026-11-11）

起因：用户质疑上一轮结论「不用编译 U-Boot」。补做以下取证，结论见 `docs/UBOOT.md`。

```bash
# 1) OpenIPC 组织内 U-Boot 仓库覆盖范围
curl -s --ssl-no-revoke "https://api.github.com/search/repositories?q=org:OpenIPC+u-boot&per_page=100"
# total_count = 25；名称含 rockchip/rv11/rk 的 = 0
curl -s --ssl-no-revoke "https://api.github.com/search/repositories?q=org:OpenIPC+rv1109&per_page=50"
# total_count = 0

# 2) OpenIPC/u-boot master 目录树（upstream 主线镜像，非 OpenIPC 定制分支）
curl -s --ssl-no-revoke "https://api.github.com/repos/OpenIPC/u-boot/git/trees/master?recursive=1" \
  -o docs/evidence/uboot-tree.json
# 40995 项，truncated=false；rockchip/rv11xx 路径 972 个
# configs/ 下 rv11xx defconfig 仅 4 个：
#   elgin-rv1108 / evb-rv1108 / neu2-io-rv1126 / sonoff-ihost-rv1126
# -> 有 RV1126 开发板配置，但 RV1109 无专用 defconfig，也没有 SPI-NAND IPC 板

# 3) Rockchip 闭源引导 blob
curl -s --ssl-no-revoke "https://api.github.com/repos/rockchip-linux/rkbin/git/trees/master?recursive=1" \
  -o docs/evidence/rkbin-tree.json
# 800 项，truncated=false；bin/rv11/ 下 rv1126/rv110x blob 52 个
curl -sSL --ssl-no-revoke \
  "https://raw.githubusercontent.com/rockchip-linux/rkbin/master/RKBOOT/RV1126MINIALL.ini"
# CODE471 = rv1126_ddr_924MHz_v1.16.bin（DDR init，闭源）
# CODE472 = rv1126_usbplug_v1.24.bin（MASKROM 下载）
# FlashData = ddr bin / FlashBoot = rv1126_spl_v1.11.bin
# OUTPUT   = rv1126_loader_v1.16.111.bin
curl -sSL --ssl-no-revoke \
  "https://raw.githubusercontent.com/rockchip-linux/rkbin/master/RKTRUST/RV1126TOS.ini"
# TOS: rv1126_tee_ta_v2.16.bin + rv1126_mcu.bin
```

官方页面原文（SoC 页，完整段落）：

> OpenIPC publishes firmware for the Rockchip RV1109, but not a bootloader for it.
> Both the guided installation and the ready-made flash image begin by writing an
> OpenIPC bootloader, so neither can be offered for this SoC. ...
> **The camera keeps its own bootloader**, so install this by flashing the kernel
> and root filesystem from it.

wiki `en/installation.md` 原文：

> Flashing the OpenIPC firmware using the U Boot bootloader which is included in the
> vendor firmware. ... write it to the flash storage (replacing **the main portion**
> of the vendor firmware).

**结论**：方案 A/A′/B 均不编译、不覆盖 U-Boot；必须自建的是内核 + DTB 与 rootfs UBI。
唯一可能需要重建 bootloader 的情形是原厂 loader 分区损坏且无可用备份，
届时前置材料是 rkbin 中匹配 S4602 DDR 的闭源 ddr/tpl bin、SPL、trust、原厂 `parameter.txt`。

新增证据文件：`uboot-tree.json`、`rkbin-tree.json`；新增专题：`docs/UBOOT.md`。

## 补证：用户提供的现场文档与分区备份（2026-11-11 下午）

用户补充 3 份文档 + 1 个备份目录，推翻了上一版「无串口日志、无备份、U-Boot 控制台未知」的前提。

| 文件 | 大小 | 内容 |
|---|---|---|
| `qianliyan原厂固件启动信息.log` | 63 KB / 1521 行 | 原厂正常启动全流程，含 `Hit key to stop autoboot` → `=>`、完整 `help`、`printenv`、dmesg mtdparts |
| `s4602变砖过程_20260629_133651.txt` | 453 KB / 9177 行 | 备份 14 分区、反复刷 OpenIPC 失败、最终变砖的完整串口记录 |
| `移动千里眼S4602.md` | 17 KB | 硬件/分区整理笔记（含少量推断，如 RV1126 实为 RV1109） |
| `移动千里眼S4602原厂固件备份/` | 253 MB / 14 文件 | 逐分区 `mtd read` + `fatwrite` 导出的原始镜像 |

```bash
# 可复现解析（新工具）
python tools/inspect_vendor_backup.py
python tools/inspect_vendor_backup.py --json
python tools/inspect_vendor_backup.py --extract   # 导出原厂内核 DTB
```

**解析结果**

- GPT 表（gpt.img @LBA1，14 条目 @LBA2）与 dmesg `mtdparts`、`printenv` 三方完全一致；
  总覆盖 252.5 MiB，缺口 `0x20000-0x300000` (2944 KiB，loader 区) 与末尾 640 KiB。
- `boot_a.img` / `boot_b.img` / `uboot_a.img` / `uboot_b.img` 都是 FIT（`d00dfeed`）。
  `boot_a` 内含 kernel(3,247,304 B) + fdt(85,937 B, @0x800) + resource；
  `uboot_a` 内含 uboot(gzip) + optee(gzip) + fdt，配置节点有 `signature`(sha256,rsa2048)。
- `system_a/b`、`model`、`log`、`data`、`data_bk` 都是 UBI 镜像；
  `system_a` 卷 `rootfs`（reserved 332 PEB，162 data PEB，卷内 squashfs `hsqs`）。
- `devinfo_*` 头部 `DEVINFO!`，**只含 MAC**（ASCII @0x34），不含 SN；`misc.img` 全 0xFF；
  `gpt.img` 仅 615 字节有效数据。`data.img` 内有业务设备码 `sn = 8nF4Gk5xHjCrhN2ap…`；
  `serial#=447f56c0594ecd4e` 是 CPU efuse 派生值，不在 flash 中。
- **UBI 几何与官方 OpenIPC rootfs.ubi 完全一致**（PEB 131072 / LEB 126976 / min I/O 2048 /
  VID 2048 / data 4096）→ 官方镜像可直接写入 system 分区。

**提取的黄金参考**：`docs/evidence/vendor-kernel.dtb`（85,937 B，从 boot_a.img 偏移 0x800）。
含 S4602 真实板级节点：`compatible=rockchip,rv1126-38x38-v10-spi-nand`、
SC5338、GMAC 外置 PHY、ES7202、PCF8563、adc-keys、chosen bootargs。

**关键实证（推翻旧结论）**

| 项 | 实测 |
|---|---|
| SoC | `rockchip-cpuinfo: SoC 11090000` → **RV1109**（DTB 写 rv1126 是共用 BSP） |
| DDR | `LPDDR4 / Size=512MB / 924MHz(final freq)`，非 DTB 里写的 DDR3 |
| Flash | `spi-nand0` 255.5 MB，`sfc_nand id: e5 f2 e5` |
| Sensor | `sc5338 1-0030: Detected SC00ce50 sensor`，2-lane MIPI |
| 签名 | `## Verified-boot: 0`、`FIT: no signed, no conf required` → **无强制签名校验** |
| U-Boot | 2017.09 + SPL 2017.09-g61c5d7f + OP-TEE 3.6.0；`=>` 可打断 |
| 命令集 | 有 `mtd`/`tftp`/`fatload`/`fatwrite`/`rockusb`/`download`/`boot_fit`；**无** `ubi`/`saveenv`，`nand` 无设备 |
| 环境 | `Using default environment`，重启恢复默认值 → 不持久化 |
| 内核加载 | `bootcmd=boot_fit;boot_android ...`，从 boot_a/b 分区读 FIT |
| rootfs | `ubi.block=0,rootfs` → `/dev/ubiblock0_0` squashfs；`ubi.mtd=9`(system_b) |

**变砖根因**：`s4602变砖过程_20260629_133651.txt` 第 8840–8842 行，
本想 `fatload system_b.img` 后恢复，但 `mtd write spi-nand0 0x10` 被截断，
U-Boot 把 `0x10` 当 addr、off 缺省 0、size 缺省整片 →
`Writing 268435456 byte(s) (131072 page(s)) at offset 0x00000000` → `data abort`。
最终 9005–9021 行 `Not fit magic` / `Trying fit image at 0x5000 sector` /
`SPL: failed to boot from all boot devices` = 半砖（SPL 存活，U-Boot 进不去）。

全部 14 个备份镜像的 md5、分区偏移、变砖时间线与教训见 `docs/INCIDENT.md`；
救砖与正确移植路径见 `docs/RECOVERY.md`。

---

## 完整备份专项取证（2026-11-11 追加，见 `docs/FULL_BACKUP.md`）

| 项 | 实测 / 来源 | 结论 |
|---|---|---|
| 现有备份覆盖 | 14 文件 = 264,765,440 B = 252.5 MiB / 256 MiB | **98.63 %**，缺 3,670,016 B |
| MTD 全片 | `mtd list`：`0x000000000000-0x000010000000` | `0x10000000` = 268,435,456 B |
| 块层容量 | 启动横幅 `523264 x 512` | `0x0FF80000` = 267,911,168 B，比 MTD 少 512 KiB |
| GPT 备表 | `gpt.img` GPT 头 `backup_lba=523263` | 备表头 `0x0FF7FE00`、条目区 `0x0FF7BE00`，**不在任何备份文件里** |
| NAND 几何 | `mtd list` | block `0x20000` / page `0x800` / OOB `128 B`（可用 63）/ ECC 8bit/512B |
| 整片读会崩 | 记录 1293 行 `mtd read spi-nand0 0x1000000` | 缺省整片 256 MiB → `data abort`；1828 行 90 MiB 成功 → **必须分块** |
| 坏块行为 | U-Boot `cmd/mtd.c` 的 `do_mtd_io()` | `mtd read` **跳过坏块且不留空洞**；有坏块时分块不能按偏移直接还原；现场从未跑过 `mtd bad` |
| `.oob` 语义 | 同上 | `read.oob` 输出 **data + OOB**，32 MiB 块需 `0x2200000` 缓冲；`.raw` 只切 `MTD_OPS_RAW` |
| `rkdeveloptool` | [Rockchip 官方文档](https://opensource.rock-chips.com/index.php?title=Rkdeveloptool) | `RL <BeginSec> <SectorLen> <File>`；Maskrom 下 flash 读写前需 `DB <Loader>` 初始化 DDR |
| 身份字段 | 逐文件扫描 | `devinfo` 只有 MAC；CPU Serial 来自 efuse 不在 flash；业务码在 `data.img` |

新增交付：`docs/FULL_BACKUP.md`、`tools/make_backup_plan.py`、`tools/verify_full_dump.py`、
`uboot/01-full-backup.txt`、`docs/evidence/snapshot-plan.txt`。
`verify_full_dump.py` 用 golden 合成全片做了闭环自检（分块→拼接→md5 比对→污染检测→lba 快照→拒绝误写出），
18 项全部通过；自检中确认 GPT 备表缺失、loader 间隙为 0%。
`docs/evidence/rkbin-tree.json`、`uboot-tree.json` 为上一轮引导链取证。

---

## 引导可行性专项取证（2026-11-11 追加，见 `docs/BOOT_FEASIBILITY.md`）

| 项 | 实测 / 来源 | 结论 |
|---|---|---|
| `bootcmd` | 变砖记录 1255 / 1605 行 `boot_fit;boot_android ...` | 内核从 boot_a/b 分区读 **FIT** |
| 签名 | `## Verified-boot: 0`、`FIT: no signed, no conf required`、`HASH(c): OK` | 可换自建 FIT，无公钥拦 |
| 原厂 `boot_a.img` | `file` → `Device Tree Blob version 17, size=1536`；magic `d00dfeed`；fdt 数据 @`0x800` | 是标准 FIT，可作 ITB 模板 |
| `scripts/mkimg` 三分支 | `docs/evidence/openipc-mkimg.sh` 末尾 | 只有 `BOOT_IMG` 被 `file` 判为 Device Tree Blob 才走 `repack_itb`（`mkimage -E -p 0x800`）；默认走 `make_boot_img`（Android boot.img） |
| ITB 模板来源 | firmware 源码搜 `BOOT_IMG`/`.itb`/`.its` **0 结果**；内核树 68,270 项无 `.itb`/`.its` | **官方 `zboot.img` 默认不是 FIT**，与 `boot_fit` 不兼容 |
| `setenv bootargs` 对照实验 | 3643 行设 `root=ubi0:rootfs rootfstype=ubifs` → 3706 行实际 `root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=9` | **U-Boot 覆盖 root 参数**；7 次启动 cmdline（149/2807/3706/5100/5990/7035/8119）全部如此 |
| bootargs 来源 | `vendor-kernel.dtb` 的 `chosen`：`... ubi.mtd=3 ubi.block=0,rootfs root=/dev/ubiblock0_0 rootfstype=squashfs ...` | U-Boot 合并 DTB chosen + env，按 slot 改写 `ubi.mtd` |
| rootfs 类型差异 | 原厂 `system_*` 卷内 `hsqs @0x41000`（squashfs）；OpenIPC `rootfs.ubi.rv1109` 卷内 UBIFS | `rootfstype=squashfs` 挂不上 UBIFS → 需 `CONFIG_CMDLINE_FORCE` |
| 内核配置 | `docs/evidence/openipc-kernel-config-keys.txt` | `RK_SFC_NAND=y`、`MTD_UBI`、`UBIFS_FS`、`SQUASHFS`、`MTD_CMDLINE_PARTS=y` 齐；`CMDLINE_FORCE` 未开；`CMDLINE_EXTEND=y` |
| sensor 驱动 | 内核树 `sc5338`/`smartsens`/`sc53*` **0 个**；但有同族 `sc500ai.c`/`sc401ai.c`/`sc5239.c`；`rv1126.generic.config` 已开 V4L2/RKCIF/RKISP、所有 `CONFIG_VIDEO_SC*` 未开 | **仍需编写 `sc5338.c`**；但资料已备齐（公开驱动 + 原厂寄存器表），见 `docs/SC5338.md` |
| 原厂 sensor 节点 | `vendor-kernel.dtb`：`sc5338@30`，`compatible = "smartsens,sc5338"`，i2c1，2-lane MIPI | 与官方 IMX307 DTB 不同 |

新增交付：`docs/BOOT_FEASIBILITY.md`、`docs/evidence/openipc-mkimg.sh`、
`docs/evidence/openipc-kernel-config-keys.txt`、`docs/evidence/linux-tree-rv11xx.json`（重新拉取的完整树）。
`tools/build_s4602.sh` 已改为「原厂 FIT 模板 + `CONFIG_CMDLINE_FORCE` + 产物 FIT magic/容量校验」，
`SLOT=a|b` 两种 dry-run 均通过。

**2026-11-11 追加交付（SC5338 资料包，见 `docs/SC5338.md`）**：
`docs/evidence/vendor-kernel.dts`（DTB 完整反编译，441 节点）、
`sc5338-camera-topology.dts`（72 节点链路摘录）、`sc5338-dmesg.txt`（34 + 53 条唯一事件）、
`sc5338-evidence.json`、`vendor-vmlinux.bin`（原厂 zImage 的 XZ payload @`0x3df0` 解压，
11,853,824 B，含内建 `smartsens,sc5338` 字符串）、
`sc5338-regs-crosscheck.txt`（9 张表相似度矩阵，原厂 HDR 表 vs 公开表 **0.9551**）；
`src/sensor-reference/` 内 9 份下载源码/配置 + 生成的 `sc5338-regs-rockchip.h`（5 组表）；
工具 `tools/collect_sc5338.py`、`tools/verify_sc5338_regs.py`（均含 `--check` 自检）。
结论：**不再需要原厂 4.19.111 内核源码**，剩余工作是编码 `sc5338.c` + 实板验证。
