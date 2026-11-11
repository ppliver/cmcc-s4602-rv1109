# S4602 变砖复盘（2026-06-29 操作记录）

证据：`qianliyan原厂固件启动信息.log`（原厂正常启动）、`s4602变砖过程_20260629_133651.txt`（9177 行现场串口记录）、
`移动千里眼S4602.md`（整理笔记）、`移动千里眼S4602原厂固件备份/`（14 个分区镜像）。

**状态判定：半砖。** BootROM → DDR bin → SPL 仍然完好（能看到 `DDR Version V1.08`、
`U-Boot SPL board init`、OP-TEE sha256 校验 OK），但 SPL 找不到 U-Boot FIT，
**进不了 `=>` 控制台**。因此所有需要 U-Boot 命令的恢复手段当前不可用。

---

## 1. 时间线与根因

| 阶段 | 行号 | 操作 | 后果 |
|---|---|---|---|
| ① 完整备份 | 1640–1833 | `mtd read spi-nand0 0x1000000 <off> <size>` + `fatwrite mmc 1 ...` 逐个导出 14 个分区 | **成功**，备份现在就在本地，md5 见第 3 节 |
| ② 误把 rootfs 当内核 | 1957–1960 | `fatload ... rootfs.ubi.rv1109` 后 `bootm 0x1000000` | `can't get kernel image!`（UBI 不是可启动镜像，属正常） |
| ③ 命令集探错 | 1964 / 1977 / 1982 | `ubi part` → `Unknown command 'ubi'`；`nand erase` → `no devices available`；`saveenv` → `Unknown command` | 确认这台 U-Boot **没有 ubi / 没有可用 nand 设备 / 没有 saveenv** |
| ④ 刷 system_a | 2069–2074 | `fatload` 读入 **7,733,248 B**，却 `mtd write ... 0xe80000 **0x2c00000**`（46,137,344 B） | 写入长度写成「整个分区」，超出文件长度的 38 MB 是 RAM 里的垃圾数据 |
| ⑤ 错误 bootcmd | 2076 / 2088 | `mtd read ... 0xa00000 ...; bootm 0x02008000` | FIT 放在 0x02008000，而 FIT 内 kernel load 也是 0x02008000 → `Sysmem Error: KERNEL overlap FIT_USER` |
| ⑥ 换地址仍失败 | 2461 / 7835–7837 | 改 0x1000000 再用 `bootm 0x1000000 - 0x08300000` | FIT 内 kernel `Load Address: 0xffffff01` → `data abort` → CPU reset |
| ⑦ 环境变量未持久化 | 6953–6955 / 6948 | `saveenv` 不存在；`env save` 只打印 usage；重启后 `printenv bootcmd` 仍是默认 `boot_fit;...` | **好消息**：bootcmd/bootargs 没被写死进 flash |
| ⑧ 两个槽都毁 | 7965–7970 | 对 **system_b (0x3a80000)** 重复 ④ 的写法 | A/B 双分区的回滚能力一并丧失 |
| ⑨ **致命一步** | 8840–8842 | 本想恢复 `system_b.img`，但粘贴的 `mtd write spi-nand0 0x10` 被截断 | U-Boot 把 `0x10` 当 addr，off/size 取缺省 → `Writing **268435456** byte(s) at offset **0x00000000**` → 从 0 开始写满整片 → `data abort` |
| ⑩ 最终状态 | 9005–9021 / 9161–9177 | `Not fit magic` → `Trying fit image at 0x5000 sector` → `fit_find_config_node: Missing FDT description` → `SPL: failed to boot from all boot devices` | **uboot_a/b 或 boot 分区的 FIT 被 ⑨ 覆盖**，SPL 起不来 U-Boot |

**根因一句话**：第 ⑨ 步一条参数不全的 `mtd write` 从 flash 偏移 0 开始写了整片 256 MB，
覆盖了 gpt / loader 保留区 / uboot_a/b / boot_a/b 等前段分区；
而 ④⑤⑧ 已经把两个 system 槽都写坏且从未替换内核，导致在此之前就已无回滚路径。

