#!/usr/bin/env python3
"""从本地证据里生成 SC5338 资料包（只读，不连设备、不联网）。

用法：
    python tools/collect_sc5338.py            # 生成全部证据文件
    python tools/collect_sc5338.py --check   # 只做自检，不写文件

产物（全部落在 docs/evidence/）：
    vendor-kernel.dts             原厂 DTB 完整反编译（不依赖 dtc）
    sc5338-camera-topology.dts    sensor / ISP / DPHY / CIF / regulator 节点摘录
    sc5338-dmesg.txt              两份现场日志里 sensor 与视频链路相关行
    vendor-vmlinux.bin            原厂 zImage 的 XZ payload 解压结果（若尚不存在）
    sc5338-evidence.json          机器可读汇总（DTB 属性、关键事件、参考驱动哈希）

约定：DTB 由 tools/inspect_vendor_backup.py 从 boot_a.img FIT @0x800 提取。
本脚本只解析扁平设备树（FDT v17），不做任何写入设备或改动备份的操作。
"""
import argparse
import hashlib
import json
import lzma
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "docs" / "evidence"
DTB = EV / "vendor-kernel.dtb"
ZIMAGE = EV / "vendor-zimage.bin"
VMLINUX = EV / "vendor-vmlinux.bin"
XZ_PAYLOAD_OFF = 0x3DF0  # 实测：ARM zImage 的 XZ piggy 数据起点
LOGS = [
    ROOT / "qianliyan原厂固件启动信息.log",
    ROOT / "s4602变砖过程_20260629_133651.txt",
]
REF_DIR = ROOT / "src" / "sensor-reference"

# 节点/属性里出现这些关键字即认为与摄像头链路有关
CAMERA_KEYS = (
    "sc5338", "sc530ai", "sc401ai", "m01_f_", "csi-dphy", "mipi-dphy",
    "rkisp", "rkcif", "cif", "camera", "sensor", "vcm", "vm149c", "dw9714",
    "ircut", "flash-led", "csi2", "isp", "phy-rockchip", "rockchip,camera",
)
# 这些属性里的 cell 可能是 phandle，输出时补注释（不擅自改写数值）
REF_PROPS = {
    "clocks", "resets", "phys", "phy-handle", "power-domains",
    "interrupts-extended", "assigned-clocks", "assigned-clock-parents",
    "remote-endpoint", "dmas", "iommus", "pinctrl-0", "pinctrl-1",
}


class Node:
    __slots__ = ("name", "props", "children", "path")

    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.props: list[tuple[str, bytes]] = []
        self.children: list["Node"] = []


def parse_fdt(data: bytes) -> Node:
    if data[:4] != b"\xd0\x0d\xfe\xed":
        raise ValueError("不是 FDT/DTB（magic 不是 d00dfeed）")
    h = struct.unpack(">10I", data[:40])
    totalsize, off_struct, off_strings, size_strings, size_struct = (
        h[1], h[2], h[3], h[8], h[9])
    if totalsize > len(data):
        raise ValueError(f"DTB 头声明 {totalsize} B，实际只有 {len(data)} B")
    strtab = data[off_strings:off_strings + size_strings]

    def gstr(off: int) -> str:
        end = strtab.find(b"\x00", off)
        return strtab[off:end].decode("utf-8", "replace")

    root = None
    stack: list[Node] = []
    p, limit = off_struct, min(len(data), off_struct + size_struct)
    while p + 4 <= limit:
        tag = struct.unpack(">I", data[p:p + 4])[0]
        if tag == 1:  # FDT_BEGIN_NODE
            end = data.find(b"\x00", p + 4)
            name = data[p + 4:end].decode("utf-8", "replace")
            p = (end + 4) & ~3
            if not stack:
                node = Node("", "/")
                root = node
            else:
                parent = stack[-1]
                node = Node(name, (parent.path.rstrip("/") + "/" + name) or "/")
                parent.children.append(node)
            stack.append(node)
        elif tag == 2:  # FDT_END_NODE
            p += 4
            if stack:
                stack.pop()
        elif tag == 3:  # FDT_PROP
            size, nameoff = struct.unpack(">II", data[p + 4:p + 12])
            p += 12
            val = data[p:p + size]
            p = (p + size + 3) & ~3
            if stack:
                stack[-1].props.append((gstr(nameoff), val))
        elif tag == 4:  # FDT_NOP
            p += 4
        elif tag == 9:  # FDT_END
            break
        else:
            raise ValueError(f"未知 FDT tag {tag} @0x{p:x}")
    if root is None:
        raise ValueError("DTB 里没有根节点")
    return root


