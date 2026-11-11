# S4602 完整备份 SOP

前置阅读：[`INCIDENT.md`](INCIDENT.md)（变砖根因、现有备份清单）、[`RECOVERY.md`](RECOVERY.md)（救砖流程）。
配套工具：[`tools/make_backup_plan.py`](../tools/make_backup_plan.py)、[`tools/verify_full_dump.py`](../tools/verify_full_dump.py)。
参数账本：[`board/s4602/board.yaml`](../board/s4602/board.yaml)。

> 本文只讨论**只读**操作。生成的命令里不含 `mtd write` / `mtd erase` / `wl` / `ef`。
> 当前设备是半砖（SPL 能跑、进不了 `=>`），所以**备份通道要先选对**。

---

## 0. "完整备份"有两层含义，必须先拆开

| 维度 | 现有状态 | 说明 |
|---|---|---|
| **范围完整**（coverage） | ❌ 252.5 MiB / 256 MiB = **98.63 %** | 14 个 GPT 分区都在，但缺 `0x20000–0x300000`（2944 KiB，loader/SPL/ddr bin）和 `0x0FF60000–0x10000000`（640 KiB 尾区，**内含 GPT 备表**） |
| **内容干净**（fidelity） | ⚠️ **存疑** | 2026-06-29 第 ⑨ 步 `mtd write spi-nand0 0x10` 从 offset 0 开始写整片并 `data abort`。前段究竟被覆盖了多少**无法从日志确定** |

解析 `gpt.img` 的 GPT 头得到 `backup_lba = 523263` → **GPT 备表头在 `0x0FF7FE00`**，
条目区在 `0x0FF7BE00`（32 sectors）。它在块层可达范围内（< `0x0FF80000`），
但 `gpt.img` 只 dump 了 `0x0–0x20000`，`data_bk` 也只到 `0x0FF60000`
—— **现有 14 个文件把 GPT 备表整个漏掉了**。这又是一条必须补读末尾的理由。

**这是最容易被忽略的一点**：把缺的 3.5 MiB 补读出来，只解决了"范围完整"；
如果那 2944 KiB 已经被 ⑨ 写坏，补到的是**一份损坏的 loader 快照**，不是原厂 loader。

因此正确的归档策略是**双来源**（见第 8 节）：

- `golden/` = 现有 14 个分区备份（2026-06-29 11:41，⑨ **之前**导出，干净可信）
- `snapshot/` = 现场从芯片读出的全片（含 loader 间隙，可能已污染）
- 两者交叉比对，loader 区必须再找**同型号正常机**或原厂 `MiniLoaderAll.bin` 印证

---

## 1. 两个"256 MiB"不一样（会算错扇区的坑）

| 口径 | 来源 | 字节范围 | 大小 |
|---|---|---|---|
| **MTD 全片** | `mtd list`：`0x000000000000-0x000010000000 : "spi-nand0"` | `0x0 – 0x10000000` | 268,435,456 B = **256 MiB** |
| **块层 / LBA** | 启动横幅：`Capacity: 255.5 MB (523264 x 512)`、`rkdeveloptool rl` | `0x0 – 0x0FF80000` | 267,911,168 B = **255.5 MiB** |
| 差值 | 块层少报 | `0x0FF80000 – 0x10000000` | 0x80000 = **512 KiB** |

- `mtd read spi-nand0 ...` 能读到 **0x10000000**（全片）。
- `rkdeveloptool rl` 最多只能读 **523264 = 0x7FC00 个扇区**，末尾 512 KiB 取不到。
- 这 512 KiB 是坏块/BBT 预留，通常是 0xFF；但**要拿到真正的 256 MiB 全片，只能走 MTD 通道或编程器**。

几何（实测 `mtd list`）：page `0x800` (2048 B)、eraseblock `0x20000` (128 KiB)、
OOB `128 B`（可用 63 B）、ECC 8 bit / 512 B。所有 `mtd` 的 off/size 必须是 `0x800` 的倍数。

---

## 2. 完整备份的四层

