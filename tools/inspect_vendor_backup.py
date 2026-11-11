#!/usr/bin/env python3
"""解析 S4602 原厂分区备份，输出可复现的取证结果。

只读工具：不写设备、不修改备份目录（除非显式 --extract）。

用法：
    python tools/inspect_vendor_backup.py
    python tools/inspect_vendor_backup.py --json
    python tools/inspect_vendor_backup.py --extract   # 额外导出内核 DTB 到 docs/evidence/

取证内容：
  * 14 个分区镜像的 size / md5 / 格式（GPT / FIT / UBI / 原始）
  * GPT 分区表（LBA -> 字节偏移）与备份覆盖缺口
  * boot/uboot FIT 的子镜像节点、大小、是否带签名节点
  * UBI 卷名（启发式定位卷表 record）
  * 内核 DTB 与 zImage 在 boot_a.img 中的位置
"""
import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "移动千里眼S4602原厂固件备份"

# 实测分区表（来自 U-Boot printenv / 内核 mtdparts / GPT 三方一致）
PARTITIONS = [
    ("gpt",       0x00000000, 0x00020000),
    ("uboot_a",   0x00300000, 0x00100000),
    ("uboot_b",   0x00400000, 0x00100000),
    ("devinfo_a", 0x00500000, 0x00080000),
    ("devinfo_b", 0x00580000, 0x00080000),
    ("boot_a",    0x00600000, 0x00400000),
    ("boot_b",    0x00A00000, 0x00400000),
    ("misc",      0x00E00000, 0x00080000),
    ("system_a",  0x00E80000, 0x02C00000),
    ("system_b",  0x03A80000, 0x02C00000),
    ("model",     0x06680000, 0x05A00000),
    ("log",       0x0C080000, 0x00F00000),
    ("data",      0x0CF80000, 0x01800000),
    ("data_bk",   0x0E780000, 0x017E0000),
]
FLASH_SIZE = 0x10000000  # 256 MiB


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kind(head: bytes) -> str:
    if head[:4] == b"UBI#":
        return "UBI"
    if head[:4] == b"\xd0\x0d\xfe\xed":
        return "FIT/DTB"
    if head[:4] == b"hsqs":
        return "squashfs"
    if head[:4] == b"\x27\x05\x19\x56":
        return "uImage"
    if head[:8] == b"DEVINFO!":
        return "devinfo"
    if head[512:520] == b"EFI PART":
        return "GPT"
    if head[:4] == b"\xff" * 4:
        return "erased(0xff)"
    if head[:4] == b"\x00" * 4:
        return "zeros"
    return "raw"


def parse_gpt(data: bytes) -> list:
    if data[512:520] != b"EFI PART":
        return []
    hdr = data[512:604]
    count = struct.unpack("<I", hdr[80:84])[0]
    entsz = struct.unpack("<I", hdr[84:88])[0]
    out = []
    for i in range(count):
        e = data[1024 + i * entsz:1024 + (i + 1) * entsz]
        if e[:16] == b"\x00" * 16:
            continue
        name = e[56:128].decode("utf-16-le", "replace").split("\x00")[0]
        first, last = struct.unpack("<QQ", e[32:48])
        out.append({
            "index": i, "name": name,
            "offset": first * 512, "size": (last - first + 1) * 512,
        })
    return out


def fdt_nodes(data: bytes) -> dict:
    """简易 FDT 遍历，返回 {属性名: [值...]} 与节点名列表（不依赖 dtc）。"""
    if data[:4] != b"\xd0\x0d\xfe\xed":
        return {"nodes": [], "props": {}}
    h = struct.unpack(">10I", data[:40])
    totalsize, off_struct, off_strings, size_strings, size_struct = h[1], h[2], h[3], h[8], h[9]
    strtab = data[off_strings:off_strings + size_strings]

    def gstr(o):
        e = strtab.find(b"\x00", o)
        return strtab[o:e].decode("utf-8", "replace")

    p, depth, stack = off_struct, 0, []
    nodes, props = [], {}
    limit = min(len(data), off_struct + size_struct)
    while p + 12 <= limit:
        tag = struct.unpack(">I", data[p:p + 4])[0]
        if tag == 1:  # BEGIN_NODE
            e = data.find(b"\x00", p + 4)
            nm = data[p + 4:e].decode("utf-8", "replace")
            stack.append(nm)
            nodes.append("/" + "/".join(stack))
            p = (e + 4) & ~3
            depth += 1
        elif tag == 2:
            if stack:
                stack.pop()
            p += 4
            depth -= 1
        elif tag == 3:  # PROP
            sz, no = struct.unpack(">II", data[p + 4:p + 12])
            p += 12
            val = data[p:p + sz]
            p = (p + sz + 3) & ~3
            key = gstr(no)
            props.setdefault(key, []).append(val)
        elif tag == 4:
            p += 4
        elif tag == 9:
            break
        else:
            break
    return {"nodes": nodes, "props": props, "totalsize": totalsize}


def parse_fit(data: bytes) -> dict:
    info = fdt_nodes(data)
    if not info["nodes"]:
        return {}
    sizes = {}
    for raw in info["props"].get("data-size", []):
        if len(raw) == 4:
            sizes[len(sizes)] = struct.unpack(">I", raw)[0]
    return {
        "totalsize": info["totalsize"],
        "nodes": info["nodes"],
        "subimage_sizes": sizes,
        "has_signature_node": any(n.endswith("/signature") for n in info["nodes"]),
        "has_hash_node": any(n.endswith("/hash") for n in info["nodes"]),
        "description": next(
            (v.rstrip(b"\x00").decode("utf-8", "replace")
             for v in info["props"].get("description", []) if len(v) < 200), ""),
    }


