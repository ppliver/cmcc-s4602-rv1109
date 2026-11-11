#!/usr/bin/env python3
"""校验 OpenIPC RV1109 NAND 固件的 UBI 结构与 md5。

只读取本地文件，不接触设备。用法：
    python tools/verify_image.py firmware/rootfs.ubi.rv1109
    python tools/verify_image.py firmware/openipc.rv1109-nand-lite.tgz --md5 5c1849...
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

EC_MAGIC = b"UBI#"
VID_MAGIC = b"UBI!"


def parse_ubi(data: bytes) -> dict:
    offs_raw = []
    i = 0
    while True:
        i = data.find(EC_MAGIC, i)
        if i < 0:
            break
        offs_raw.append(i)
        i += 4
    if len(offs_raw) < 2:
        raise ValueError("不是 UBI 镜像：找不到 EC 头")

    peb = offs_raw[1] - offs_raw[0]
    # 只按物理块边界取 EC 头：压缩数据里偶然出现的 "UBI#" 不是块头
    offs = [o for o in range(0, len(data) - peb + 1, peb)
            if data[o:o + 4] == EC_MAGIC]
    # EC 头的 vid_hdr_offset / data_offset 是权威值，不要靠扫描猜：
    # 00000010: 0000 0800 (2048)  0000 1000 (4096)
    ec = data[offs[0]:offs[0] + 64]
    min_io = struct.unpack(">I", ec[0x10:0x14])[0]
    data_off = struct.unpack(">I", ec[0x14:0x18])[0]
    if not (0 < min_io < data_off <= peb):
        raise ValueError(f"EC 头偏移不合理: vid={min_io}, data={data_off}, peb={peb}")
    if data[offs[0] + min_io:offs[0] + min_io + 4] != VID_MAGIC:
        raise ValueError("vid_hdr_offset 处找不到 VID 头")
    leb = peb - data_off

    # 卷表位于两个 layout PEB 的数据区。record 步长在 UBI 版本里可能是
    # 172 或 128 字节，用「name 可打印 + reserved_pebs 合理」自动判定。
    import re as _re
    volumes = []
    for o in offs[:2]:
        v = o + min_io
        if data[v:v + 4] != VID_MAGIC:
            continue
        for stride in (172, 128):
            found = []
            tbl = data[o + data_off:o + data_off + 4096]
            for r in range(0, len(tbl) - stride, stride):
                rec = tbl[r:r + stride]
                name_len = struct.unpack(">H", rec[14:16])[0]
                if not (1 <= name_len <= 127):
                    continue
                name = rec[16:16 + name_len].rstrip(b"\x00").decode("utf-8", "replace")
                if not _re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
                    continue
                reserved = struct.unpack(">I", rec[0:4])[0]
                if not (0 < reserved < 100000):
                    continue
                found.append({
                    "name": name,
                    "vol_id": len(found),
                    "reserved_pebs": reserved,
                    "vol_type": rec[12],
                    "alignment": struct.unpack(">I", rec[4:8])[0],
                })
            if len(found) >= 2:
                volumes = found
                break
        if volumes:
            break
    if not volumes:
        raise ValueError("无法从 layout PEB 解析卷表")

    # 每个用户卷实际占用的 LEB 数
    counts = {}
    for o in offs:
        v = o + min_io
        if data[v:v + 4] != VID_MAGIC:
            continue
        vol_id = struct.unpack(">I", data[v + 8:v + 12])[0]
        if vol_id < 0x7FFFE000:
            counts[vol_id] = counts.get(vol_id, 0) + 1
    for idx, vol in enumerate(volumes):
        vol["image_lebs"] = counts.get(idx, 0)

    return {
        "size_bytes": len(data),
        "peb_bytes": peb,
        "leb_bytes": leb,
        "min_io_bytes": min_io,
        "data_offset": data_off,
        "peb_count": len(offs),
        "layout_pebs": len(offs) - sum(counts.values()),
        "volumes": volumes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--md5", help="期望的 md5，不一致则退出码 1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.image.is_file():
        print(f"文件不存在: {args.image}", file=sys.stderr)
        return 2
    data = args.image.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    try:
        info = parse_ubi(data)
    except ValueError as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 2
    info["md5"] = md5

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"文件      : {args.image}")
        print(f"大小      : {info['size_bytes']} bytes ({info['size_bytes'] / 1048576:.2f} MiB)")
        print(f"md5       : {md5}")
        print(f"PEB / LEB : {info['peb_bytes']} / {info['leb_bytes']} bytes")
        print(f"min I/O   : {info['min_io_bytes']} bytes, data offset {info['data_offset']}")
        print(f"PEB 总数  : {info['peb_count']} (layout {info['layout_pebs']})")
        for v in info["volumes"]:
            print(f"  vol {v['vol_id']}: {v['name']:<12} "
                  f"reserved={v['reserved_pebs']:<4} image_lebs={v['image_lebs']:<4} "
                  f"type={'dynamic' if v['vol_type'] == 1 else 'static'}")
    if args.md5 and md5.lower() != args.md5.lower():
        print(f"md5 不匹配: 期望 {args.md5}, 实际 {md5}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
