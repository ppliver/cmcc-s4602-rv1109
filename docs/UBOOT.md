# U-Boot 要不要编译？—— S4602 / RV1109 引导链决策

日期：2026-11-11　　适用：`docs/PORTING.md` 方案 A / B / C

## 1. 直接回答

**当前方案（A）下不编译 U-Boot，也不刷 U-Boot 分区。要编译的是内核 + DTB，不是 bootloader。**

2026-11-11 补证后，这句从「按官方策略推断」升级为**有本机实测支持**：
SPL 日志明确 `## Verified-boot: 0`（无强制签名），且即便设备已半砖，
BootROM → DDR bin → SPL 链路**至今仍能运行**（只是找不到 U-Boot FIT）。
也就是说：原厂 loader 是好的、不需要重建，缺的是被误写破坏的分区内容。

但这仍是**有条件的**，不是"U-Boot 不用管"：

| 现场观测 | U-Boot 怎么处理 | 走哪条路 |
|---|---|---|
| 能进 `=>`，`help` 有 `mtd`/`tftp`/`fatload`，无签名校验 | **不编译、不改动**；RAM 里 `iminfo`/`bootm` 验证自建 FIT（本机**无 `saveenv`**，且 `setenv bootargs` 实测会被覆盖 → 用内核 `CONFIG_CMDLINE_FORCE`，见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md) 2.3） | 方案 A |
| 能进 `=>`，但缺 `ubi` 命令或内核从 boot 分区 FIT 加载 | **仍不编译**；`mtd write` 直接写 UBI 镜像，内核侧 `ubi.mtd=8` 挂载即可，不需要 U-Boot 支持 UBI | 方案 A |
| 进不了 `=>`，或没有 NAND 写命令 | **也不编译**；走 MASKROM + `rockusb`/`rkdeveloptool` 按扇区恢复分区，loader 区原样保留 | 方案 B |
| 原厂校验内核签名 | ~~编译 U-Boot 也救不了（校验在 BootROM/SPL 层，早于 U-Boot proper）~~ **本机已排除：`## Verified-boot: 0`、`FIT: no signed`** | 不适用 |
| 原厂 loader 分区损坏且无可用备份 | 才需要重建整套 loader = 新开一条 SoC bootloader 移植线；**先按 [`FULL_BACKUP.md`](FULL_BACKUP.md) 从同型机或编程器取干净 loader 镜像** | 超出本次范围 |

一句话：**"不编译"是因为复用原厂 bootloader 是 OpenIPC 对 RV1109 的官方安装方式，不是因为这一层不重要。**
恰恰相反，它是整个项目最大的单点风险 —— 官网两块已知 RV1109 板（Longse RL200/RL400）的
「U-Boot 控制台」都标「缺」，而 S4602 实测**可以** Ctrl+C 进 `=>`（变砖前），
但 2026-06-29 之后因引导链分区被冲掉而进不去了（见 `INCIDENT.md`）。

**「U-Boot 能不能引导 OpenIPC 固件」的专门判定见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md)**：
结论是**能，且不用改 U-Boot** —— `bootcmd=boot_fit` 无签名校验，
把原厂 `boot_a.img` 当 FIT 模板重打包 OpenIPC 内核即可。
障碍不在 U-Boot 本身，而在「官方 `zboot.img` 默认不是 FIT」「U-Boot 强制注入
`rootfstype=squashfs`」「SC5338 驱动尚未编码」这三处。前两处已有解法（FIT 模板 /
`CONFIG_CMDLINE_FORCE`），第三处资料已备齐，见 [`SC5338.md`](SC5338.md)。

---

## 2. 官方依据（原文）

OpenIPC RV1109 SoC 页：

> OpenIPC publishes firmware for the Rockchip RV1109, **but not a bootloader for it**.
> Both the guided installation and the ready-made flash image begin by writing an OpenIPC
> bootloader, so neither can be offered for this SoC.
> **The camera keeps its own bootloader**, so install this by flashing the kernel and
> root filesystem from it.