| 层 | 内容 | 为什么需要 | 本机通道 |
|---|---|---|---|
| **L1 逻辑全片** | `0x0 – 0x10000000` 逐字节，含所有分区、间隙、GPT 主表/备表 | 唯一能"整片还原"的镜像；也是唯一包含 SPL / ddr bin 的逻辑备份 | 通道 A（U-Boot `mtd read`）或通道 D |
| **L2 OOB 物理层** | 每 page 128 B OOB，全片共 `131072 × 128 = 16 MiB` | 编程器克隆、坏块标记、出厂 ECC 字节；逻辑恢复一般用不到 | `mtd read.raw.oob` 或编程器 |
| **L3 坏块表** | `mtd bad spi-nand0` 输出 | **决定 L1 能不能按偏移还原**（见第 3 节） | 通道 A 前置 |
| **L4 身份 / 物料** | `devinfo_a/b` 的 **MAC** `48:E5:33:B5:60:D6`（@0x34）、`data` 分区的业务设备码 `sn = 8nF4Gk5xHjCrhN2ap…`、芯片丝印、DDR 型号与频率、DDR bin 版本 | 换片、重建 loader、找回网络身份 | 已有 devinfo/data + 拍照 + 日志 |

**身份字段纠错**（2026-11-11 逐个备份文件扫描的结果，推翻了之前的写法）：

- `devinfo_a/b` 里**只有 MAC**（头部 `DEVINFO!`，ASCII MAC 在 `0x34`），**没有序列号**。
- `447f56c0594ecd4e` 来自 `rockchip-cpuinfo` 的 `Serial` 与 U-Boot env `serial#`，
  是 **CPU efuse 派生的芯片固有值**，不存于任何 flash 分区 —— 同一颗芯片永远读得到，**无需备份**。
  它在 `log.img` 里出现只是日志文本。
- `data.img` 里另有一串 `version:2 / mac = 48:E5:33:B5:60:D6 / sn = 8nF4Gk5xHjCrhN2ap…`
  （base64 形态的业务设备码/凭证），**这个才在 flash 里、必须备份**，已在 `data.img` 中。
- 需要单独离线保存的是 **MAC + 业务设备码**，不是 CPU Serial。

L4 里目前**唯一没记录**的是 NAND 芯片丝印（`board.yaml` 的 `chip_mark: null`），
只有 `sfc_nand id: e5 f2 e5`，不足以确定具体料号。拆机时补拍。

---

## 3. 坏块：决定"分块 dump 能不能拼成全片"

查 U-Boot `cmd/mtd.c` 的 `do_mtd_io()` 实现，`mtd read` 的循环里有：

```c
/* Skip the block if it is bad */
if (mtd_is_aligned_with_block_size(mtd, off) && mtd_block_isbad(mtd, off)) {
        off += mtd->erasesize;
        continue;
}
```

跳过坏块时 **不推进 `datbuf`、不减少 `remaining`**，即缓冲区里不留空洞，继续往后找好块，
直到读满 `size` 字节。结论：

- **若 `mtd bad spi-nand0` 无输出（无坏块）**：`mtd read <off> <size>` 与物理偏移一一对应，分块拼接 = 有效全片镜像。
- **若有坏块**：读出的 `size` 字节并非 `[off, off+size)`，拼接后偏移会漂移。此时 L1 只能当"按好块顺序的逻辑流"保存，**不能按偏移写回**；恢复必须走同样的 MTD 层顺序写入，或改用编程器 raw 镜像。

所以备份第一步永远是 `mtd bad spi-nand0`，把输出贴进日志。现场从未执行过这条命令，坏块情况**目前未知**。

---

## 4. 通道选择（按设备当前状态）

| 能进 `=>`？ | 推荐通道 | 覆盖 | 前置 |
|---|---|---|---|
| ❌（**当前半砖**） | **通道 B**：MASKROM + `rkdeveloptool rl` | 255.5 MiB | USB OTG 枚举 `2207` + 匹配的 DDR bin（`db`） |
| ❌ 且 B 不通 | **通道 D**：拆片编程器 | 256 MiB + OOB | 拆焊，风险最高 |
| ✅（救砖后） | **通道 A**：U-Boot `mtd read` 分块 + `fatwrite` → SD 卡 | **256 MiB** | FAT32 SD 卡 ≥ 1 GiB |
| ✅ 且原厂 Linux 能起 | 通道 C 作补充：`dd if=/dev/mtdN` | 仅 14 分区 | 有可写挂载点 |

> **通道 B 是纯读操作，现在就能做，不会进一步损坏设备**（只要不执行 `wl` / `ef`）。
> 但它读到的 loader 区可能已被 ⑨ 污染 —— 必须归档为 `snapshot`，不能当 golden。

---

## 5. 通道 A · U-Boot 分块全片 dump（救砖后首选）

### 5.1 为什么必须分块

变砖记录 **1293 行**实测过一次整片读：

```
=> mtd read spi-nand0 0x1000000
Reading 268435456 byte(s) (131072 page(s)) at offset 0x00000000
data abort ... ### ERROR ### Please RESET the board ###
```

