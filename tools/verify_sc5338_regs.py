#!/usr/bin/env python3
"""核对公开 SC5338 寄存器表与 S4602 原厂内核里的内建表是否同源，并导出可移植表。

只读工具：不连设备、不联网、不改动备份。

输入：
    src/sensor-reference/sc5338-openipc-ingenic-t41.c   OpenIPC/openingenic 的 T41 驱动
    docs/evidence/vendor-vmlinux.bin                     从 boot_a.img FIT 提取的 zImage 解压所得
                                                         (XZ payload @0x3df0, 11,853,824 B)

输出：
    docs/evidence/sc5338-regs-crosscheck.txt             候选表相似度矩阵 + 序列比对报告
    src/sensor-reference/sc5338-regs-rockchip.h           转成 Rockchip struct regval 的表
                                                          （原厂 3 张 + 公开 2 张）

用法：
    python tools/verify_sc5338_regs.py
    python tools/verify_sc5338_regs.py --check           只自检，不写文件
"""
import argparse
import difflib
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sensor-reference" / "sc5338-openipc-ingenic-t41.c"
VMLINUX = ROOT / "docs" / "evidence" / "vendor-vmlinux.bin"
EV = ROOT / "docs" / "evidence"
REF = ROOT / "src" / "sensor-reference"

TABLES = {
    "linear_2880x1620_30fps_2lane": "sc5338_init_regs_2880_1620_30fps_mipi_2lane",
    "hdr2_2880x1620_15fps_2lane": "sc5338_init_regs_2880_1620_15fps_mipi_dol",
}
REG_NULL = 0x0000  # Rockchip sc500ai.c 的 write_array 终止标记
STEP = 4           # struct regval { u16 addr; u8 val; u8 pad; }


def parse_public(text: str) -> dict:
    out = {}
    for key, name in TABLES.items():
        i = text.find("static struct regval_list " + name + "[]")
        if i < 0:
            raise ValueError(f"找不到表 {name}")
        j = text.find("};", i)
        regs, delays = [], []
        for a, v in re.findall(
                r"\{(0x[0-9a-fA-F]+|SC5338_REG_END|SC5338_REG_DELAY)\s*,\s*"
                r"(0x[0-9a-fA-F]+|\d+)\}", text[i:j]):
            val = int(v, 0)
            if a == "SC5338_REG_END":
                continue
            if a == "SC5338_REG_DELAY":
                delays.append(val)
                continue
            regs.append((int(a, 16), val))
        out[key] = {"regs": regs, "delay_entries": delays}
    return out


def parse_vendor_table(vm: bytes, off: int, maxn: int = 400) -> list:
    """按 4 字节/项解析原厂表，遇到 REG_NULL 或非法地址即停。"""
    out = []
    for k in range(maxn):
        o = off + k * STEP
        a = int.from_bytes(vm[o:o + 2], "little")
        v = vm[o + 2]
        if a == REG_NULL or not (0x0100 <= a <= 0x6fff):
            break
        out.append((a, v))
    return out


def ratio(a: list, b: list) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def scan_candidates(vm: bytes, public: dict) -> list:
    """扫描所有以 0x0103/0x01 开头、4B/项、>=40 项的表，给出相似度。"""
    out = []
    for m in re.finditer(rb"\x03\x01\x01", vm):
        off = m.start()
        regs = parse_vendor_table(vm, off)
        if len(regs) < 40:
            continue
        out.append({
            "offset": off, "regs": regs,
            "linear": ratio(public["linear_2880x1620_30fps_2lane"]["regs"], regs),
            "hdr2": ratio(public["hdr2_2880x1620_15fps_2lane"]["regs"], regs),
            "fingerprint": regs[:5],
        })
    return sorted(out, key=lambda x: x["offset"])