OpenIPC wiki `en/installation.md`：

> Flashing the OpenIPC firmware using the U Boot bootloader **which is included in the
> vendor firmware**. This method interrupts the normal boot process of the vendor
> firmware, and instead instructs U-Boot to load the OpenIPC firmware over the network,
> and write it to the flash storage (replacing **the main portion** of the vendor firmware).

注意 "the main portion" —— 被替换的是 kernel + rootfs，不含 loader 与 env 分区。
这也是本工程红线「不覆盖 uboot / env 分区」的由来。

补充取证（2026-11-11）：

- OpenIPC 组织下 25 个 `u-boot*` 仓库，覆盖 HiSilicon / SigmaStar / Ingenic / Novatek /
  Allwinner / Xmedia / GrainMedia / T41…，**没有一个 Rockchip**；
  `org:OpenIPC+rv1109` 仓库搜索返回 0。
- `br-ext-chip-rockchip/` 只有 4 个 `*_lite_defconfig` 和 2 个 kernel `.config`，
  **没有 U-Boot 包定义**；构建系统里出现的 U-Boot 只是 `host-uboot-tools`
  （提供 `mkimage`，用来打包 `zboot.img`），不是 target bootloader。
- `OpenIPC/u-boot` 的 master 是 **upstream U-Boot 主线镜像**（标准 U-Boot README），
  树里有 972 个 rockchip/rv11xx 路径，但只带两个 RV1126 板级 defconfig：
  `configs/neu2-io-rv1126_defconfig`、`configs/sonoff-ihost-rv1126_defconfig`
  —— 是 Edgeble Neu2 / Sonoff iHost 开发板，**不是 SPI-NAND IPC**；RV1109 无专用 defconfig，
  更没有 S4602 的。拿它编译出来的东西不能直接刷进这块板。

---

## 3. 到底要编译什么（别混）

| 组件 | 谁提供 | 本次要不要自己构建 | 说明 |
|---|---|---|---|
| BootROM | SoC 掩膜 ROM | 否，不可改 | 固化在 RV1109 里 |
| TPL / DDR init bin | Rockchip 闭源 blob | **否**（复用原厂） | 见第 4 节 |
| SPL / miniloader | Rockchip 闭源 blob + 配置 | **否**（复用原厂） | 见第 4 节 |
| U-Boot proper + env | 原厂固件 | **否**（复用、不覆盖） | 提供 tftp / nand / bootcmd |
| **Linux 内核 + S4602 DTB** | OpenIPC `rockchip-rv11xx` | **是，必须自己构建** | 产出 `zboot.img`；官方 nightly 包里没有 |
| **rootfs UBI 镜像** | OpenIPC lite | 是，按 S4602 分区重打包 | `ubinize-s4602.cfg` / PEB 参数实测后定 |
| `mkimage`（host 工具） | Buildroot `host-uboot-tools` | 自动装 | **只是打包工具，不是编译 U-Boot** |

`tools/build_s4602.sh` 构建的正是内核 + rootfs，没有 U-Boot 这一步，这与官方策略一致。

---

## 4. 为什么 RV1109 上"自己编个 U-Boot"特别不现实

Rockchip 的引导链不是单个 `u-boot.bin`：

```
BootROM  →  TPL (ddr bin, 闭源, 初始化 DDR)
         →  SPL / miniloader (闭源 blob, 加载 trust + U-Boot)
         →  U-Boot proper (开源, 提供命令/环境变量/加载内核)
         →  zboot.img → Linux
```

`rockchip-linux/rkbin` 的 `RKBOOT/RV1126MINIALL.ini` 是权威定义（RV1109 与 RV1126 同代、
共用 `bin/rv11/` 下的 blob）：

