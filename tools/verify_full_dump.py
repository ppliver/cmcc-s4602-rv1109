#!/usr/bin/env python3
"""校验 / 拼接 S4602 全片备份，并与 golden 分区备份交叉比对。

只读工具：不写设备、不修改备份目录。只有显式 --out 才写出拼接后的镜像。

用法：
    # 工具自检：用 golden 的 14 个分区合成一张全片，验证解析逻辑
    python tools/verify_full_dump.py --source golden

    # 现场 U-Boot 分块 dump（8 × 32 MiB = 256 MiB）
    python tools/verify_full_dump.py --source chunks --dir snapshot/ --out snapshot/full-256M.img

    # 现场 rockusb rl 快照（255.5 MiB，块层上限）
    python tools/verify_full_dump.py --source lba --dir snapshot/

    # 已有单一全片镜像
    python tools/verify_full_dump.py --source image --image snapshot/full-256M.img --json

检查项：
  * 分块长度 / 拼接总长 / 全片 md5
  * GPT 主表 (@0x200) 与备表 (backup_lba 指向的位置)
  * 14 个分区按偏移切出后与 golden 的 md5 逐个比对（判断 ⑨ 是否污染前段）
  * loader 间隙 0x20000-0x300000 与尾区的有效字节比例
  * devinfo 中的 MAC / SN

依据：docs/FULL_BACKUP.md、board/s4602/board.yaml、docs/INCIDENT.md
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_vendor_backup import PARTITIONS, FLASH_SIZE, md5  # noqa: E402

GOLDEN_DIR = ROOT / "移动千里眼S4602原厂固件备份"
CHUNK = 0x2000000
NCHUNKS = FLASH_SIZE // CHUNK
LBA_SECTORS = 523264
SECTOR = 512
LBA_SIZE = LBA_SECTORS * SECTOR          # 0x0FF80000 = 255.5 MiB
GPT_BACKUP_LBA = 523263                 # 由 gpt.img 的 GPT 头解析得到
GPT_BACKUP_OFF = GPT_BACKUP_LBA * SECTOR

LOADER_GAP = (0x00020000, 0x00300000)
TAIL_GAP = (0x0FF60000, FLASH_SIZE)
MAC = b"48:E5:33:B5:60:D6"
# 447f56c0594ecd4e 是 rockchip-cpuinfo 从 CPU efuse 读出的 Serial（U-Boot env serial#），
# 不在任何 flash 分区里，因此不做存在性校验；见 analyze() 里的说明。


def effective_ratio(data, start, end):
    """返回 (非0xFF且非0x00 的字节数, 总数)。全片镜像里 0xFF/0x00 之外的视为有效内容。"""
    seg = bytes(data[start:min(end, len(data))])
    if not seg:
        return 0, 0
    useful = len(seg) - seg.count(0xFF) - seg.count(0x00)
    return max(useful, 0), len(seg)


def md5_slice(data, off, size):
    """用 memoryview 算分区 md5，避免为大分区再复制一份内存。"""
    return hashlib.md5(memoryview(data)[off:off + size]).hexdigest()


def load_golden_synthetic():
    """用现有 14 个分区按偏移合成一张 256 MiB 全片，间隙填 0xFF。仅用于自检。"""
    img = bytearray(b"\xff" * FLASH_SIZE)
    filled = []
    for name, off, size in PARTITIONS:
        p = GOLDEN_DIR / f"{name}.img"
        if not p.is_file():
            raise FileNotFoundError(f"golden 缺少 {p}")
        blob = p.read_bytes()
        if len(blob) != size:
            raise ValueError(f"{p.name} 大小 {len(blob)} != 期望 {size}")
        img[off:off + size] = blob
        filled.append(name)
    return img, filled


def load_chunks(directory: Path, prefix: str, expected: list):
    out = bytearray()
    info = []
    for i, size in enumerate(expected):
        p = directory / f"{prefix}{i:02d}.bin"
        if not p.is_file():
            raise FileNotFoundError(f"缺少分块 {p}")
        blob = p.read_bytes()
        info.append({"file": p.name, "size": len(blob), "expected": size,
                     "ok": len(blob) == size})
        if len(blob) != size:
            raise ValueError(f"{p.name} 大小 {len(blob)} != 期望 {size}（0x{size:x}）")
        out += blob
    return out, info


def analyze(data, source: str, chunk_info=None):
    # 保持 bytearray，避免为 256 MiB 镜像再复制一份造成 ~512 MB 内存峰值
    total = len(data)
    full = (total == FLASH_SIZE)
    report = {
        "source": source,
        "size": total,
        "size_mib": round(total / 1024 / 1024, 3),
        "full_mtd_image": full,
        "md5": hashlib.md5(data).hexdigest(),
        "chunks": chunk_info or [],
    }

    # GPT 主表 / 备表
    report["gpt_primary"] = data[0x200:0x208] == b"EFI PART"
    backup_lba = GPT_BACKUP_LBA
    if report["gpt_primary"]:
        backup_lba = int.from_bytes(data[512 + 32:512 + 40], "little")
    report["gpt_backup_lba"] = backup_lba
    report["gpt_backup_offset"] = backup_lba * SECTOR
    boff = backup_lba * SECTOR
    report["gpt_backup_present"] = (
        boff + 8 <= total and data[boff:boff + 8] == b"EFI PART")
    report["gpt_backup_entries_offset"] = (backup_lba - 32) * SECTOR

    # 分区 md5 与 golden 交叉比对
    parts = []
    for name, off, size in PARTITIONS:
        if off + size > total:
            parts.append({"name": name, "offset": off, "size": size,
                          "status": "out-of-range"})
            continue
        digest = md5_slice(data, off, size)
        gp = GOLDEN_DIR / f"{name}.img"
        golden = md5(gp) if gp.is_file() else None
        parts.append({
            "name": name, "offset": off, "size": size,
            "md5": digest, "golden_md5": golden,
            "status": "match" if golden and digest == golden else
                      ("no-golden" if golden is None else "MISMATCH"),
        })
    report["partitions"] = parts
    report["matched"] = sum(p["status"] == "match" for p in parts)
    report["mismatched"] = [p["name"] for p in parts if p["status"] == "MISMATCH"]

    # gap 分析
    lu, lt = effective_ratio(data, *LOADER_GAP)
    tu, tt = effective_ratio(data, *TAIL_GAP)
    report["loader_gap"] = {
        "range": f"0x{LOADER_GAP[0]:08x}-0x{LOADER_GAP[1]:08x}",
        "useful_bytes": lu, "total_bytes": lt,
        "useful_pct": round(lu / lt * 100, 2) if lt else 0.0,
    }
    report["tail_gap"] = {
        "range": f"0x{TAIL_GAP[0]:08x}-0x{TAIL_GAP[1]:08x}",
        "useful_bytes": tu, "total_bytes": tt,
        "useful_pct": round(tu / tt * 100, 2) if tt else 0.0,
    }
    gb_start = report["gpt_backup_entries_offset"]
    gb_useful, gb_total = effective_ratio(data, gb_start, LBA_SIZE)
    report["gpt_backup_area"] = {
        "range": f"0x{gb_start:08x}-0x{LBA_SIZE:08x}",
        "useful_bytes": gb_useful,
        "total_bytes": gb_total,
    }

    # 身份信息：
    #   MAC 在 devinfo_a/b @0x34；
    #   业务设备码 (sn = ...) 在 data 分区；
    #   U-Boot serial# / rockchip-cpuinfo 的 447f56c0594ecd4e 是芯片 efuse 派生的 CPU Serial，
    #   不存放于 flash，换不了芯片也不会丢，因此不在备份校验项里。
    d_off = next(o for n, o, s in PARTITIONS if n == "devinfo_a")
    d_size = next(s for n, o, s in PARTITIONS if n == "devinfo_a")
    dat_off = next(o for n, o, s in PARTITIONS if n == "data")
    dat_size = next(s for n, o, s in PARTITIONS if n == "data")
    m = re.search(rb"sn = ([A-Za-z0-9+/=_-]{8,80})",
                  data[dat_off:dat_off + dat_size])
    report["identity"] = {
        "mac_in_devinfo": MAC in data[d_off:d_off + d_size],
        "business_sn_in_data": bool(m),
        "business_sn_prefix": m.group(1)[:24].decode("ascii", "replace") if m else None,
        "chip_serial_note": "447f56c0594ecd4e 来自 rockchip-cpuinfo (CPU efuse)，不在 flash 中，无需备份",
    }

    # 结论
    notes = []
    if source == "golden":
        notes.append("这是用 golden 分区合成的自检镜像，不是现场 dump，不可作为备份归档。")
    if not full:
        notes.append(f"不是 256 MiB 全片（{total} B）；块层快照缺末尾 512 KiB。")
    if report["mismatched"]:
        notes.append("⑨ 的误写污染了这些分区: " + ", ".join(report["mismatched"])
                     + " —— 恢复时以 golden 文件为准。")
    if report["loader_gap"]["useful_pct"] < 1:
        notes.append("loader 间隙几乎全是 0xFF/0x00：没读到有效 loader，备份仍不完整。")
    elif source != "golden":
        notes.append("loader 间隙有内容，但来源未经外部干净镜像验证，必须标记 unverified。")
    if not report["gpt_backup_present"]:
        notes.append("GPT 备表缺失：现有 14 分区备份不含它，需现场补读末尾。")
    if not report["gpt_primary"]:
        notes.append("GPT 主表签名缺失，前段可能已被破坏。")
    if not notes:
        notes.append("范围完整；仍需外部干净 loader 镜像交叉验证后才可升级为 golden 全片。")
    report["notes"] = notes
    return report


def print_report(r):
    W = 78
    print("=" * W)
    print(f"S4602 全片镜像校验  source={r['source']}")
    print("=" * W)
    size_ok = "OK (256 MiB 全片)" if r["full_mtd_image"] else "非全片"
    print(f"总长        {r['size']} B ({r['size_mib']} MiB)  {size_ok}")
    print(f"全片 md5    {r['md5']}")
    if r["chunks"]:
        print(f"分块        {sum(1 for c in r['chunks'] if c['ok'])}/{len(r['chunks'])} 长度正确")
    print(f"GPT 主表    @0x00000200  " + ("EFI PART OK" if r["gpt_primary"] else "缺失"))
    print(f"GPT 备表    @0x{r['gpt_backup_offset']:08x}  "
          + ("EFI PART OK" if r["gpt_backup_present"] else "缺失 <- 需现场补读"))
    lg, tg = r["loader_gap"], r["tail_gap"]
    print(f"loader 间隙 {lg['range']}  有效 {lg['useful_pct']}% "
          f"({lg['useful_bytes']}/{lg['total_bytes']} B)")
    print(f"尾区        {tg['range']}  有效 {tg['useful_pct']}%")
    ident = r["identity"]
    print(f"身份信息    devinfo MAC={'OK' if ident['mac_in_devinfo'] else '未找到'}  "
          f"data 业务码={'OK' if ident['business_sn_in_data'] else '未找到'}"
          + (f" ({ident['business_sn_prefix']}...)" if ident["business_sn_prefix"] else ""))
    print(f"            芯片 Serial {ident['chip_serial_note']}")
    print(f"\n分区 md5 与 golden 比对：{r['matched']}/{len(r['partitions'])} 一致")
    for p in r["partitions"]:
        mark = {"match": "OK", "MISMATCH": "差异", "no-golden": "无基准",
                "out-of-range": "越界"}[p["status"]]
        print(f"  {p['name']:<11} {mark:<5} {p.get('md5', '-')}")
    print(f"\n结论：")
    for n in r["notes"]:
        print(f"  - {n}")
    print("=" * W)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["golden", "chunks", "lba", "image"],
                    default="chunks")
    ap.add_argument("--dir", type=Path, default=ROOT / "snapshot")
    ap.add_argument("--image", type=Path, help="--source image 时的单一全片镜像")
    ap.add_argument("--out", type=Path, help="写出拼接后的镜像（默认不写）")
    ap.add_argument("--allow-synthetic-out", action="store_true",
                    help="允许把 golden 合成镜像写出（默认拒绝，防止误当真实 dump）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    chunk_info = None
    try:
        if args.source == "golden":
            data, _ = load_golden_synthetic()
            if args.out and not args.allow_synthetic_out:
                print("拒绝把 golden 合成镜像写出：它不是现场 dump。"
                      "确需写出请加 --allow-synthetic-out。", file=sys.stderr)
                return 2
        elif args.source == "chunks":
            data, chunk_info = load_chunks(args.dir, "full-", [CHUNK] * NCHUNKS)
        elif args.source == "lba":
            sizes = [min(CHUNK, LBA_SIZE - i * CHUNK) for i in range(NCHUNKS)]
            sizes = [s for s in sizes if s > 0]
            data, chunk_info = load_chunks(args.dir, "snap-", sizes)
        else:
            if not args.image or not args.image.is_file():
                print("--source image 需要 --image <文件>", file=sys.stderr)
                return 2
            data = args.image.read_bytes()
    except (FileNotFoundError, ValueError) as e:
        print(f"读取失败: {e}", file=sys.stderr)
        return 1

    report = analyze(data, args.source, chunk_info)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(data)
        report["out"] = str(args.out)
        if not args.json:
            print(f"已写出 {args.out} ({len(data)} B)", file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
