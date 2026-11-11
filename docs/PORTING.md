# CMCC S4602 (RV1109 / NAND) → OpenIPC 移植报告

日期：2026-11-11（按 2026-06-29 现场日志与分区备份**大幅修订**）
状态：**离线工程 + 实证取证完成；设备为半砖，设备侧未执行**

> 本版推翻了 2026-11-11 上午那版的多处「未验证」结论。
> 用户补充了原厂启动日志、变砖过程记录、S4602 笔记与 14 个分区备份。
> 复盘见 [`INCIDENT.md`](INCIDENT.md)，救砖与实施路径见 [`RECOVERY.md`](RECOVERY.md)。

---

## 1. 结论先说

| 判断 | 结论 | 依据 |
|---|---|---|
| 需要把 RV1109 从零移植进 OpenIPC 吗 | **不需要** | 官方已有 `rv1109_lite_defconfig`、内核分支 `rockchip-rv11xx`、nightly NAND 固件 |
| S4602 的硬件参数还未知吗 | **已知大半** | 串口日志 + 原厂 DTB + 备份解析：RV1109 / LPDDR4 512MB / SPI-NAND 256MB / SC5338 / 14 分区 |
| U-Boot 控制台可进吗 | **变砖前可以**（Ctrl+C → `=>`） | 原厂日志 97–103 行完整 `help` / `printenv` |
| 原厂校验内核签名吗 | **不校验** | SPL 日志 `## Verified-boot: 0`、`FIT: no signed, no conf required` |
| 要不要编译 U-Boot | **不要** | 官方策略 + SPL/ddr bin 至今仍在运行；详见 [`UBOOT.md`](UBOOT.md) |
| **U-Boot 能引导 OpenIPC 内核吗** | **能**（不用改 U-Boot） | `boot_fit` + 无签名校验 + 原厂 FIT 可作模板；详见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md) |
| 官方 nightly 能直接刷吗 | **不能** | 不含 `zboot.img`，DTB 是 Longse 评估板 + IMX307；且官方 `zboot.img` 默认不是 FIT |
| **刷进去能直接挂载 rootfs 吗** | **不能** | U-Boot 强制注入 `rootfstype=squashfs`，OpenIPC rootfs 是 UBIFS → 需 `CONFIG_CMDLINE_FORCE` |
| **刷进去能出图吗** | **不能** | 内核树无 `sc5338.c`（但有同族 sc500ai/sc401ai/sc5239）；资料已备齐、驱动未编码未实板验证，见 [`SC5338.md`](SC5338.md) |
| 现在能继续刷 OpenIPC 吗 | **不能，设备是半砖** | 2026-06-29 最后一次 `mtd write` 从偏移 0 写满整片，SPL 找不到 U-Boot FIT |
| 备份够救砖吗 | **够恢复 14 个 GPT 分区，不够恢复 loader** | 缺 `0x20000–0x300000` 的 SPL/ddr bin 区；目前该区仍完好 |
| 本次完成到哪 | **取证、复盘、参数账本、工具链、方案全部落地** | 未连接板子、未执行任何写操作 |

一句话：SoC 支持现成、板级参数基本齐全、**U-Boot 不是障碍**；
真正的瓶颈是「设备半砖 + 镜像格式/bootargs + SC5338 驱动编码与实板验证」
（**资料缺口已关闭**，见 [`SC5338.md`](SC5338.md)）。
下一步不是「继续刷」，而是**救砖 → 恢复原厂 → 补备份 → 按实证路径构建与验证**。

---

## 2. 证据链

