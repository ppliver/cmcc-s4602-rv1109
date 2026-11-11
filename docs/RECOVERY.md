# S4602 救砖与 OpenIPC 移植（实证版）

前置阅读：[`INCIDENT.md`](INCIDENT.md)（变砖根因与备份清单）、[`PORTING.md`](PORTING.md)（差距矩阵）、
[`FULL_BACKUP.md`](FULL_BACKUP.md)（完整备份的分通道 SOP 与双来源归档）。

> **当前设备是半砖**：SPL 能运行，但进不了 U-Boot 控制台。
> 因此本文第 1 阶段的目标是**先拿到一个能写 flash 的通道**，而不是直接刷 OpenIPC。
> 所有写命令都必须在串口日志全程可观察、电源稳定、备份 md5 已核对的前提下逐条执行。

---

## 阶段 0 · 确认状态（只读）

1. 接 UART：**3.3 V / 1500000 8N1**，RX/TX 交叉，GND 共地。
2. 上电，记录完整输出。判据：

| 看到 | 判定 | 下一步 |
|---|---|---|
| `=>` 提示符 | U-Boot 可进（比预期好） | 直接走 **阶段 2B** |
| `SPL: failed to boot from all boot devices` + `# Reset the board to bootrom #` | 与记录一致，半砖 | 走 **阶段 1** |
| 连 `DDR Version` 都没有 | BootROM/loader 区已损坏 | 只能编程器或 MASKROM + rkbin；**现有备份不含 loader，见 INCIDENT 第 3 节** |

3. 同时把 USB OTG 接到 PC，看是否枚举出 `VID 0x2207`（Rockchip MASKROM）。
   Windows 设备管理器 / Linux `lsusb` 均可。枚举成功 = 有救砖通道。

---

## 阶段 1 · 建立写 flash 的通道

按风险从低到高：

### 1A. MASKROM / rockusb（首选）

BootROM 在找不到有效 loader 时会进 MASKROM；SPL 失败日志里的
`# Reset the board to bootrom #` 也指向这条路。

```bash
# Linux（建议用 192.168.219.177 那台 Ubuntu 构建机）
lsusb | grep 2207                      # 应看到 Rockchip
rkdeveloptool ld                       # 列出设备，确认为 Maskrom
# 用 rkbin 中 RV1126/RV1109 的 loader 初始化 DDR（必须与 LPDDR4 512MB@924MHz 匹配）
rkdeveloptool db RV1126MINIALL_loader.bin
rkdeveloptool rd                       # 复位到 loader 模式，之后才能 wl
```

**关键风险：`db` 阶段需要正确的 DDR 初始化 bin。** rkbin 的
`RKBOOT/RV1126MINIALL.ini` 默认用 `rv1126_ddr_924MHz_v1.16.bin`，
而本机实测是 **LPDDR4 924 MHz**（不是 DDR3），要选 `_lp4_` 或 `rv1126_tpl_924MHz_lp3/lp4` 系列。
选错 DDR bin 表现为 `db` 后设备无响应、DDR 起不来。

**更稳的替代**：从**设备自己**把现有 loader 区读出来当 `db` 的镜像 —— 但 MASKROM 下读 flash
本身也需要 loader，形成先有鸡还是先有蛋。所以建议优先向厂商/固件包索取原厂
`MiniLoaderAll.bin` / `parameter.txt`，或从同型号机器 dump 0x0–0x300000。

### 1B. U-Boot 的 `download` / `rockusb`

如果阶段 0 意外能进 `=>`，直接：

```
download            # 或: rockusb 0 mmc 0 / rockusb 0 mtd 1，按 help 提示
```

进入 rockusb 后主机侧用 `upgrade_tool` / `rkdeveloptool` 按扇区写。

### 1C. 编程器 / 短接 SPI-NAND（最后手段）

拆片用编程器，或短接 SPI-NAND 的 CLK/IO 引脚强制 BootROM 进 MASKROM。
成本高、风险大，且需要重新焊接。**只在 1A/1B 都失败时考虑。**

---

## 阶段 2 · 恢复原厂固件（先验证备份，再谈移植）

### 2A. 通过 MASKROM 按扇区写（rkdeveloptool）

`rkdeveloptool wl <sector> <file>` 用的是 **512 字节扇区号，不是字节偏移**。换算表：