def walk(node: Node):
    yield node
    for c in node.children:
        yield from walk(c)


def phandle_map(root: Node) -> dict[int, str]:
    out = {}
    for n in walk(root):
        for k, v in n.props:
            if k in ("phandle", "linux,phandle") and len(v) == 4:
                out[struct.unpack(">I", v)[0]] = n.path
    return out


def printable_strings(v: bytes):
    if not v or v[-1] != 0:
        return None
    parts = v.split(b"\x00")[:-1]
    if not parts:
        return None
    for s in parts:
        if any(c < 0x20 or c > 0x7e for c in s):
            return None
    return [s.decode("ascii") for s in parts]


def fmt_prop(name: str, v: bytes, ph: dict[int, str]) -> str:
    if not v:
        return name + ";"
    strs = printable_strings(v)
    if strs is not None:
        return f'{name} = {", ".join(chr(34) + s + chr(34) for s in strs)};'
    if len(v) % 4 == 0:
        cells = [struct.unpack(">I", v[i:i + 4])[0] for i in range(0, len(v), 4)]
        # 单 cell 且是引用型属性：还原成绝对路径引用，便于阅读
        if len(cells) == 1 and cells[0] in ph and (
                name.endswith("-supply") or name in REF_PROPS):
            return f"{name} = <&{{{ph[cells[0]]}}}>;"
        body = "<" + " ".join(f"0x{c:x}" for c in cells) + ">"
        refs = [f"0x{c:x}->&{{{ph[c]}}}" for c in cells if c in ph]
        suffix = "  /* " + ", ".join(refs) + " */" if refs and (
                name in REF_PROPS or name.endswith("-supply")) else ""
        return f"{name} = {body};{suffix}"
    return name + " = [" + " ".join(f"{b:02x}" for b in v) + "];"


def render_tree(node: Node, ph: dict[int, str], indent: int = 0, top: bool = False) -> str:
    pad = "\t" * indent
    name = "/" if top else (node.name or "/")
    lines = [f"{pad}{name} {{"]
    for k, v in node.props:
        lines.append("\t" * (indent + 1) + fmt_prop(k, v, ph))
    for c in node.children:
        lines.append(render_tree(c, ph, indent + 1))
    lines.append(pad + "};")
    return "\n".join(lines)


def node_matches(n: Node) -> bool:
    blob = n.path.lower()
    for k, v in n.props:
        blob += " " + k.lower()
        s = printable_strings(v)
        if s:
            blob += " " + " ".join(s).lower()
    return any(key in blob for key in CAMERA_KEYS)


def decode_props(n: Node) -> dict:
    out = {}
    for k, v in n.props:
        s = printable_strings(v)
        if s is not None:
            out[k] = s if len(s) > 1 else s[0]
        elif len(v) % 4 == 0 and v:
            out[k] = [struct.unpack(">I", v[i:i + 4])[0]
                      for i in range(0, len(v), 4)]
        else:
            out[k] = "hex:" + v.hex()
    return out