```ini
[CODE471_OPTION]  Path1=bin/rv11/rv1126_ddr_924MHz_v1.16.bin     ; DDR 初始化
[CODE472_OPTION]  Path1=bin/rv11/rv1126_usbplug_v1.24.bin         ; MASKROM 下载
[LOADER_OPTION]   FlashData=bin/rv11/rv1126_ddr_924MHz_v1.16.bin
                  FlashBoot=bin/rv11/rv1126_spl_v1.11.bin
[OUTPUT]          PATH=rv1126_loader_v1.16.111.bin
```

`RKTRUST/RV1126TOS.ini` 另外还要 `rv1126_tee_ta_v2.16.bin` + `rv1126_mcu.bin`。
`bin/rv11/` 下有 52 个 rv1126/rv110x blob，按 DDR 类型与频率细分：
`rv1126_tpl_924MHz_ddr3_tb_v1.08.bin`、`...lp3...`、`...ddr4...`、`...lp4...`，
以及 `ddr_784MHz` / `924MHz` / `1056MHz` 等。

由此得出三个硬约束：

1. **DDR 初始化参数是闭源、按板级颗粒选型和走线定制的。** S4602 的 DDR 丝印/容量/频率
   还没抄回来 → 选不出正确的 ddr bin → 就算把 U-Boot proper 编出来，内存都起不来。
2. **完整可启动镜像是一组 blob + ini 打包出来的**（`boot_merger` / `loaderimage`），
   还需要原厂 `parameter.txt` 分区表与 trust 镜像；缺一样就拼不出 `idbloader.img`。
3. **写 loader 分区是本工程明令禁止的动作。** 一旦写错，BootROM 找不到 SPL，
   板子彻底成砖，只能靠编程器或短接 SPI-NAND 救 —— 而本工程连 S4602 的
   分区偏移和 NAND 丝印都还没确认。

所以"编译 U-Boot"在这块板上不是 `make defconfig && make` 的小事，等于
**新开一条 RV1109 bootloader 移植线**，且前置材料（原厂 loader 备份 + DDR 参数）恰恰是
现在最缺的。

---

## 5. 如果真走到"必须重建 bootloader"

只在以下情况同时成立时才考虑：原厂 loader 分区损坏 / 有签名且必须换链 /
原厂 U-Boot 完全不可用，且**其他路都试过**。材料清单：

- [ ] 已校验的全片 NAND 备份（用于抠出原厂 idbloader / ddr bin / parameter / trust）
- [ ] S4602 的 DDR 丝印、容量、频率，匹配到 rkbin 中对应 ddr/tpl bin
- [ ] NAND 丝印，确认 SPL 是否带对应 SPI-NAND 驱动
- [ ] 原厂 `parameter.txt`（分区表、CMDLINE、`mtdparts`）
- [ ] U-Boot 源码 + 新板级 defconfig（可参考 `neu2-io-rv1126_defconfig`），
      打开 `SPI-NAND` / `UBI` / `UBIFS` / `eth` / `tftp` 相关命令
- [ ] `RV1126MINIALL.ini` / `RV1126TOS.ini` 风格的打包配置 + `boot_merger`
- [ ] 能写回 loader 分区的手段（MASKROM / 编程器）+ 可演练的回滚路径

风险等级：**最高**。这不是"移植 OpenIPC"，是"给一块无资料的定制板重写引导链"。
建议先向厂商/运营商索取原厂 SDK 或完整固件包，比逆向 DDR bin 现实得多。

---

## 6. 现场判据（`uboot/00-readonly.txt` 要盯的几条）

决定"要不要编译 U-Boot"的不是猜测，是这几条只读输出：

| 命令 | 判据 | 影响 |
|---|---|---|
| `version` | U-Boot 版本、编译日期 | 能否找到对应源码 / 判断有无 `ubi` 支持 |
| `help` | 有无 `tftpboot`、`nand`/`mtd`、`ubi part`、`ubifsload`、`rockusb` | 缺 `ubifsload` → 走 A′；全缺 → 走 B |
| `printenv bootcmd` | 内核从哪个分区、什么格式加载 | 决定 `zboot.img` 落位与封装格式 |
| `printenv` | 有无 secure boot / verify / hash 字样 | 命中则走 C，编译 U-Boot 无效 |
| `nand info` / `mtd list` | 容量、页/块大小、分区 | UBI 参数与写命令的地址 |
| `bdinfo` | DRAM 起始与大小 | `load_addr` 是否落在可用 RAM |