| 分区 | 字节偏移 | 起始 sector (hex) | 文件 |
|---|---|---|---|
| gpt | 0x00000000 | `0x0` | gpt.img |
| uboot_a | 0x00300000 | `0x1800` | uboot_a.img |
| uboot_b | 0x00400000 | `0x2000` | uboot_b.img |
| devinfo_a | 0x00500000 | `0x2800` | devinfo_a.img |
| devinfo_b | 0x00580000 | `0x2c00` | devinfo_b.img |
| boot_a | 0x00600000 | `0x3000` | boot_a.img |
| boot_b | 0x00A00000 | `0x5000` | boot_b.img |
| misc | 0x00E00000 | `0x7000` | misc.img |
| system_a | 0x00E80000 | `0x7400` | system_a.img |
| system_b | 0x03A80000 | `0x1d400` | system_b.img |
| model | 0x06680000 | `0x33400` | model.img |
| log | 0x0C080000 | `0x60400` | log.img |
| data | 0x0CF80000 | `0x67c00` | data.img |
| data_bk | 0x0E780000 | `0x73c00` | data_bk.img |

> `0x5000` sector 正是变砖日志里 SPL 反复尝试 `Trying fit image at 0x5000 sector` 的位置 ——
> 也就是 **boot_b 分区起点**。这与「boot/uboot FIT 被 ⑨ 从 0 开始的误写破坏」的判定一致。
>
> `rkdeveloptool wl` 按**文件实际长度**写入，不要手工指定长度。
> 特别注意 `data_bk`：GPT 表记到 `0x0FF78200`（`0x17F8200`），但备份文件与 mtdparts 都是
> `0x17E0000`（结束 `0x0FF60000`），多出的 99,328 B 没有备份，不要尝试补齐。

**分批写、每批验证，不要一次性写整片：**

```bash
# 第 1 批：只恢复引导链，先让 SPL 能找到 U-Boot
rkdeveloptool wl 0x0     gpt.img
rkdeveloptool wl 0x1800  uboot_a.img
rkdeveloptool wl 0x2000  uboot_b.img
rkdeveloptool wl 0x3000  boot_a.img
rkdeveloptool wl 0x5000  boot_b.img
rkdeveloptool rd          # 复位，看串口是否出现 U-Boot 横幅与 => 提示符
```

出现 `=>` 后再写第 2 批（devinfo / misc / system / model / log / data / data_bk）。
`devinfo_a/b` 含 MAC（业务设备码在 `data` 分区），**必须原样写回**；
`serial#` 来自 CPU efuse，不在 flash 中，无需恢复。见 `INCIDENT.md` 第 3 节。

### 2B. 通过 U-Boot + SD 卡逐分区写（能进 `=>` 时最方便）

备份本来就是用 `mtd read` + `fatwrite` 导出的，反向写回即可。
**每条 `mtd write` 的 size 必须等于文件的实际字节数**（这正是变砖原因）：

```
# 示例：恢复 uboot_a（1 MiB = 0x100000）
fatload mmc 1:1 0x1000000 uboot_a.img
# 必须看到 "1048576 bytes read"
mtd erase spi-nand0 0x300000 0x100000
mtd write spi-nand0 0x1000000 0x300000 0x100000
mtd read  spi-nand0 0x2000000 0x300000 0x100000
cmp.b 0x1000000 0x2000000 0x100000      # 必须无差异
```

绝对禁止写成 `mtd write spi-nand0 0x10` 这类缺 off/size 的形式 —— U-Boot 会把 size 缺省为
**整片 256 MB、offset 缺省为 0**，2026-06-29 就是这么把前段分区全冲掉的。

### 2C. 恢复后的两项必做

1. **补完整备份**（不是只补 loader 区）：按 [`FULL_BACKUP.md`](FULL_BACKUP.md) 通道 A
   做 `0x0–0x10000000` 的 8 × 32 MiB 分块全片 dump —— 现有备份除了缺
   `0x20000–0x300000` 的 loader 区，还缺末尾的 **GPT 备表**（`0x0FF7FE00`）。
   生成好的只读命令清单是 `uboot/01-full-backup.txt`，逐条执行、逐条核对回显；
   **不要**用 `mtd read spi-nand0 0x1000000` 这种不带 off/size 的形式（实测 data abort）。
   导出后用 `python tools/verify_full_dump.py --source chunks --dir ...` 校验并交叉比对 md5。
   若现场只想**最低限度**补 loader 区，仍可用
   `mtd read spi-nand0 0x1000000 0x0 0x300000` + `fatwrite mmc 1 0x1000000 loader-area.img 0x300000`，
   但这只补 2944 KiB 缺口，**不含 GPT 备表、不构成完整备份**。
2. **确认原厂能稳定启动**（出现 `Cmiot login:`、网口正常），再进入移植阶段。

> **双来源提醒**：⑨ 从 offset 0 误写过整片，现场补读到的 loader 区可能已被污染。
> 归档要分成 `golden/`（现有 14 分区，⑨ 之前导出）与 `snapshot/`（现场全片），
> 并找同型号正常机或原厂 `MiniLoaderAll.bin` 交叉验证 loader 区，见 `FULL_BACKUP.md` 第 8 节。
> 在验证之前，不要把 `board.yaml` 的 `loader_area_backed_up` 改成 `true`。