def collect_logs() -> tuple[str, dict]:
    sensor_re = re.compile(
        r"sc5338|sc530ai|sc401ai|m01_f_sc5338|Detected SC|set format|start stream",
        re.I)
    link_re = re.compile(
        r"mipi-dphy|rockchip-mipi|csi-dphy|rkisp|rkcif|rkcif|isp[0-9]|"
        r"csi2|sensor id|v4l2|media|stream on|stream off|video\d|/dev/video",
        re.I)
    sensor_lines, link_lines = [], []
    for p in LOGS:
        if not p.is_file():
            print(f"警告：日志不存在 {p}", file=sys.stderr)
            continue
        for i, line in enumerate(p.read_text("utf-8", "replace").splitlines(), 1):
            if sensor_re.search(line):
                sensor_lines.append((p.name, i, line.rstrip()))
            elif link_re.search(line):
                link_lines.append((p.name, i, line.rstrip()))

    def uniq(rows):
        seen, out = {}, []
        for f, i, line in rows:
            norm = re.sub(r"^\[\s*[\d.]+\]\s*", "", line).strip()
            e = seen.setdefault(norm, {"event": norm, "count": 0, "first": f"{f}:{i}"})
            e["count"] += 1
            if e["count"] == 1:
                out.append(e)
        return out

    out = ["# SC5338 / 视频链路 dmesg 证据（由 tools/collect_sc5338.py 生成）", ""]
    out.append("## 来源")
    for p in LOGS:
        out.append(f"#   {p.name} ({p.stat().st_size if p.is_file() else 0} B)")
    out.append("")
    out.append(f"## A. sensor 相关唯一事件（{len(uniq(sensor_lines))} 条，去时间戳，含出现次数）")
    for e in uniq(sensor_lines):
        out.append(f"[{e['count']:>3}x] {e['first']}  {e['event']}")
    out.append("")
    out.append(f"## B. 视频链路 / ISP 唯一事件（{len(uniq(link_lines))} 条）")
    for e in uniq(link_lines):
        out.append(f"[{e['count']:>3}x] {e['first']}  {e['event']}")
    out.append("")
    out.append("## C. sensor 相关原始行（全部，保留时间戳与来源行号）")
    for f, i, line in sensor_lines:
        out.append(f"{f}:{i}: {line}")
    return "\n".join(out) + "\n", {
        "sensor_unique_events": uniq(sensor_lines),
        "link_unique_events": uniq(link_lines),
        "sensor_raw_lines": len(sensor_lines),
        "link_raw_lines": len(link_lines),
    }


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ensure_vmlinux(write: bool = True) -> bytes:
    """从原厂 zImage 的 XZ payload 解压 vmlinux；已存在则直接读。

    zImage 头 @0x24 magic 0x016f2818、end=0x318cc8；XZ 流实测在 0x3df0，
    解压得 11,853,824 B（含内建 sc5338/sc530ai/sc401ai 驱动字符串与寄存器表）。
    """
    if VMLINUX.is_file():
        return VMLINUX.read_bytes()
    if not ZIMAGE.is_file():
        raise FileNotFoundError(f"缺少 {ZIMAGE}，先跑 inspect_vendor_backup.py --extract")
    blob = ZIMAGE.read_bytes()
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    out = dec.decompress(blob[XZ_PAYLOAD_OFF:])
    if write:
        VMLINUX.write_bytes(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只自检，不写文件")
    args = ap.parse_args()

    if not DTB.is_file():
        print(f"缺少 {DTB}", file=sys.stderr)
        return 1
    data = DTB.read_bytes()
    vm = ensure_vmlinux(write=not args.check)
    root = parse_fdt(data)
    ph = phandle_map(root)
    nodes = list(walk(root))
    cam = [n for n in nodes if node_matches(n)]

    dts = "/dts-v1/;\n\n/* 由 tools/collect_sc5338.py 从 docs/evidence/vendor-kernel.dtb 反编译。\n" \
          "   phandle 数值保持原样；引用型属性里出现的 phandle 在行尾注释或 &{/path} 中标出。\n" \
          "   这不是 dtc 输出，不能直接编译，仅作 S4602 板级 DTS 的取证/改写参考。 */\n\n" \
          + render_tree(root, ph, top=True) + "\n"

    top_lines = [
        "/* SC5338 摄像头链路节点摘录 —— 来源 docs/evidence/vendor-kernel.dtb",
        f"   DTB {len(data)} B / md5 {hashlib.md5(data).hexdigest()}",
        f"   共 {len(nodes)} 个节点，命中 {len(cam)} 个摄像头相关节点。",
        "   每个片段前的注释是节点在 DTB 里的完整路径，片段本身可直接抄进目标 DTS。 */",
        "",
    ]
    for n in cam:
        top_lines.append(f"/* {n.path} */")
        top_lines.append(render_tree(n, ph, top=False))
        top_lines.append("")

    dmesg, log_stat = collect_logs()

    sensor_nodes = [n for n in nodes if "sc5338" in n.path.lower()
                    or any("sc5338" in " ".join(printable_strings(v) or []).lower()
                           for _, v in n.props)]
    refs = []
    if REF_DIR.is_dir():
        for p in sorted(REF_DIR.iterdir()):
            if p.is_file():
                refs.append({"file": p.relative_to(ROOT).as_posix(),
                             "bytes": p.stat().st_size, "sha256": sha256(p)})

    summary = {
        "generated_by": "tools/collect_sc5338.py",
        "dtb": {"file": DTB.relative_to(ROOT).as_posix(), "bytes": len(data),
                "md5": hashlib.md5(data).hexdigest(),
                "version": struct.unpack(">I", data[20:24])[0],
                "nodes_total": len(nodes), "phandles": len(ph),
                "camera_nodes": len(cam)},
        "sensor_nodes": [{"path": n.path, "props": decode_props(n)} for n in sensor_nodes],
        "dmesg": log_stat,
        "vmlinux": {
            "file": VMLINUX.relative_to(ROOT).as_posix(),
            "bytes": len(vm), "sha256": hashlib.sha256(vm).hexdigest(),
            "xz_payload_offset": hex(XZ_PAYLOAD_OFF),
            "sc5338_string_hits": vm.count(b"sc5338"),
            "sc530ai_string_hits": vm.count(b"sc530ai"),
            "sc401ai_string_hits": vm.count(b"sc401ai"),
            "compatible_string": vm.count(b"smartsens,sc5338"),
            "kernel_4_19_string": vm.count(b"Linux version 4.19"),
        },
        "reference_drivers": refs,
        "notes": [
            "chip id 由日志 Detected SC00ce50 sensor 给出：0x00ce50（16-bit 0xce50）",
            "原厂驱动版本 00.01.01；lane_num(2)；set format / start stream 2880x1620 hdr:5",
            "hdr:5 == HDR_X2（include/uapi/linux/rk-camera-module.h 枚举）",
            "模块名 m01_f_sc5338（rockchip-mipi-dphy-rx 匹配日志）",
            "模组 CMK-OT1607-FV1 / 镜头 M12-40IRC-4MP-F16（vendor-kernel.dtb）",
            "原厂内核内建 sc5338/sc530ai/sc401ai 三套驱动，共址 i2c1@0x30 靠 chip id 甄别",
            "寄存器表交叉验证见 docs/evidence/sc5338-regs-crosscheck.txt",
        ],
    }

    if args.check:
        print(f"[ok] FDT v{summary['dtb']['version']} nodes={len(nodes)} "
              f"phandles={len(ph)} camera={len(cam)} sensor_nodes={len(sensor_nodes)}")
        print(f"[ok] dmesg 唯一事件 {len(log_stat['sensor_unique_events'])} + "
              f"{len(log_stat['link_unique_events'])}, 原始行 "
              f"{log_stat['sensor_raw_lines']} + {log_stat['link_raw_lines']}")
        print(f"[ok] 参考驱动 {len(refs)} 个")
        assert sensor_nodes, "DTB 里没有找到 sc5338 节点"
        assert any("Detected SC00ce50" in e["event"]
                   for e in log_stat["sensor_unique_events"]), "日志里没有 chip id 证据"
        print("自检通过")
        return 0

    EV.mkdir(parents=True, exist_ok=True)
    (EV / "vendor-kernel.dts").write_text(dts, "utf-8")
    (EV / "sc5338-camera-topology.dts").write_text("\n".join(top_lines), "utf-8")
    (EV / "sc5338-dmesg.txt").write_text(dmesg, "utf-8")
    (EV / "sc5338-evidence.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf-8")
    for name in ("vendor-kernel.dts", "sc5338-camera-topology.dts",
                 "sc5338-dmesg.txt", "sc5338-evidence.json"):
        p = EV / name
        print(f"写出 {p.relative_to(ROOT).as_posix()}  {p.stat().st_size} B")
    print(f"节点 {len(nodes)} / phandle {len(ph)} / 摄像头节点 {len(cam)} / "
          f"sensor 节点 {len(sensor_nodes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