`help` 里报 `Unknown command` 属正常，记录下来即可，**不要换成写命令去试**。

---

## 7. 证据文件

- `docs/evidence/EVIDENCE.md` —— 本轮取证命令与结果
- `docs/evidence/uboot-tree.json` —— `OpenIPC/u-boot` master 完整目录树（40,995 项）
- `docs/evidence/rkbin-tree.json` —— `rockchip-linux/rkbin` master 完整目录树（800 项）
- 在线原文：`https://openipc.org/cameras/vendors/rockchip/socs/rv1109`
- 在线原文：`https://github.com/OpenIPC/wiki/blob/master/en/installation.md`
- 在线原文：`https://github.com/rockchip-linux/rkbin/blob/master/RKBOOT/RV1126MINIALL.ini`

---

## 8. 补证：本机引导链实测（2026-11-11）

用户补充了 2026-06-29 的串口日志与 14 个分区备份，以下为对本文结论的实证校验：

| 之前的推断 | 实测结果 | 判定 |
|---|---|---|
| 「定制机可能校验内核签名」 | `## Verified-boot: 0`、`FIT: no signed, no conf required`、`HASH(c): OK` | **证伪**，方案 C 排除 |
| 「U-Boot 控制台可能进不去」 | `Hit key to stop autoboot('CTRL+C'): 0` → `=>`，附完整 `help` / `printenv` | **可以进**（变砖前） |
| 「内核加载方式未知」 | `bootcmd=boot_fit;boot_android ...`，从 `boot_a/b` 分区读 FIT（kernel + fdt + resource） | 已明确 |
| 「DDR 参数未知」 | `LPDDR4 512 MB @ 924 MHz`，`DDR Version V1.08 20210825` | 已明确，但闭源 ddr bin 仍未备份 |
| 「是否需要重建 loader」 | 设备半砖后 SPL 仍能打印 DDR 日志、OP-TEE sha256 校验 OK | **不需要**，只需恢复被冲掉的分区 |

原厂 `uboot_a.img` / `uboot_b.img`（md5 相同 `c8987b23…`）是 FIT 容器，含
`uboot`(gzip) + `optee`(gzip) + `fdt`，每个子镜像带 sha256，配置节点里还有
`signature` 节点（`sha256,rsa2048`）—— 但 SPL 的 `Verified-boot: 0` 表明这条验证链未启用。
**结论不变：不编译、不覆盖 U-Boot，直接从备份把 uboot_a/b 原样写回即可。**

真正缺的是 `0x20000–0x300000`（2944 KiB）的 BootROM loader / SPL / ddr bin 区：
14 个分区备份都没覆盖它，`gpt.img` 里只有 GPT 表（615 字节有效数据）。
目前这段仍完好，救砖恢复控制台后**第一件事必须补备份**（见 `RECOVERY.md` 2C / `uboot/01-backup.txt`）。

另需注意本机 U-Boot 的两个「反直觉」点，直接影响刷写脚本：
- **没有 `saveenv`**，`env save` 只打印 usage，启动为 `Using default environment`
  → bootargs 无法持久化；且 `setenv bootargs` / DTB `chosen` 实测都会被 U-Boot
  覆盖成 `rootfstype=squashfs`，**必须开内核 `CONFIG_CMDLINE_FORCE`**
  （见 `BOOT_FEASIBILITY.md` 第 2 节）；
- **没有 `ubi` 命令**、`nand` 无设备 → 只能用 `mtd write spi-nand0 <addr> <off> <size>`，
  且 size 必须等于文件实际字节数（这正是变砖根因，见 `INCIDENT.md`）。