| # | 事实 | 来源 |
|---|---|---|
| 1 | OpenIPC 支持 RV1109，发布 `openipc.rv1109-nand-lite.tgz` | openipc.org SoC 页；release `nightly-20261110-9dc02de` |
| 2 | 不为该 SoC 提供 bootloader，要求保留原厂 U-Boot | 官网原文 "The camera keeps its own bootloader…" |
| 3 | nightly 包只有 `rootfs.ubi.rv1109`（7,864,320 B），**没有内核镜像** | 本地解包 + `verify_image.py` |
| 4 | UBI：PEB 128 KiB / LEB 126976 / min I/O 2048 / 60 PEB，卷 rootfs + rootfs_data | 同上 |
| 5 | 官方 DTB 目标 `rv1109-38x38-v10-spi-nand-imx307.img` = Longse 板 + IMX307 + DDR3 | `rv1109_lite_defconfig` |
| 6 | Makefile 对 rockchip NAND 只打包 `rootfs.ubi` | `src/firmware-master/Makefile` 223–224 |
| 7 | OpenIPC 组织 25 个 U-Boot 仓库无一覆盖 Rockchip；`br-ext-chip-rockchip/` 无 U-Boot 包 | GitHub 组织搜索 → `UBOOT.md` |
| 8 | RV1109/RV1126 的 DDR init / SPL / trust 是 rkbin 闭源 blob | `RKBOOT/RV1126MINIALL.ini` → `UBOOT.md` |
| 9 | **SoC 确证 RV1109**（`rockchip-cpuinfo: SoC 11090000`），2× Cortex-A7 | dmesg；DTB 写 rv1126 是共用 BSP |
| 10 | **LPDDR4 512 MB @ 924 MHz**，SPI-NAND 256 MiB（id `e5 f2 e5`） | SPL / dmesg |
| 11 | **sensor = SC5338**（`0x00ce50`），i2c1@0x30，2-lane MIPI；ES7202 / PCF8563 / GMAC 外置 PHY | dmesg + 原厂 DTB |
| 12 | **14 个 GPT 分区**完整偏移与大小，A/B 双槽，当前 slot `_b` | GPT + `mtdparts` + `printenv` 三方一致 |
| 13 | 原厂 UBI 几何与 OpenIPC 官方镜像**完全一致** | dmesg `ubi0` 段 vs `verify_image.py` |
| 14 | **`## Verified-boot: 0`**，FIT 内 sha256 但无公钥签名 → 排除方案 C | SPL 日志 |
| 15 | `bootcmd=boot_fit;boot_android ...`，内核从 **boot_a/b 分区的 FIT** 加载 | `printenv`；启动日志 |
| 16 | U-Boot 有 `mtd read/write/erase`、`tftp`、`fatload/fatwrite`、`rockusb`、`download`、`boot_fit`；**没有** `ubi` / `saveenv`，`nand` 无设备 | `help` 全文 |
| 17 | 环境变量**不持久化**（`Using default environment`，重启恢复默认） | 变砖记录 6948 行 |
| 18 | 原厂 `boot_a.img` 是 FIT（kernel + fdt + resource），内含**真实 S4602 DTB** | `tools/inspect_vendor_backup.py` → `docs/evidence/vendor-kernel.dtb` |
| 19 | 14 个分区备份齐全、md5 已算；`uboot_a` 与 `uboot_b` 内容相同 | 备份目录 → `INCIDENT.md` 第 3 节 |
| 20 | 备份缺 `0x20000–0x300000`（2944 KiB loader 区），`gpt.img` 只有 GPT 表 | GPT 解析 + 覆盖计算 |
| 21 | 变砖根因：第 8840–8842 行参数被截断的 `mtd write` 从 offset 0 写 256 MB | 变砖记录 → `INCIDENT.md` |
| 22 | 最终状态：SPL 正常但 `Not fit magic` / `SPL: failed to boot from all boot devices` | 变砖记录 9005–9021 |
| 23 | **`scripts/mkimg` 三分支**：只有 `BOOT_IMG` 被 `file` 判为 Device Tree Blob 才走 `repack_itb`（FIT）；默认走 `make_boot_img`（Android boot.img） | `docs/evidence/openipc-mkimg.sh` |
| 24 | firmware 与内核树里 `BOOT_IMG` / `.itb` / `.its` **均为 0** → 官方 `zboot.img` 默认不是 FIT | 源码搜索 + 68,270 项内核树 |
| 25 | 原厂 `boot_a.img` 被 `file` 判为 `Device Tree Blob v17`，`-p 0x800` 与 mkimg 一致 → **可作 ITB 模板** | 备份镜像 + mkimg |
| 26 | **`setenv bootargs` 无效**：3643 行设 `root=ubi0:rootfs`，3706 行实际仍是 `root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=9`；7 次启动 cmdline 全部如此 | 变砖记录；`BOOT_FEASIBILITY.md` 2.1 |
| 27 | `vendor-kernel.dtb` 的 `chosen` 含 `root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=3`；原厂 `system_*` 卷内是 squashfs，OpenIPC 是 UBIFS | DTB 解析 + `hsqs @0x41000` |
| 28 | OpenIPC 内核树 `sc5338` / `smartsens` / `sc53*` **0 个**；但有同族 `sc500ai.c` / `sc401ai.c` / `sc5239.c`；`rv1126.generic.config` 已开 V4L2/RKCIF/RKISP、所有 `CONFIG_VIDEO_SC*` 未开 | `docs/evidence/linux-tree-rv11xx.json`；`SC5338.md` |
| 28a | **SC5338 资料包**：公开驱动（OpenIPC/openingenic）+ 原厂 vmlinux 寄存器表（与公开 HDR2 表相似度 0.9551）+ V4L2 骨架；已导出 5 组 `struct regval` 表 | `docs/SC5338.md`、`docs/evidence/sc5338-regs-crosscheck.txt` |
| 29 | 内核配置 `RK_SFC_NAND=y` / `MTD_UBI` / `UBIFS_FS` / `SQUASHFS` / `MTD_CMDLINE_PARTS=y` 齐；`CMDLINE_FORCE` 未开 | `rv1126.generic.config` |