**注意**：⑨ 之后 SPL 仍能打印 DDR 日志，说明 BootROM 加载 SPL 的那段区域**尚未被完全破坏**
（很可能 data abort 在写完前若干个块之前就发生了）。这是现在还能救的前提。

---

## 2. 为什么当时那条路本来就不可能成功

即使没有 ⑨，前面的做法也起不来 OpenIPC：

1. **只刷了 rootfs，没刷内核。** `boot_b` 分区始终是原厂 FIT（Linux 4.19.111 + 原厂 DTB +
   `root=/dev/ubiblock0_0 rootfstype=squashfs` 的启动逻辑）。OpenIPC 的 `rootfs.ubi` 是
   UBIFS 卷，原厂内核不会按 `root=ubi0:rootfs` 去挂它，更没有 majestic 所需的 OpenIPC 内核配置。
   **必须同时替换 boot 分区的 `zboot.img`（内核 + S4602 DTB）**。
2. **bootargs 写法不对。** 用过 `root=/dev/ubi0_0`、`ubi.mtd=system_a`、`console=ttyFIQ0,1500000`。
   正确的是 `root=ubi0:rootfs rootfstype=ubifs ubi.mtd=<数字>`，且 `ubi.mtd` 只接受 MTD 编号
   （system_a=8 / system_b=9），不接受分区名。第 3643 行那次 `root=ubi0:rootfs rootfstype=ubifs` 是对的。
3. **A/B 槽没留后路。** 当前 slot 是 `_b`，只刷 `_a` 时原厂仍能正常启动（日志 3486 行确实回到
   `Cmiot login:`）；把 `system_b` 也刷掉之后，两个槽同时失效。
4. **`mtd write` 的 size 必须是真实文件长度**，不是分区大小。④⑧ 都写成 `0x2c00000`，
   用未初始化的 RAM 填满了分区尾部。
5. **这台 U-Boot 没有 `saveenv`**，所以「先 setenv 试启动、成功后保存」的思路不成立；
   bootargs 曾被认为要写进内核 DTB 的 `chosen`，但 2026-11-11 复核 7 次启动日志发现
   **U-Boot 连 `setenv bootargs` 的 root 参数都会覆盖**（3643 → 3706 行），
   正确解法是内核 `CONFIG_CMDLINE_FORCE=y`，见 `docs/BOOT_FEASIBILITY.md` 第 2 节。

---

## 3. 备份清单（已本地校验）

14 个文件、252.5 MiB。`python tools/inspect_vendor_backup.py` 可复现全部结果。

| 分区 | 偏移 | 大小 | 格式 | md5 |
|---|---|---|---|---|
| gpt | 0x00000000 | 128 KiB | GPT | `52732a786a105a20d269ee769fc93134` |
| uboot_a | 0x00300000 | 1 MiB | FIT | `c8987b2380025e68e441be04ed11de72` |
| uboot_b | 0x00400000 | 1 MiB | FIT | `c8987b2380025e68e441be04ed11de72` |
| devinfo_a | 0x00500000 | 512 KiB | devinfo | `f90261d5c4b642bf2bba1dff352b1584` |
| devinfo_b | 0x00580000 | 512 KiB | devinfo | `0e5b8c68ccf5afe9cf0eb3047deec207` |
| boot_a | 0x00600000 | 4 MiB | FIT | `e005d8a14a94ac9c5bcf165c641d0d78` |
| boot_b | 0x00A00000 | 4 MiB | FIT | `8eccc74a60884b6a48c12021740bcf75` |
| misc | 0x00E00000 | 512 KiB | 全 0xFF | `c3820feab36216484292bab0b625b530` |
| system_a | 0x00E80000 | 44 MiB | UBI(卷 rootfs, squashfs) | `3e5c37fbb261ed36d52c8df1574ba429` |
| system_b | 0x03A80000 | 44 MiB | UBI(卷 rootfs, squashfs) | `458bc1e356c67e1a6109e670b6974f87` |
| model | 0x06680000 | 90 MiB | UBI | `615dca05939d10d7252c07c90b25bd6a` |
| log | 0x0C080000 | 15 MiB | UBI(卷 log) | `4f28b6dffca3aaec6bad2e72f83f3fc9` |
| data | 0x0CF80000 | 24 MiB | UBI(卷 data) | `14e1eb99cb9b46b1afe89c91a9150010` |
| data_bk | 0x0E780000 | 23.875 MiB | UBI(卷 data_bk) | `0fb076a1ff43ce91eaeac0b4b27ab9fa` |