---

## 阶段 3 · 移植 OpenIPC（正确做法）

前提：阶段 2 完成、原厂可回滚、备份（含 loader 区）已校验。

### 3.1 必须同时替换的两个东西

| 分区 | 内容 | 来源 |
|---|---|---|
| `boot_a` (0x600000, 4 MiB) | **OpenIPC 内核 + S4602 DTB 的 `zboot.img`** | `tools/build_s4602.sh` 自行构建；官方 nightly **不含** |
| `system_a` (0xe80000, 44 MiB) | OpenIPC `rootfs.ubi.rv1109`（7,864,320 B = `0x780000`） | 官方 nightly，UBI 几何与本机**实测一致** |

2026-06-29 的错误是**只刷了 rootfs、内核仍是原厂 FIT**。原厂内核会按
`root=/dev/ubiblock0_0 rootfstype=squashfs` 找 squashfs，不会去挂 OpenIPC 的 UBIFS 卷，
所以无论 bootargs 怎么改都起不来。

### 3.2 DTS 参考已升级

`docs/evidence/vendor-kernel.dtb`（从 `boot_a.img` FIT 数据区 `0x800` 提取，85,937 B）
是 **S4602 这块板真实的设备树**，含 SC5338 / GMAC 外置 PHY / ES7202 / PCF8563 / adc-keys /
chosen bootargs。做 OpenIPC 版 DTS 时应以它为板级事实来源，
把 OpenIPC `rockchip-rv11xx` 分支的 `rv1109-38-v10-spi-nand.dts` 当功能基线：

- `sensor` → SC5338（i2c1@0x30，2-lane MIPI，HDR_X2）；OpenIPC 分支**没有** `sc5338.c`，
  但 [`SC5338.md`](SC5338.md) 已备齐驱动资料与寄存器表，按第 6 节编码即可（不需原厂内核源码）
- `bootargs` → `earlycon=uart8250,mmio32,0xff570000 console=ttyFIQ0 ubi.mtd=8 root=ubi0:rootfs rootfstype=ubifs`
  （`system_a` = MTD **8**；官方基线写的是 `ubi.mtd=3`，必须改）
- `mtdparts` → 原厂 14 分区完整串，见 `board/s4602/board.yaml`
- DDR / PHY / GPIO → 按 vendor DTB 逐项核对

### 3.3 `zboot.img` 格式：默认不兼容，必须用原厂 FIT 作模板（2026-11-11 已查清）

**结论**：OpenIPC 官方 `zboot.img` **默认不是 FIT**，直接写进 `boot_a/b` 会 `Not fit magic`。
`scripts/mkimg` 有三个分支，只有传入 `BOOT_IMG` 且 `file` 判为 `Device Tree Blob` 时才走
`repack_itb`（`mkimage -E -p 0x800` → FIT）；OpenIPC firmware 与内核树里都没有 ITB 模板，
所以默认走 `make_boot_img` 产出 Android boot.img 格式。

**解法**：把备份里的**原厂 `boot_a.img` 当 ITB 模板**（实测 `file` →
`Device Tree Blob version 17, size=1536`，`-p 0x800` 与原厂数据区偏移一致），
让 `mkimg` 用 OpenIPC zImage + S4602 DTB 重新打包成同构 FIT，hash 由 mkimage 重算。
`tools/build_s4602.sh` 已按此实现（`BOOT_IMG=boot-template.img`），并校验产物
magic `d00dfeed` 与 ≤ 4 MiB。完整推导见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md) 第 1 节。

写入 flash 之前仍在 RAM 里试，`iminfo` 必须能列出 kernel/fdt/hash OK：

```
fatload mmc 1:1 0x1000000 zboot.img.s4602
iminfo 0x1000000              # 必须列出 kernel / fdt / hash OK
bootm 0x1000000               # 或 boot_fit，以 iminfo 结果为准
```

**不要再 `setenv bootargs`**（见 3.5，实测无效）；内核 cmdline 已由
`CONFIG_CMDLINE_FORCE` 内置，串口里核对 `Kernel command line` 即可。

**地址不能与内核 load address 重叠**：日志证明 FIT 放 `0x02008000` 会撞
`KERNEL (0x02008000-0x02320e00) overlap FIT_USER`，用 `0x1000000` 这类高地址。

### 3.4 A/B 槽策略（这次的关键教训）

**只刷一个槽，另一个槽原封不动留作回滚**（这条红线不变；上次把 `system_a` 和
`system_b` 都刷掉，直接失去回滚）。

⚠️ **但刷哪个槽还没定，且与当前 `board.yaml` 的假设相反**：
活动槽实测是 `_b`（`android_slotsufix=_b`、`ubi.mtd=9`、`boot_fit` 读 `boot_b`），
而 `flash_plan` 现在写的是 `boot_a` / `system_a`。两条路二选一：