不带 `<off> <size>` 会缺省为整片 256 MiB，直接 `data abort`。
反之 **1828 行**读 90 MiB (`0x5a00000`) 到同一地址是成功的。
因此取 **32 MiB / 块 × 8 块**（保守，远低于已验证的 90 MiB 上限）：

- 装载地址 `0x1000000`（16 MiB），每块占 `0x2000000`，峰值 `0x3000000`（48 MiB）—— 在已验证可用 RAM 区内。
- off 全部 `0x2000000` 对齐，size 是 `0x800` 的倍数。
- `fatwrite` 的 size **必须等于** `mtd read` 的 size（小于会截断，大于会写出 RAM 垃圾）。

生成好的完整命令清单见 [`uboot/01-full-backup.txt`](../uboot/01-full-backup.txt)，
由 `python tools/make_backup_plan.py --mode uboot --out uboot/01-full-backup.txt` 生成，避免手敲错偏移。

### 5.2 前置（只读）

```
mmc dev 1
fatls mmc 1
mtd list
mtd bad spi-nand0
```

`mtd bad` 的输出**必须记录**。若列出坏块，按第 3 节处理，不要把分块当可直接按偏移还原的镜像。

### 5.3 分块表（8 块，逐条执行）

| # | off | size | 输出文件 | `fatwrite` size |
|---|---|---|---|---|
| 00 | `0x00000000` | `0x2000000` | `full-00.bin` | `0x2000000` |
| 01 | `0x02000000` | `0x2000000` | `full-01.bin` | `0x2000000` |
| 02 | `0x04000000` | `0x2000000` | `full-02.bin` | `0x2000000` |
| 03 | `0x06000000` | `0x2000000` | `full-03.bin` | `0x2000000` |
| 04 | `0x08000000` | `0x2000000` | `full-04.bin` | `0x2000000` |
| 05 | `0x0A000000` | `0x2000000` | `full-05.bin` | `0x2000000` |
| 06 | `0x0C000000` | `0x2000000` | `full-06.bin` | `0x2000000` |
| 07 | `0x0E000000` | `0x2000000` | `full-07.bin` | `0x2000000` |

每块两条命令（以 #00 为例）：

```
mtd read spi-nand0 0x1000000 0x00000000 0x2000000
fatwrite mmc 1 0x1000000 full-00.bin 0x2000000
```

**逐条执行、逐条看输出**：必须出现 `Reading 33554432 byte(s) ...` 与 `33554432 bytes written`。
任何一条报错立即停手，**不要连续粘贴多行**（⑨ 就是粘贴被截断导致参数丢失）。

### 5.4 L2 可选：带 OOB 的物理层备份

`cmd/mtd.c` 里 `mtd read.oob` 输出的是 **data + OOB 拼接**（不是纯 OOB）：

```
buf[0 .. L-1]              = data
buf[L .. L + N*O - 1]      = OOB     其中 N = L / 0x800 页，O = 128 B
```

对 32 MiB 块：`N = 16384`，OOB = `0x200000` (2 MiB)，
所以 `fatwrite` 的 size 必须是 **`0x2200000`**（`0x2000000 + 0x200000`），不是 `0x2000000`：

```
mtd read.raw.oob spi-nand0 0x1000000 0x00000000 0x2000000
fatwrite mmc 1 0x1000000 full-00.raw.bin 0x2200000
```

`.raw` 用 `MTD_OPS_RAW` 取未经 ECC 重排的原始 OOB，才是物理克隆需要的。
**注意两点**：

1. `mtd read` 路径走 `map_sysmem(user_addr, 0)`，**U-Boot 不检查缓冲区长度**，调用者自己保证 RAM 够；
   `0x1000000 + 0x2200000 = 0x3200000`（50 MiB）在已验证范围内。
2. 本机是 U-Boot **2017.09**，与主线 `cmd/mtd.c` 可能有差异。L2 建议先只做一个 eraseblock
   （`mtd read.raw.oob ... 0x0 0x20000` → 期望 `0x20000 + 0x2000 = 0x22000` B）核对实际字节数，
   确认布局后再跑全片。**L2 是可选的，L1 才必须做。**

### 5.5 每块落盘后立即在 PC 侧校验

```bash
# 文件大小必须全部是 33554432
stat -c '%n %s' full-*.bin
python tools/verify_full_dump.py --source chunks --dir <SD卡挂载点> --out full-256M.img
```

不要攒到最后才校验 —— SD 卡写满、FAT 目录项损坏这类问题只有当场能补读。

---

## 6. 通道 B · MASKROM / rockusb（当前半砖唯一非拆机通道）