要点：
- `uboot_a` 与 `uboot_b` md5 **完全相同** → 两个槽的 U-Boot 一致，恢复时写哪个都对。
- `boot_a` 与 `boot_b` md5 不同（A/B 镜像内容有差异），**不要互相顶替**。
- `devinfo_*` 头部 `DEVINFO!`，**只含 MAC `48:E5:33:B5:60:D6`（ASCII @0x34），不含 SN**。
  必须原样保留的是 MAC 与 `data.img` 里的业务设备码 `sn = 8nF4Gk5xHjCrhN2ap…`。
  `serial#=447f56c0594ecd4e` 是 `rockchip-cpuinfo` 从 CPU efuse 读出的芯片 Serial，
  **不在 flash 分区里**，同一颗芯片永远读得到，无需备份（之前写成「devinfo 含序列号」有误，
  2026-11-11 逐文件扫描后纠正）。
- `gpt.img` 只有 615 字节有效数据（GPT 表头 + 14 条目），**不含 idbloader / ddr bin / SPL**。
- **口径差异**：GPT 表把 `data_bk` 记到 LBA 523200（结束 `0x0FF78200`，长 `0x17F8200`），
  而内核 `mtdparts` 与备份文件都是 `0x17E0000`（结束 `0x0FF60000`），差 99,328 B。
  备份是按 mtdparts 读的，**恢复时按文件实际长度写，不要用 GPT 的结束 LBA**（多出的部分没有备份）。

### 备份缺口（必须知道）

| 区域 | 大小 | 说明 |
|---|---|---|
| `0x00020000 – 0x00300000` | **2944 KiB** | BootROM 加载的 loader / SPL / ddr bin 所在保留区，**备份完全没覆盖** |
| `0x0FF60000 – 0x0FF80000` | 128 KiB | 尾部：含 **GPT 备表**（`backup_lba=523263` → 备表头 `0x0FF7FE00`、条目区 `0x0FF7BE00`），现有备份也没有 |
| `0x0FF80000 – 0x10000000` | 512 KiB | 块层（`rkdeveloptool rl` / 523264 sectors）读不到的最后一段，只有 MTD 或编程器能取 |

也就是说：**这份备份能恢复所有 GPT 分区，但恢复不了 SPL/ddr bin 本身，也没有 GPT 备表。**
目前 SPL 还活着，所以不影响救砖；但只要后续再伤到 0x20000–0x300000，
现有备份就无能为力，只能走 MASKROM + rkbin 或编程器。

**完整备份怎么做**（分通道、分块命令、坏块/OOB、双来源归档、校验工具）见
[`FULL_BACKUP.md`](FULL_BACKUP.md)。特别提醒：把缺口补读出来只解决「范围完整」，
⑨ 从 offset 0 误写过整片，补到的 loader 区**可能已被污染**，必须按该文档第 8 节做双来源比对。

---

## 4. 实测得到的硬事实（推翻了之前的「未验证」）