| 方案 | 做法 | 代价 |
|---|---|---|
| 刷 `_b`（更省事） | 写 `boot_b` `0x00A00000` + `system_b` `0x03A80000`，`ubi.mtd=9` | 改 `flash_plan` 偏移；`_a` 自动成为原厂回滚槽 |
| 刷 `_a`（保留现状偏移） | 先搞清 misc / env 选槽机制并把活动槽切到 `_a`，`ubi.mtd=8` | 多一个未知项；`misc.img` 实测全 `0xFF` |

`tools/build_s4602.sh` 用 `SLOT=a|b` 切换 `ubi.mtd` 与写入偏移。
未实测确认前，`openipc_blockers.slot_strategy_confirmed` 保持 `false`，
`make_flash_plan.py` 会拒绝放行。详见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md) 4.1。

RAM 里从任一槽试启动（不改 A/B 标记，纯验证）：

```
mtd read spi-nand0 0x1000000 0x600000 0x400000    # boot_a
bootm 0x1000000
```

验证通过、能稳定 SSH 之后再定夺 A/B 标记。**不要像上次那样把 `system_a` 和
`system_b` 都刷掉。**

### 3.5 环境变量与 bootargs：两者都不可靠

这台 U-Boot **没有 `saveenv`**，`env save` 也只打印 usage；启动横幅是
`Using default environment`，重启后 `printenv bootcmd` 恢复默认值。
所以「先 setenv 试、成功再保存」不成立。

**更关键的发现：连 `setenv bootargs` 都改不动 root 参数。**
变砖记录 3643 行设了 `root=ubi0:rootfs rootfstype=ubifs`，3706 行内核实际收到的仍是
`root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=9`；日志里 7 次启动的 cmdline 全都如此。
U-Boot 把 `vendor-kernel.dtb` 的 `chosen bootargs` 与 env 合并、按 slot 注入参数，
**因此「把 bootargs 写进 DTB chosen」也不保险**（本段旧建议已证伪）。

可靠的做法是**内核 `CONFIG_CMDLINE_FORCE=y`**，把完整 cmdline 写进 `CONFIG_CMDLINE`
（必须含 `mtdparts`，因为 `CONFIG_MTD_CMDLINE_PARTS=y` 依赖它），彻底忽略 bootloader
传参。`tools/build_s4602.sh` 第 3 步已实现；构建后须实测打印的
`Kernel command line` 确认 FORCE 生效。详见 [`BOOT_FEASIBILITY.md`](BOOT_FEASIBILITY.md) 第 2 节。

---

## 阶段 4 · 红线（本次事故新增）

1. `mtd write` 四参数齐全：`<name> <addr> <off> <size>`，**size = 实际文件字节数**，
   且写完后 `mtd read` 回读 + `cmp.b` 比对。
2. 禁止对 `spi-nand0` 使用缺省 off/size 的写命令；禁止写入长度 > 文件长度。
3. 禁止同时刷两个 A/B 槽；至少保留一套可启动的原厂组合。
4. 禁止把 `rootfs.ubi` 当可启动镜像喂给 `bootm`。
5. 禁止在没确认 `iminfo` 能识别前把 `zboot.img` 写进 boot 分区。
6. 不碰 `0x0–0x300000`（loader/SPL/ddr bin）；现有备份不含它，弄坏就真没救。
7. 每一步都在串口留原始日志；任何一条报错立即停手，不要连续粘贴多行命令
   （上次大量 `=> => xxx` 导致命令被当成未知命令）。

---

## 优先级

| P | 动作 |
|---|---|
| **P0** | 接串口确认当前是「SPL 失败」还是能进 `=>`；同时看 USB 是否枚举 `2207` |
| **P0** | 取得与 LPDDR4 924 MHz 匹配的原厂 loader / MiniLoaderAll.bin（向固件包或同型机索取） |
| **P0** | 若能枚举 `2207`，按 [`FULL_BACKUP.md`](FULL_BACKUP.md) 通道 B 先做一份**全片只读快照**（`rl`），把 ⑨ 之后的 flash 现状固定下来 |
| **P1** | 按 2A 第 1 批只恢复 gpt + uboot_a/b + boot_a/b，重启验证能进 U-Boot |
| **P1** | 能进 U-Boot 后按 `FULL_BACKUP.md` 通道 A 做 8 × 32 MiB 全片 dump，恢复其余分区，验证原厂可启动 |
| **P2** | 在 Ubuntu 构建机构建带 S4602 DTB 的 `zboot.img`，用 `vendor-kernel.dtb` 核对板级节点 |
| **P2** | RAM 里 `iminfo` + `bootm` 验证内核格式，再只刷 `_a` 槽，验收 SSH 与 RTSP 出图 |
