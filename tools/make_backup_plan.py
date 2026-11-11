#!/usr/bin/env python3
"""生成 S4602 全片备份的只读命令清单。

安全性：本脚本只生成 **读** 命令，绝不产生 mtd write / mtd erase / wl / ef / gpt / prm / ul。
生成结果需要人工逐条执行，脚本本身不接触设备。

用法：
    python tools/make_backup_plan.py --mode uboot --out uboot/01-full-backup.txt
    python tools/make_backup_plan.py --mode rockusb --out docs/evidence/snapshot-plan.txt
    python tools/make_backup_plan.py --mode both          # 打印到 stdout
    python tools/make_backup_plan.py --list               # 只打印分块/扇区表

依据：
  * mtd list: 0x0-0x10000000 (256 MiB), page 0x800, eraseblock 0x20000, OOB 128
  * 启动横幅: 523264 x 512 = 0x0FF80000 (块层上限)
  * 变砖记录 1293 行: 不带 off/size 的 mtd read 整片 -> data abort
  * 变砖记录 1828 行: 90 MiB 单次读取成功 -> 32 MiB/块是安全选择
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FLASH_SIZE = 0x10000000        # 256 MiB，MTD 全片
CHUNK = 0x2000000              # 32 MiB / 块
LOAD_ADDR = 0x1000000          # 现场实测可用的 RAM 装载地址
PAGE = 0x800                   # 2048 B
OOB = 128                      # mtd list: OOB size
NCHUNKS = FLASH_SIZE // CHUNK  # 8
LBA_SECTORS = 523264           # 块层容量 (0x7FC00 sectors)
SECTOR = 512
SECTORS_PER_CHUNK = CHUNK // SECTOR   # 0x10000

# GPT 备表位置（从 gpt.img 的 GPT 头 backup_lba 字段解析）
GPT_BACKUP_LBA = 523263
GPT_BACKUP_OFF = GPT_BACKUP_LBA * SECTOR   # 0x0FF7FE00


def chunk_rows():
    rows = []
    for i in range(NCHUNKS):
        off = i * CHUNK
        rows.append({
            "idx": i,
            "off": off,
            "size": CHUNK,
            "name": f"full-{i:02d}.bin",
            "oob_size": CHUNK + (CHUNK // PAGE) * OOB,
        })
    return rows


def lba_rows():
    rows = []
    cursor = 0
    for i in range(NCHUNKS):
        begin = i * SECTORS_PER_CHUNK
        if begin >= LBA_SECTORS:
            break
        count = min(SECTORS_PER_CHUNK, LBA_SECTORS - begin)
        rows.append({
            "idx": i,
            "begin": begin,
            "count": count,
            "name": f"snap-{i:02d}.bin",
            "bytes": count * SECTOR,
        })
        cursor = begin + count
    return rows


def print_tables():
    print(f"MTD 全片 0x0-0x{FLASH_SIZE:08x} ({FLASH_SIZE/1024/1024:.0f} MiB), "
          f"{NCHUNKS} 块 × 0x{CHUNK:x}")
    print(f"{'#':<3}{'off':<14}{'size':<12}{'file':<16}{'read.oob size':<16}")
    for r in chunk_rows():
        print(f"{r['idx']:<3}0x{r['off']:08x}    0x{r['size']:08x}   "
              f"{r['name']:<16}0x{r['oob_size']:08x}")
    print(f"\n块层 LBA 上限 {LBA_SECTORS} sectors (0x{LBA_SECTORS:x}) "
          f"= 0x{LBA_SECTORS*SECTOR:08x}")
    print(f"{'#':<3}{'BeginSec':<14}{'SectorLen':<14}{'file':<16}{'bytes':<12}")
    for r in lba_rows():
        print(f"{r['idx']:<3}0x{r['begin']:08x}    0x{r['count']:08x}     "
              f"{r['name']:<16}{r['bytes']:<12}")
    print(f"\n块层读不到的尾部: 0x{LBA_SECTORS*SECTOR:08x}-0x{FLASH_SIZE:08x} "
          f"({(FLASH_SIZE-LBA_SECTORS*SECTOR)//1024} KiB)")
    print(f"GPT 备表头: 0x{GPT_BACKUP_OFF:08x} (backup_lba={GPT_BACKUP_LBA})")


def uboot_plan():
    L = []
    a = L.append
    a("# S4602 全片备份 —— U-Boot 通道（只读，由 make_backup_plan.py 生成）")
    a("#")
    a("# 前提：能进 \"=>\" 提示符；FAT32 SD 卡 >= 1 GiB，剩余 >= 512 MiB。")
    a("# 逐条执行，逐条核对串口回显；任何一条报错立即停手。")
    a("# 禁止连续粘贴多行命令（2026-06-29 的变砖就是粘贴被截断导致参数丢失）。")
    a("# 详细 SOP：docs/FULL_BACKUP.md")
    a("")
    a("# ---- 前置：确认 SD 卡、几何、坏块表 ----")
    a("mmc dev 1")
    a("fatls mmc 1")
    a("mtd list")
    a("mtd bad spi-nand0")
    a("# ↑ 把 mtd bad 的输出完整记进 meta/mtd-bad.txt。")
    a("#   有坏块时 mtd read 会跳过且不留空洞，分块不能按偏移直接还原，见 FULL_BACKUP.md 第 3 节。")
    a("")
    a("# ---- 8 块 × 32 MiB：每条必须看到")
    a("#     Reading 33554432 byte(s) ... / 33554432 bytes written ----")
    for r in chunk_rows():
        a(f"# --- 块 {r['idx']:02d}  off 0x{r['off']:08x} -> 0x{r['off']+r['size']:08x} ---")
        a(f"mtd read spi-nand0 0x{LOAD_ADDR:x} 0x{r['off']:08x} 0x{r['size']:x}")
        a(f"fatwrite mmc 1 0x{LOAD_ADDR:x} {r['name']} 0x{r['size']:x}")
        a("")
    a("# ---- 可选 L2（含 OOB）：先用一块试布局，不要直接跑全片 ----")
    r0 = chunk_rows()[0]
    a(f"# mtd read.raw.oob spi-nand0 0x{LOAD_ADDR:x} 0x{r0['off']:08x} 0x{PAGE*64:x}")
    a(f"# fatwrite mmc 1 0x{LOAD_ADDR:x} oob-test.bin 0x{PAGE*64 + 64*OOB:x}")
    a(f"# 确认布局后，每块 fatwrite 的 size 应为 0x{r0['oob_size']:x}（不是 0x{r0['size']:x}）")
    a("")
    a("# ---- PC 侧校验（在主机上做，不是 U-Boot）----")
    a("# stat -c '%n %s' full-*.bin        # 每个必须是 33554432")
    a("# python tools/verify_full_dump.py --source chunks --dir <SD卡目录> --out snapshot/full-256M.img")
    a("")
    a("# 红线：本清单不含任何 mtd write / mtd erase。")
    a(f"# mtd read 必须带齐 off/size，不带参数会缺省整片 0x{FLASH_SIZE:x} 并 data abort。")
    return "\n".join(L) + "\n"


def rockusb_plan():
    L = []
    a = L.append
    a("# S4602 全片快照 —— MASKROM / rockusb 通道（只读，由 make_backup_plan.py 生成）")
    a("#")
    a("# 适用：当前半砖、进不了 \"=>\"、但 USB OTG 能枚举 VID 0x2207。")
    a("# 全部是读命令；db 只把 loader 下载到 RAM，不写 flash。")
    a("# 绝不要执行 wl / ef / gpt / prm / ul —— 那是写操作。")
    a("# 详细 SOP：docs/FULL_BACKUP.md 第 6 节")
    a("")
    a("lsusb | grep 2207")
    a("rkdeveloptool ld")
    a("rkdeveloptool rfi              # 记录 Flash 容量，核对是否 523264 sectors")
    a("rkdeveloptool rci              # 记录芯片信息")
    a("rkdeveloptool rid              # 记录 Flash ID")
    a("")
    a("# db 的 loader 必须匹配本机 LPDDR4 512MB @ 924MHz (DDR Version V1.08 20210825)。")
    a("# 优先用原厂 MiniLoaderAll.bin 或同型号正常机 dump；rkbin 只能作候选。")
    a("# 把 <MiniLoaderAll.bin> 换成实际文件后再执行，不要原样粘贴。")
    a("rkdeveloptool db <MiniLoaderAll.bin>")
    a("rkdeveloptool td               # 连通性测试，通过才继续")
    a("")
    for r in lba_rows():
        a(f"rkdeveloptool rl 0x{r['begin']:x} 0x{r['count']:x} {r['name']}")
    a("")
    a("rkdeveloptool rd")
    a("")
    a("# ---- 覆盖说明 ----")
    a(f"# 合计 0x{LBA_SECTORS:x} sectors = 0x{LBA_SECTORS*SECTOR:08x} B = 255.5 MiB")
    a(f"# 块层读不到 0x{LBA_SECTORS*SECTOR:08x}-0x{FLASH_SIZE:08x} 的 512 KiB；")
    a(f"# GPT 备表头 0x{GPT_BACKUP_OFF:08x} 在可达范围内，务必确认它出现在 snap-07.bin 中。")
    a("# 这份是「当前状态快照」，loader 区可能已被 2026-06-29 第 ⑨ 步污染，")
    a("# 归档为 snapshot/，不要当 golden。见 FULL_BACKUP.md 第 8 节。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["uboot", "rockusb", "both"], default="both")
    ap.add_argument("--out", type=Path, help="写入文件；不指定则打印到 stdout")
    ap.add_argument("--list", action="store_true", help="只打印分块/扇区表")
    args = ap.parse_args()

    if args.list:
        print_tables()
        return 0

    if args.mode == "uboot":
        text = uboot_plan()
    elif args.mode == "rockusb":
        text = rockusb_plan()
    else:
        text = uboot_plan() + "\n" + "=" * 78 + "\n\n" + rockusb_plan()

    # 安全自检：只检查可执行行（注释里说明红线是允许的）
    code_lines = [ln.strip() for ln in text.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    write_patterns = (
        r"^mtd\s+(write|erase)\b",
        r"^rkdeveloptool\s+(wl|wlx|ef|gpt|prm|ul)\b",
        r"^(wl|wlx|ef|gpt|prm|ul)\s",
    )
    for line in code_lines:
        for pat in write_patterns:
            if re.search(pat, line, re.I):
                print(f"ERROR: 生成的可执行行含写命令，拒绝输出: {line!r}",
                      file=sys.stderr)
                return 2

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"已生成 {args.out} ({len(text.splitlines())} 行，纯只读命令)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