**仍未确认**：NAND 芯片丝印型号、PHY 确切型号（笔记写 YT8512B，DTB 未见）、
A/B 选槽机制（活动槽 `_b` 与 `flash_plan` 的 `_a` 假设冲突）、USB OTG 能否进 MASKROM、
**rkaiq 的 IQ/3A tuning XML**（原厂 `system_a.img` 是 squashfs-in-UBI，尚未解包）、
`CONFIG_CMDLINE_FORCE` 在本 BSP 上的实际行为、
`mkimg` 生成的 ITS 里残留 signature 节点是否会让 `mkimage` 报错，
以及 SC5338 的 xvclk/link freq、原厂 3 张寄存器表各自对应的 mode（见 `SC5338.md` 第 7 节）。
**原厂 4.19.111 内核源码已不再是必需材料** —— 寄存器表已从原厂内核二进制中提取。

> 完整三层判定与解法见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md)。

---

## 3. 差距矩阵（实测后）

| 维度 | 官方基线 | S4602 实测 | 动作 |
|---|---|---|---|
| SoC | RV1109 | **RV1109**（ID 11090000） | 无需改 |
| 内核 | 4.19.111 / rockchip-rv11xx | 原厂同为 4.19.111 | 复用分支，换 DTB |
| DDR | 评估板 DDR3 | **LPDDR4 512 MB @ 924 MHz** | 保留原厂 loader；核对内核 reserved memory |
| Flash | SPI-NAND UBI 128K/2K | **完全一致** | 官方 rootfs.ubi 可直接写 system 分区 |
| Sensor | IMX307 | **SC5338 / i2c1@0x30 / 2-lane / HDR_X2** | 换 DTS sensor 节点 + 按 `SC5338.md` 编码驱动 + 补 IQ 参数 |
| 网口 | 评估板 GMAC | GMAC + 外置 PHY（型号待确证） | 改 DTS；`ethaddr` 用 `48:E5:33:B5:60:D6` |
| 分区 | `ubi.mtd=3` | **14 分区，system_a = MTD 8** | `ubi.mtd=8` + 原厂 mtdparts |
| 内核位置 | — | **独立 boot_a/b 分区（FIT）** | 必须构建并写入 `zboot.img` |
| Bootloader | 无 OpenIPC U-Boot | 原厂 U-Boot 2017.09 + SPL + OP-TEE，可打断、不校验签名 | **保留、不覆盖** |
| 环境变量 | `saveenv` | **无 saveenv，不持久化** | `CONFIG_CMDLINE_FORCE` 内置 cmdline（setenv / DTB chosen 实测均被覆盖） |
| 当前状态 | — | **半砖，进不了 U-Boot** | 先走 `RECOVERY.md` 阶段 0–2 |