官方 `rkdeveloptool` 命令表（[Rockchip 文档](https://opensource.rock-chips.com/index.php?title=Rkdeveloptool)）：

```
DB <Loader>                  DownloadBoot
RL <BeginSec> <SectorLen> <File>     ReadLBA
RFI / RCI / RID / TD / RD            只读信息 / 测试 / 复位
```

### 6.1 流程（全读，不写）

```bash
lsusb | grep 2207
rkdeveloptool ld                 # 确认是 Maskrom
rkdeveloptool rfi                # 记录 Flash 容量，核对是否 523264 sectors
rkdeveloptool rci                # 记录芯片信息
rkdeveloptool rid                # 记录 Flash ID
# 关键：Maskrom 下 flash 读写需要先初始化 DDR 并运行 usbplug
rkdeveloptool db <MiniLoaderAll.bin>
rkdeveloptool td                 # 连通性测试，通过才继续
rkdeveloptool rl 0x00000 0x10000 snap-00.bin
rkdeveloptool rl 0x10000 0x10000 snap-01.bin
...
rkdeveloptool rl 0x70000 0x0FC00 snap-07.bin   # 最后一段只到 0x7FC00
rkdeveloptool rd
```

`rl` 的 `<BeginSec>` / `<SectorLen>` 都是 **512 字节扇区号**。
8 段合计 `7 × 0x10000 + 0xFC00 = 0x7FC00 = 523264` sectors = `0x0FF80000` B。

### 6.2 两个硬风险

1. **`db` 需要匹配的 DDR bin**。本机实测 **LPDDR4 512 MB @ 924 MHz**（`DDR Version V1.08 20210825`）。
   rkbin 的 `RKBOOT/RV1126MINIALL.ini` 默认给 `rv1126_ddr_924MHz_v1.16.bin`，
   但 `bin/rv11/` 有 52 个按 DDR3/DDR4/LP3/LP4 与频率细分的 blob，选错表现为 `db` 后设备无响应。
   **优先向原厂固件包索取本机 `MiniLoaderAll.bin`，或从同型号正常机 dump `0x0–0x300000`；
   rkbin 只能作为没有更好来源时的候选，且必须挑 `_lp4_` / 924 MHz。**
2. **`rl` 覆盖不到末尾 512 KiB**（第 1 节）。这份快照是 255.5 MiB，不是 256 MiB，
   归档时文件名与校验报告要标明 `lba-limited`。

### 6.3 若能通过 U-Boot 进 rockusb

救砖后可用 `download` / `rockusb` 让设备进入 rockusb 模式 —— 此时 DDR 已初始化，
主机侧**不需要 `db`**，直接 `rl` 即可读。比 Maskrom 路径安全得多。
当前半砖进不了 `=>`，这条路暂不可用。

---

## 7. 通道 C / D（补充与终极）

**通道 C · 原厂 Linux**（能出现 `Cmiot login:` 时）：
`cat /dev/mtdN > /mnt/sd/<name>.img` 可导出分区，但 cmdlinepart 只创建了 14 个 MTD 分区，
**`0x20000–0x300000` 的间隙没有设备节点，读不到**。所以它只能复核 14 分区，不能替代通道 A/B。
需要含 OOB 时用 `nanddump -o`（busybox 未必提供）。

**通道 D · 拆片编程器**：
唯一能拿到真正物理级镜像（data + OOB + 坏块标记，不做坏块跳过）的方式，
也是 loader 区被彻底破坏后的最后手段。代价是拆焊、需要编程器支持该 SPI-NAND 料号
（丝印未记录，`e5 f2 e5` 只是 ID）。**排在最后。**

---

## 8. 双来源归档（本项目的正确做法）

```
backup/
├── golden/          # 现有 14 个 .img（⑨ 之前导出，干净）
│   └── 原目录：移动千里眼S4602原厂固件备份/
├── snapshot/        # 现场全片 dump（通道 A 或 B，可能含被污染的 loader 区）
│   ├── full-00.bin … full-07.bin
│   └── full-256M.img          # verify_full_dump.py 拼接产物
├── meta/
│   ├── mtd-bad.txt            # mtd bad spi-nand0 原始输出
│   ├── mtd-list.txt           # mtd list 原始输出
│   ├── serial-boot.log        # 完整上电串口日志
│   ├── chip-photos/           # NAND / DDR / sensor / PHY 丝印
│   └── md5sum.txt
└── external/        # 同型号正常机 dump 的 0x0–0x300000，或原厂 MiniLoaderAll.bin
```

**交叉校验逻辑**（`verify_full_dump.py` 自动做）：

1. 从 `snapshot/full-256M.img` 按 `board.yaml` 的 14 个偏移切出分区，逐个与 `golden/` 的 md5 比对。
   - **全部一致** → ⑨ 没有污染 GPT 分区，snapshot 可信度高。
   - **前段（gpt / uboot_a/b / boot_a/b）不一致** → ⑨ 确实覆盖了前段，这些分区以 golden 为准。
2. 检查 `0x20000–0x300000` 的非零比例：全 0xFF / 全 0x00 = 没读到有效 loader，备份仍然不完整。
3. 本工程**不自动产出可写的 `recovery-full.img`**：`verify_full_dump.py` 只做校验与拼接。
   实际恢复按 [`RECOVERY.md`](RECOVERY.md) 2A **逐分区写回**（用 golden 的 14 个文件），
   loader 间隙不写、原样保留。只有拿到 `external/` 的干净 loader 镜像并比对一致后，
   才值得另行构建整片写回镜像，且必须显式标注来源与验证状态。

> 绝不要因为"补了 3.5 MiB"就把 `board.yaml` 的 `loader_area_backed_up` 直接改成 `true`。
> 该字段的语义是**有干净、可恢复的 loader 备份**。

---

## 9. 校验清单

```bash
# 1. 生成命令清单（纯文本，不碰设备）
python tools/make_backup_plan.py --mode uboot  --out uboot/01-full-backup.txt
python tools/make_backup_plan.py --mode rockusb --out docs/evidence/snapshot-plan.txt

# 2. 工具自检（用 golden 合成全片，验证解析逻辑，不产生大文件）
python tools/verify_full_dump.py --source golden

# 3. 现场 dump 回来后
python tools/verify_full_dump.py --source chunks --dir snapshot/ --out snapshot/full-256M.img
python tools/verify_full_dump.py --source image  --image snapshot/full-256M.img --json > snapshot/meta/report.json
```

`verify_full_dump.py` 会检查：每块长度、拼接总长、全片 md5、
GPT 主表（`0x200` 处 `EFI PART`）与备表、14 分区 md5 与 golden 的逐个比对、
loader 间隙有效字节比例、尾区内容。**只读**，只有显式 `--out` 才写拼接文件。

---

## 10. 红线

1. **备份通道里不允许出现任何写命令**：`mtd write` / `mtd erase` / `wl` / `ef` / `gpt` / `prm` / `ul` 一律禁止。
2. `mtd read` **必须带齐 `<off> <size>`**；不带参数会缺省整片 256 MiB → 实测 `data abort`。
3. `fatwrite` 的 size 必须等于 `mtd read` 的 size（L2 带 OOB 时是 `0x2200000`）。
4. 先 `mtd bad spi-nand0` 再决定 dump 能否按偏移还原；有坏块时不得直接按偏移写回。
5. 逐条执行、逐条核对串口回显，禁止批量粘贴多行命令。
6. SD 卡 FAT32、≥ 1 GiB、剩余空间 ≥ 512 MiB；每块落盘立刻 `stat` 核对长度。
7. `devinfo_a/b` 的 MAC 与 `data` 分区的业务设备码，**单独再复制一份到 `meta/` 并离线保存**；
   `serial#` 是芯片固有值，不在备份范围内。
8. 补出来的 loader 区在拿到外部干净来源比对之前，一律标记 `unverified`。

---

## 优先级

| P | 动作 | 为什么现在做 |
|---|---|---|
| **P0** | 接 USB OTG 看是否枚举 `2207`；若能，走通道 B 先做一份**全片只读快照** | 半砖状态下唯一不拆机、不改硬件的读全片机会；⑨ 之后 flash 内容还在变化，越早固定越好 |
| **P0** | 索取匹配 LPDDR4 924 MHz 的原厂 `MiniLoaderAll.bin` / 同型机 `0x0–0x300000` dump | 既是 `db` 的前置，也是 loader 区"干净来源"的唯一判据 |
| **P1** | 按 `RECOVERY.md` 2A 救砖，恢复出 `=>` | 解锁通道 A（唯一能读满 256 MiB 的通道） |
| **P1** | 进 `=>` 后立刻 `mtd bad` + 通道 A 分块全片 dump + `verify_full_dump.py` 交叉校验 | 拿到范围完整、可与 golden 比对的 L1 |
| **P2** | 补拍 NAND / DDR 丝印，归档 L4 物料信息 | 换片、编程器、重建 loader 都要用 |
| **P3** | 可选做 L2 OOB 备份（先单块试布局） | 只在需要考虑物理级克隆时才必要 |