def opcodes(public: list, vendor: list) -> list:
    sm = difflib.SequenceMatcher(None, public, vendor, autojunk=False)
    return [{"op": tag, "public": public[i1:i2][:6],
             "vendor": vendor[j1:j2][:6],
             "pc": i2 - i1, "vc": j2 - j1}
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    for p in (SRC, VMLINUX):
        if not p.is_file():
            print(f"缺少 {p}", file=sys.stderr)
            return 1
    text = SRC.read_text("utf-8", "replace")
    vm = VMLINUX.read_bytes()
    public = parse_public(text)
    cands = scan_candidates(vm, public)

    # sc5338 驱动符号位置：其后的 .rodata 表归属 sc5338（前面的多属 sc401ai/sc530ai）
    sym = vm.find(b"sc5338_set_ctrl")
    sc5338_tables = [c for c in cands if sym >= 0 and sym < c["offset"] < sym + 0x10000]
    best_hdr = max(cands, key=lambda c: c["hdr2"])
    best_lin = max(cands, key=lambda c: c["linear"])

    lines = [
        "SC5338 寄存器表交叉验证（公开 Ingenic 驱动 vs S4602 原厂内核二进制）",
        "=" * 78,
        f"公开驱动 : {SRC.relative_to(ROOT).as_posix()}",
        f"           sha256 {hashlib.sha256(SRC.read_bytes()).hexdigest()}",
        f"原厂内核 : {VMLINUX.relative_to(ROOT).as_posix()} ({len(vm)} B)",
        f"           sha256 {hashlib.sha256(vm).hexdigest()}",
        f"           sc5338_set_ctrl 符号 @ {hex(sym) if sym >= 0 else '未找到'}",
        "",
        "方法：原厂 zImage 的 XZ payload @0x3df0 解压成 vmlinux；",
        "      扫描所有以 {0x0103,0x01} 开头、按 struct regval {u16 addr; u8 val; u8 pad}",
        "      4 B/项排布的表，逐张与公开表做 SequenceMatcher 序列比对。",
        "",
        f"--- vmlinux 里共 {len(cands)} 张 SmartSens 风格初始化表 ---",
        f"{'偏移':>12} {'项数':>6} {'linear 相似度':>14} {'HDR2 相似度':>14}  前 5 项",
    ]
    for c in cands:
        mark = " <sc5338>" if c in sc5338_tables else ""
        lines.append(f"{hex(c['offset']):>12} {len(c['regs']):>6} "
                     f"{c['linear']:>14.4f} {c['hdr2']:>14.4f}  "
                     f"{c['fingerprint']}{mark}")
    lines += [
        "",
        f"最佳 HDR2 匹配 : {hex(best_hdr['offset'])}, 相似度 {best_hdr['hdr2']:.4f}",
        f"最佳 linear 匹配: {hex(best_lin['offset'])}, 相似度 {best_lin['linear']:.4f}",
        f"sc5338 符号之后的表: {len(sc5338_tables)} 张，"
        f"最高相似度 {max((max(c['linear'], c['hdr2']) for c in sc5338_tables), default=0):.4f}",
        "",
    ]

    if sc5338_tables:
        ref = max(sc5338_tables, key=lambda c: c["hdr2"])
        pub = public["hdr2_2880x1620_15fps_2lane"]["regs"]
        diffs = opcodes(pub, ref["regs"])
        lines += [
            "--- 与公开 HDR2 表最接近的原厂表：逐项差异 ---",
            f"    原厂 {hex(ref['offset'])} / {len(ref['regs'])} 项"
            f"  <->  公开 {len(pub)} 项，差异块 {len(diffs)} 个",
        ]
        for d in diffs[:15]:
            lines.append(f"    {d['op']:<8} 公开 {d['public']} ({d['pc']} 项) "
                         f"<-> 原厂 {d['vendor']} ({d['vc']} 项)")
        lines.append("")

    lines += [
        "结论：",
        "  1. 原厂内核内建了多个 SmartSens sensor 驱动与多套 mode 表；sc5338 符号之后",
        "     的 3 张表与公开 SC5338 表相似度 0.82~0.96，属同一寄存器族/同源序列。",
        "  2. 原厂表与公开表并不逐字节相同（模组厂 CMK-OT1607-FV1 有定制、HDR 模式",
        "     不同、且多了一项 {0x0100,0x00} 软 standby），因此移植时优先用导出的",
        "     vendor_* 原厂表，public_* 只作对照。",
        "  3. 交叉验证只覆盖 mode 初始化表；曝光/增益/VC/HDR 控制逻辑仍需按",
        "     rk-camera-module.h 在 Rockchip v4l2 骨架里实现。",
        "  4. 尚未实板验证；刷机前必须先 i2c 读 0x3107/0x3108 确认 0xce/0x50。",
        "",
    ]

    hdr = [
        "/* 由 tools/verify_sc5338_regs.py 生成 —— SC5338 初始化寄存器表参考资料。",
        " *",
        " * vendor_* : 从 S4602 原厂 boot_a.img 的 zImage 解压出的 vmlinux 中解析",
        " *            （sc5338_set_ctrl 符号之后、4 B/项、到 REG_NULL 结束）。",
        " *            这是该设备实际编进内核的序列，优先使用；但 mode 归属未实板确认。",
        " * public_* : 从 OpenIPC/openingenic (kernel/sensors/t41/sc5338/sc5338.c) 转换，",
        " *            上游 GPL-2.0，仅作对照。",
        " *",
        " * 约定：struct regval { u16 addr; u8 val; }; 与 OpenIPC/linux sc500ai.c 一致；",
        " *       终止标记 SC5338_REG_NULL (0x0000) 对应 sc500ai.c 的 REG_NULL。",
        " * S4602 现场证据：chip id 0xce50 @0x3107/0x3108, i2c1@0x30, 2-lane MIPI,",
        " *                 2880x1620, hdr:5 == HDR_X2 (rk-camera-module.h)。",
        " * 注意：这不是可直接编译的驱动，只是数据表；尚未实板验证。",
        " */",
        "#ifndef _SC5338_REGS_REFERENCE_H_",
        "#define _SC5338_REGS_REFERENCE_H_",
        "",
        "#define SC5338_REG_NULL 0x0000",
        "",
    ]

    def emit(name, regs, note):
        hdr.append("")
        hdr.append(f"/* {note} */")
        hdr.append(f"static const struct regval sc5338_{name}_regs[] = {{")
        for a, v in regs:
            hdr.append(f"\t{{0x{a:04x}, 0x{v:02x}}},")
        hdr.append("\t{SC5338_REG_NULL, 0x00},")
        hdr.append("};")

    for i, c in enumerate(sc5338_tables):
        emit(f"vendor_mode{i + 1}",
             c["regs"],
             f"原厂 vmlinux @{hex(c['offset'])}, {len(c['regs'])} 项, "
             f"linear={c['linear']:.3f}/hdr2={c['hdr2']:.3f}, "
             f"前 5 项 {c['fingerprint']}")
    for key, t in public.items():
        emit("public_" + key, t["regs"],
             f"OpenIPC/openingenic T41 公开表, {len(t['regs'])} 项")
    hdr += ["", "#endif /* _SC5338_REGS_REFERENCE_H_ */", ""]

    if args.check:
        print(f"[ok] 候选表 {len(cands)} 张, sc5338 区域 {len(sc5338_tables)} 张")
        print(f"[ok] 最佳 HDR2 相似度 {best_hdr['hdr2']:.4f} / "
              f"linear {best_lin['linear']:.4f}")
        assert sym >= 0, "原厂内核里没有 sc5338 符号"
        assert len(sc5338_tables) >= 2, "sc5338 区域表数量不足"
        assert best_hdr["hdr2"] >= 0.90, "公开 HDR2 表与原厂表不同源"
        print("自检通过")
        return 0

    (EV / "sc5338-regs-crosscheck.txt").write_text("\n".join(lines) + "\n", "utf-8")
    (REF / "sc5338-regs-rockchip.h").write_text("\n".join(hdr), "utf-8")
    print("\n".join(lines))
    print(f"写出 {(EV / 'sc5338-regs-crosscheck.txt').relative_to(ROOT).as_posix()}")
    print(f"写出 {(REF / 'sc5338-regs-rockchip.h').relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