DTS 适配的黄金参考已从「官方 Longse 评估板 DTS」升级为
**`docs/evidence/vendor-kernel.dtb`（原厂真实板级 DTB）**。

---

## 4. 实施路径（修订）

> 关于 U-Boot：A / B 两条路都**不编译、不覆盖 U-Boot**；这是官方安装方式，
> 且本机 `Verified-boot: 0` + SPL 仍存活已实证。详见 `UBOOT.md`。
> 真正必须自建的是**内核 + DTB**（`zboot.img`）与 rootfs UBI。

### 前置：救砖（当前所在阶段）
设备进不了 U-Boot → 先按 `RECOVERY.md` 阶段 0–2 建立 MASKROM/rockusb 写通道，
按扇区恢复 gpt + uboot_a/b + boot_a/b，重启验证能进 `=>`，
再恢复其余分区并补备份 `0x0–0x300000`。

### 方案 A（首选：恢复原厂后，U-Boot 控制台可用）
保留原厂 SPL/U-Boot/DDR 参数 → 只刷**一个槽**（`_a` 或 `_b` 待现场定夺，见
[`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md) 4.1）：
`boot_*` 写**自建 FIT** `zboot.img`、`system_*` 写 OpenIPC `rootfs.ubi`。

- **FIT 必须自建**：`tools/build_s4602.sh` 用原厂 `boot_a.img` 作 ITB 模板触发
  `mkimg` 的 `repack_itb`，产物 magic `d00dfeed` 且 ≤ 4 MiB，RAM 里 `iminfo` 验证。
  官方 `zboot.img` 默认是 Android boot.img 格式，`boot_fit` 不认。
- **bootargs 必须内置**：开内核 `CONFIG_CMDLINE_FORCE=y`，把 UBIFS 参数 +
  完整 `mtdparts` 写进 `CONFIG_CMDLINE`。`setenv bootargs` 与 DTB `chosen`
  都已被现场实验证明会被 U-Boot 覆盖。
- 装载地址用 `0x1000000` 之类高地址，`0x02008000` 会与 FIT kernel load 重叠。
- `mtd write` 的 size 用**文件实际字节数**（rootfs = `0x780000`），并回读 `cmp.b` 校验。
- 另一个槽原厂完整保留作回滚；**SC5338 驱动编码 + 实板验证完成前，验收只到 SSH/UBIFS，不含出图**（资料见 `SC5338.md`）。

### 方案 B（U-Boot 控制台不可恢复）
MASKROM + `rkdeveloptool` / `upgrade_tool` 按扇区写入，或用 `sdcard_upgrade` 脚本
（脚本里已有 `zboot.img` / `system.ubi` 的现成分区偏移）。
注意该脚本会同时刷 gpt/uboot、且要求 SD 卡可用；**只放 zboot.img 与 system.ubi，
不要放 gpt.img / uboot.img**，可跳过 loader 覆盖步骤。风险更高，必须先有完整备份。

### 方案 C（原厂校验签名）—— **已排除**
`## Verified-boot: 0` 且 FIT 无签名，不存在签名障碍。

---

## 5. 红线

- **设备未救砖、原厂未恢复、loader 区未补备份前，不执行任何写 flash 动作。**
- `mtd write` 四参数齐全，size = 实际文件字节数；写后 `mtd read` + `cmp.b` 回读。
  （2026-06-29 变砖直接原因：命令被截断成 `mtd write spi-nand0 0x10`，从 0 写了 256 MB。）
- 不覆盖 `uboot` / `env` / `0x0–0x300000` loader 区。
- 只刷一个 A/B 槽；不同时刷两套 system。
- 必须同时替换内核与 rootfs；只刷 rootfs 起不来。
- 官方 nightly 包不要直接刷；`make_flash_plan.py` 拒绝时不要绕过手写命令。
- 串口 3.3 V / 1500000 8N1；刷写全程不断电、逐条执行、全程留日志。

---

## 6. 验收标准

| 阶段 | 判据 |
|---|---|
| 救砖 | 串口出现 U-Boot 横幅与 `=>`；`printenv` 恢复默认值 |
| 原厂恢复 | 出现 `Cmiot login:`、网口正常、MAC 为 `48:E5:33:B5:60:D6` |
| 引导 | OpenIPC 内核解压日志、DTB 解析无报错、`ubi0` attach `mtd8` 成功 |
| 系统 | `init` 起来，`cat /proc/mtd` 与实测 14 分区一致，`ipctool` 识别 RV1109 |
| 网络 | `eth0` 拿到地址，SSH 可登录（默认 root / 12345，登录后立即改密码） |
| 出图 | `Detected SC00ce50 sensor` → `start stream 2880x1620, hdr: 5` → `majestic` RTSP 有流（逐级验收见 `SC5338.md` 第 6 节） |
| 回滚 | 切回 `_b` 槽能启动原厂固件 |

---

## 7. 交付清单

**已完成**
- `firmware/`、`src/firmware-master/`：官方固件与源码本地副本
- `移动千里眼S4602原厂固件备份/`：14 个分区镜像（用户提供的原始文件，未改动）
- `docs/INCIDENT.md` / `docs/RECOVERY.md` / `docs/PORTING.md` / `docs/UBOOT.md` / `docs/BOOT_FEASIBILITY.md` / `docs/SC5338.md`
- `docs/evidence/`：官方 DTS ×2、**原厂内核 DTB + 完整反编译 + vmlinux**、SC5338 拓扑/dmesg/寄存器表比对、内核/U-Boot/rkbin tree 快照、取证记录
- `src/sensor-reference/`：SC5338 参考驱动、内核配置、导出的 `struct regval` 表
- `board/s4602/`：`board.yaml`（已填实测）、defconfig、UBI 布局、分区表
- `tools/`：`inspect_vendor_backup.py`、`verify_image.py`、`make_dts.py`、
  `probe_board.py`、`make_flash_plan.py`（含砖机守卫）、`build_s4602.sh`
- `uboot/`：00 只读探测 / 01 备份 / 02 刷写模板 / 03 bootargs

**必须现场完成**
1. 确认当前砖态与 MASKROM 通道，取得匹配 LPDDR4 的原厂 loader
2. 按扇区恢复引导链分区 → 验证能进 U-Boot → 恢复其余分区 → 补备份 loader 区
3. 按 `docs/SC5338.md` 编写 `sc5338.c`（原厂内核源码已非必需）；构建 FIT `zboot.img` + `CMDLINE_FORCE`
4. 定夺 A/B 槽方向并同步 `flash_plan` → RAM 验证 → 只刷一个槽 → 验收

---

## 8. 下一步（按优先级）

1. **P0** 接 UART 抓当前上电输出；确认 USB OTG 是否枚举 `2207`。
2. **P0** 取得原厂 `MiniLoaderAll.bin` / `parameter.txt`（固件包或同型机 dump）。
3. **P1** 按 [`SC5338.md`](SC5338.md) 完成 `sc5338.c` 编码（资料已备齐，**不需原厂内核源码**）；另需从原厂 rootfs 解出 rkaiq IQ/3A tuning XML。
4. **P1** `RECOVERY.md` 2A 第 1 批只恢复 gpt + uboot_a/b + boot_a/b，验证能进 `=>`。
5. **P1** 按 [`FULL_BACKUP.md`](FULL_BACKUP.md) 补完整全片备份，恢复其余分区，确认原厂可启动。
6. **P2** 用 `vendor-kernel.dtb` 校订 DTS，跑改好的 `build_s4602.sh`
   （FIT 模板 + `CMDLINE_FORCE`），核对 magic/尺寸；实测 `Kernel command line`。
7. **P2** 定夺 A/B 槽方向 → `make_flash_plan.py --check` 三项 blocker 全绿 → 只刷一个槽并验收。