| 项 | 实测值 | 来源 |
|---|---|---|
| SoC | **RV1109**（`rockchip-cpuinfo: SoC 11090000`），U-Boot/DTB 写 RV1126 是共用 BSP | dmesg |
| DDR | **LPDDR4 512 MB @ 924 MHz**，`DDR Version V1.08 20210825`；DTB 里 "DDR3 Board" 是模板残留 | SPL 日志 |
| Flash | SPI-NAND **256 MiB**，id `e5 f2 e5`，PEB 128 KiB / LEB 126976 / min I/O 2048 | dmesg / UBI |
| Sensor | **SmartSens SC5338**（ID `0x00ce50`），i2c1 @0x30，**2-lane MIPI** | dmesg |
| 音频 / RTC | ES7202（PDM）、PCF8563 (i2c4@0x51) | dmesg |
| 网口 | GMAC + **外置 PHY**（`integrated PHY? (no)`），100 Mbps；md 笔记写 YT8512B，DTB 中未见型号字符串，**待确认** | dmesg |
| MAC / 业务码 | MAC `48:E5:33:B5:60:D6` (devinfo) / `sn = 8nF4Gk5xHjCrhN2ap…` (data) | 备份镜像扫描 |
| CPU Serial | `447f56c0594ecd4e`，`rockchip-cpuinfo` / U-Boot `serial#`，**CPU efuse 派生，不在 flash 中** | dmesg / printenv |
| 串口 | **1500000 8N1**，3.3 V，`console=ttyFIQ0` | printenv |
| U-Boot | 2017.09 (2021-10-28) + SPL 2017.09-g61c5d7f (2023-06-09) + OP-TEE 3.6.0 | 启动横幅 |
| 签名 | **`## Verified-boot: 0`**、`FIT: no signed, no conf required`；FIT 内部有 sha256，但**无强制公钥签名** | SPL 日志 |
| 可打断 | `Hit key to stop autoboot('CTRL+C')` → `=>` **可以** | 原厂日志 97–103 行 |
| 命令集 | 有 `mtd read/write/erase`、`tftp`、`fatload/fatwrite`、`rockusb`、`download`、`boot_fit`、`gpt`、`env`；**没有** `ubi`、`saveenv`，`nand` 无设备 | help 输出 |
| 内核加载 | `bootcmd=boot_fit;boot_android ...`，从 **boot_a/b 分区读 FIT**；FIT 含 kernel + fdt + resource | printenv / 启动日志 |
| rootfs | UBI 卷 `rootfs` 内是 squashfs，经 `ubi.block=0,rootfs` 挂成 `/dev/ubiblock0_0` | dmesg |
| 分区表 | 14 个 GPT 分区，A/B 双槽；完整偏移见 `board/s4602/board.yaml` | GPT + mtdparts 三方一致 |
| UBI 几何 | PEB 131072 / LEB 126976 / min I/O 2048 / VID 2048 / data 4096 | dmesg |
| 官方 rootfs.ubi | 同样的 PEB/LEB/min I/O → **可直接写进 system 分区，无需改 ubinize 参数** | `verify_image.py` |

**「担心运营商定制机有内核签名校验」已被证伪**（`Verified-boot: 0`）。
这直接把之前报告里的方案 C 排除掉，也让「不必编译 U-Boot」从推断变成实证。

---

## 5. 已提取的证据文件

- `docs/evidence/vendor-kernel.dtb` — 从 `boot_a.img` 的 FIT（数据区偏移 `0x800`）提取的
  **原厂内核 DTB，85,937 B**，含 S4602 真实的 sensor / GMAC / ES7202 / PCF8563 / adc-keys /
  chosen bootargs 节点。这是做 S4602 DTS 的黄金参考，优先级高于官方 Longse 评估板 DTS。
- `docs/evidence/rkbin-tree.json`、`uboot-tree.json` — 引导链取证（上一轮）。
- `tools/inspect_vendor_backup.py` — 可复现解析 GPT / FIT / UBI 卷 / md5 / 覆盖缺口。

---

## 6. 下一步

见 [`docs/RECOVERY.md`](RECOVERY.md)：先救砖 → 补备份 loader 区 → 再按实证路径移植 OpenIPC。