def ubi_volumes(data: bytes) -> list:
    """启发式定位 UBI 卷表 record：name 位于 record+16，name_len 位于 +14。

    卷表可能出现在镜像中部（实测 system_a.img 在 ~21 MiB 处），
    因此按已知卷名在整个文件里搜索，而不是只扫开头。
    """
    out = []
    for nm in (b"rootfs", b"rootfs_data", b"model", b"log", b"data", b"data_bk"):
        needle = nm + b"\x00"
        pos = 0
        while True:
            i = data.find(needle, pos)
            if i < 0:
                break
            pos = i + 1
            rec = data[i - 16:i - 16 + 128]
            if len(rec) < 128:
                continue
            nl = struct.unpack(">H", rec[14:16])[0]
            reserved = struct.unpack(">I", rec[0:4])[0]
            if nl != len(nm) or not (1 <= reserved <= 100000):
                continue
            item = {"name": nm.decode(), "reserved_pebs": reserved, "vol_type": rec[12]}
            if item not in out:
                out.append(item)
    return out[:8]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--extract", action="store_true", help="导出 boot_a FIT 内的内核 DTB")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"备份目录不存在: {args.dir}", file=sys.stderr)
        return 1

    report = {"dir": str(args.dir), "files": {}, "gpt": [], "fit": {},
              "ubi": {}, "coverage": {}, "extracted": []}

    for path in sorted(args.dir.iterdir()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        head = data[:520]
        entry = {"size": len(data), "md5": md5(path), "format": kind(head)}
        report["files"][path.name] = entry
        if entry["format"] == "GPT":
            report["gpt"] = parse_gpt(data)
        elif entry["format"] == "FIT/DTB" and path.name.startswith(("boot_", "uboot_")):
            report["fit"][path.name] = parse_fit(data)
        elif entry["format"] == "UBI":
            report["ubi"][path.name] = ubi_volumes(data)

    covered = [(n, o, s) for n, o, s in PARTITIONS
               if (args.dir / f"{n}.img").is_file()
               and (args.dir / f"{n}.img").stat().st_size == s]
    total = sum(s for _, _, s in covered)
    gaps = []
    cursor = 0
    for _, o, s in sorted(covered, key=lambda x: x[1]):
        if o > cursor:
            gaps.append({"offset": cursor, "size": o - cursor})
        cursor = max(cursor, o + s)
    if cursor < FLASH_SIZE:
        gaps.append({"offset": cursor, "size": FLASH_SIZE - cursor})
    report["coverage"] = {"files": len(covered), "bytes": total, "gaps": gaps}

    if args.extract:
        boot = args.dir / "boot_a.img"
        if boot.is_file():
            blob = boot.read_bytes()
            fit = parse_fit(blob)
            sizes = fit.get("subimage_sizes", {})
            fdt_size, kernel_size = sizes.get(0, 0), sizes.get(1, 0)
            outdir = ROOT / "docs" / "evidence"
            outdir.mkdir(parents=True, exist_ok=True)

            # FIT 数据区：fdt 子镜像从 0x800 开始（已由启动日志与 magic 双重确认）
            if fdt_size and blob[0x800:0x804] == b"\xd0\x0d\xfe\xed":
                out = outdir / "vendor-kernel.dtb"
                out.write_bytes(blob[0x800:0x800 + fdt_size])
                report["extracted"].append({"file": str(out), "offset": 0x800,
                                            "size": fdt_size})
                print(f"已导出 {out} (offset 0x800, {fdt_size} B)", file=sys.stderr)

            # zImage magic 在镜像头 +0x24；按 FIT 记录的 kernel data-size 截断
            magic = b"\x18\x28\x6f\x01"
            i = blob.find(magic, 0x400)
            if i >= 0 and kernel_size:
                start = i - 0x24
                out = outdir / "vendor-zimage.bin"
                out.write_bytes(blob[start:start + kernel_size])
                report["extracted"].append({"file": str(out), "offset": start,
                                            "size": kernel_size})
                print(f"已导出 {out} (offset 0x{start:x}, {kernel_size} B)", file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("S4602 原厂分区备份取证")
    print("=" * 78)
    print(f"{'文件':<16}{'大小':>12}  {'格式':<12} md5")
    for name, e in report["files"].items():
        print(f"{name:<16}{e['size']:>12}  {e['format']:<12} {e['md5']}")
    print("\n---- GPT 分区表 ----")
    for p in report["gpt"]:
        print(f"  {p['index']:>2} {p['name']:<11} 0x{p['offset']:08x} +0x{p['size']:08x}")
    print("\n---- FIT 结构 ----")
    for name, f in report["fit"].items():
        print(f"  {name}: {f.get('description','')}")
        print(f"    子镜像: {', '.join(n.rsplit('/',1)[-1] for n in f['nodes'][:12])}")
        print(f"    sizes={f['subimage_sizes']} hash={f['has_hash_node']} signature={f['has_signature_node']}")
    print("\n---- UBI 卷 ----")
    for name, vols in report["ubi"].items():
        print(f"  {name}: {vols}")
    cov = report["coverage"]
    print(f"\n---- 覆盖 ----\n  {cov['files']} 个分区 / {cov['bytes']} bytes "
          f"({cov['bytes']/1024/1024:.1f} MiB) / flash {FLASH_SIZE/1024/1024:.0f} MiB")
    for g in cov["gaps"]:
        print(f"  缺口 0x{g['offset']:08x} +0x{g['size']:08x} ({g['size']/1024:.0f} KiB)")
    print("\n提示: 0x20000-0x300000 是 BootROM loader/SPL/ddr bin 区域，备份未覆盖。")
    print("      该区域当前仍可运行 SPL，说明尚未损坏；一旦损坏，现有备份无法恢复它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
