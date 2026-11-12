#!/usr/bin/env python3
"""生成 U-Boot 刷写命令；缺任何前置证据就拒绝生成。

安全护栏（刻意做成硬拒绝，不提供 --force）：
  * 设备必须先救砖、恢复原厂固件、补完 loader 区备份（device_state.*）
  * U-Boot 控制台可中断、有 tftp/fatload 与 mtd 写命令、secure_boot 明确为 false
  * 实测 mtdparts、flash 容量、kernel/rootfs 分区偏移与大小
  * 备份已验证（backup_verified: true）、镜像 md5 与 board.yaml 一致
  * 写入长度必须等于镜像真实字节数，且不得超过目标分区

2026-11-11 按现场日志修正（见 docs/INCIDENT.md）：
  - 这台 U-Boot 没有 nand 设备、没有 ubi、没有 saveenv，必须用 `mtd write spi-nand0 ...`
  - `mtd write` 四参数必须齐全；2026-06-29 的变砖正是缺 off/size 导致从 0 写满整片
  - 只刷 A/B 的一个槽，另一个槽原厂完整保留作回滚（刷哪个槽仍需现场定夺）

用法：
    python tools/make_flash_plan.py --check
    python tools/make_flash_plan.py
"""
import argparse
import hashlib
import re
import sys
import zlib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "board" / "s4602" / "board.yaml"
OUT = ROOT / "uboot" / "02-flash.generated.txt"
IMAGE = ROOT / "firmware" / "rootfs.ubi.rv1109"


def _simple_yaml(text: str) -> dict:
    """不依赖 PyYAML 的缩进子集解析（键: 值 / 键: 嵌套字典）。"""
    root: dict = {}
    stack = [(-1, root)]
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*(?:#.*)?$", line.strip())
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip().strip('"').strip("'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            continue
        if raw == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        elif raw in ("null", "~", "None"):
            parent[key] = None
        elif raw.lower() in ("true", "false"):
            parent[key] = raw.lower() == "true"
        elif re.fullmatch(r"-?\d+", raw):
            parent[key] = int(raw)
        else:
            parent[key] = raw
    return root


def load_yaml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return _simple_yaml(text)


def truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.lower() == "true")


def to_int(v, default=0) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 0)
        except ValueError:
            return default
    return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--board-yaml", type=Path, default=YAML)
    ap.add_argument("--image", type=Path, default=IMAGE)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    cfg = load_yaml(args.board_yaml)
    flash = cfg.get("flash", {}) or {}
    ub = cfg.get("uboot", {}) or {}
    plan = cfg.get("flash_plan", {}) or {}
    state = cfg.get("device_state", {}) or {}

    problems = []

    # --- 设备状态：砖机一律拒绝 ---
    if state.get("status") != "operational":
        problems.append(f"设备未恢复 (device_state.status = {state.get('status')!r}，需为 operational)")
    for label, key in [
        ("控制台当前可达 (device_state.console_reachable_now)", "console_reachable_now"),
        ("原厂固件已恢复并验证 (device_state.vendor_restored)", "vendor_restored"),
        ("loader 区 0x0-0x300000 已补备份 (device_state.loader_area_backed_up)", "loader_area_backed_up"),
    ]:
        if not truthy(state.get(key)):
            problems.append(label)

    # --- U-Boot 能力与分区证据 ---
    checks = [
        ("U-Boot 控制台可中断 (uboot.console_interruptible)", ub.get("console_interruptible")),
        ("有镜像加载命令 tftp/fatload (uboot.has_tftp / has_fatload)",
         truthy(ub.get("has_tftp")) or truthy(ub.get("has_fatload"))),
        ("有 mtd 写入命令 (uboot.has_mtd / has_nand)",
         truthy(ub.get("has_mtd")) or truthy(ub.get("has_nand"))),
        ("无签名校验 (uboot.secure_boot == false)", ub.get("secure_boot") is False),
        ("实测 mtdparts (flash.mtdparts)", flash.get("mtdparts")),
        ("flash 容量 (flash.size_mib)", flash.get("size_mib")),
        ("kernel 分区 (flash_plan.kernel_partition)", plan.get("kernel_partition")),
        ("rootfs 分区 (flash_plan.rootfs_partition)", plan.get("rootfs_partition")),
        ("RAM 装载地址 (flash_plan.load_addr)", plan.get("load_addr")),
        ("备份已验证可恢复 (flash_plan.backup_verified)", plan.get("backup_verified")),
        ("写入长度已定义 (flash_plan.rootfs_write_size)", plan.get("rootfs_write_size")),
    ]
    for label, val in checks:
        if isinstance(val, bool):
            ok = val
        elif label.startswith("备份"):
            ok = truthy(val)
        else:
            ok = val not in (None, "", False) and not (isinstance(val, dict) and not val)
        if not ok:
            problems.append(label)

    # --- OpenIPC 引导链 blocker（2026-11-11 新增，见 docs/BOOT_FEASIBILITY.md）---
    # 这三项不是「U-Boot 能不能引导」，而是「刷进去能否起来」：
    # 官方 zboot.img 默认不是 FIT、U-Boot 强制注入 squashfs bootargs、活动槽是 _b。
    blockers = cfg.get("openipc_blockers", {}) or {}
    for label, key in [
        ("自建 FIT 内核已构建并 iminfo 验证 (openipc_blockers.fit_image_built_and_verified)",
         "fit_image_built_and_verified"),
        ("bootargs 策略已实测生效、UBIFS root 能挂 (openipc_blockers.bootargs_strategy_verified)",
         "bootargs_strategy_verified"),
        ("A/B 槽方向已确认并同步 flash_plan 偏移 (openipc_blockers.slot_strategy_confirmed)",
         "slot_strategy_confirmed"),
    ]:
        if not truthy(blockers.get(key)):
            problems.append(label)

    for label, part in [
        ("kernel 分区结构需含 offset/size", plan.get("kernel_partition")),
        ("rootfs 分区结构需含 offset/size", plan.get("rootfs_partition")),
    ]:
        if not isinstance(part, dict) or not all(k in part for k in ("offset", "size")):
            problems.append(label)

    # --- 镜像与写入长度 ---
    md5 = None
    size = 0
    if args.image.is_file():
        data = args.image.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        size = len(data)
    else:
        problems.append(f"镜像不存在: {args.image}")
    expected = (plan.get("rootfs_md5") or "").strip()
    if md5 and expected and md5 != expected:
        problems.append(f"镜像 md5 不符: 期望 {expected}, 实际 {md5}")
    declared = to_int(plan.get("rootfs_write_size"), 0)
    if size and declared != size:
        problems.append(
            f"写入长度必须等于镜像真实字节数: 应为 0x{size:X} ({size}), "
            f"board.yaml 写的是 {plan.get('rootfs_write_size')!r}")
    rootfs_part = plan.get("rootfs_partition") or {}
    if isinstance(rootfs_part, dict) and size:
        part_size = to_int(rootfs_part.get("size"), 0)
        if part_size and size > part_size:
            problems.append(f"镜像 {size} B 超过 rootfs 分区 {part_size} B")

    if problems:
        print("拒绝生成刷写命令，以下前置条件未满足:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print("\n先按 docs/RECOVERY.md 完成救砖、恢复原厂、补备份，再更新 board.yaml。", file=sys.stderr)
        print("注意：设备当前是半砖，任何写 flash 动作都必须先有可写通道。", file=sys.stderr)
        return 1
    if args.check:
        print("前置条件齐备（但尚未执行任何写操作）")
        return 0

    kernel = plan["kernel_partition"]
    rootfs = plan["rootfs_partition"]
    load = plan["load_addr"]
    size_hex = f"0x{size:X}"
    readback = "0x2000000"

    # --- 2026-11-12 实机校准（COM4 / U-Boot 2017.09）---
    # 1) 这张 TF 卡是「整卡 FAT 卷、无分区表」：fatload/fatwrite 必须用 `mmc 1`，
    #    `mmc 1:1` 实测报 Unrecognized filesystem。
    # 2) 本版 U-Boot 裁掉了 iminfo；用 fdt addr/header/print + crc32 与本机逐位比对替代。
    # 3) `? mtd` 实证：<size> 必须是 page(0x800) 倍数（erase 必须 block 0x20000 倍数）。
    #    FIT 文件尾不足一页的部分须在 RAM 里补 0xFF 后按对齐长度写入。
    # 4) `cmp.b` 实测报 Usage；改用 crc32 源/回读双向比对（备份阶段已验证可靠）。
    zboot = ROOT / "firmware" / "zboot.img.s4602"
    zboot_size = zboot.stat().st_size if zboot.is_file() else 0
    zboot_crc = (f"{zlib.crc32(zboot.read_bytes()) & 0xffffffff:08x}"
                 if zboot_size else "<CRC>")
    page = 0x800
    zboot_write = ((zboot_size + page - 1) // page) * page if zboot_size else 0
    zboot_write_hex = f"0x{zboot_write:X}" if zboot_write else "<ALIGNED_SIZE>"
    zboot_pad = zboot_write - zboot_size
    rootfs_crc = f"{zlib.crc32(args.image.read_bytes()) & 0xffffffff:08x}"
    lines = [
        "# 由 tools/make_flash_plan.py 生成 —— " + datetime.now().isoformat(timespec="seconds"),
        "# 逐条执行，任何一条报错立即停手。禁止整段粘贴（曾导致命令被截断而变砖）。",
        "# 前置：docs/RECOVERY.md 阶段 2 已完成，原厂可启动，loader 区已补备份。",
        "# 本设备 U-Boot 实测：无 nand 设备 / 无 ubi / 无 saveenv，只能用 mtd 命令。",
        "# 红线：mtd write 四参数齐全，size 必须等于文件实际字节数，绝不写整个分区。",
        "",
        f"# 镜像: {args.image.name} ({size} bytes = {size_hex}, md5 {md5})",
        "# --- 0. 槽策略（2026-11-12 现场定夺）---",
        "# SPL 横幅打印 _b / env 无 android_slotsufix / misc 全 0xFF → 活动槽写死在厂商 SPL，",
        "# 而红线是不碰 uboot/loader，所以设备永远只认 _b。故刷 _b，_a 原厂完整留作回滚。",
        f"#   boot_b   = {kernel['offset']} (size {kernel['size']})",
        f"#   system_b = {rootfs['offset']} (size {rootfs['size']}) / ubi.mtd=9",
        "",
        "# --- 1. 【非破坏】RAM 里验证 FIT，确认无误前不碰 flash ---",
        "# 本版 U-Boot 无 iminfo；改用 fdt + crc32（比 iminfo 更强，与本机逐位比对）。",
        f"fatload mmc 1 {load} zboot.img.s4602",
        f"crc32 {load} 0x{zboot_size:X}          # 必须 = {zboot_crc}",
        f"fdt addr {load} ; fdt header ; fdt print /",
        "#  header: magic 0xd00dfeed, version 17；print: images/kernel@1 + fdt@1 + conf@1",
        "# 可选（会离开 U-Boot，需重新断电复位）：boot_fit " + load,
        "#   → 验证 boot_fit 是否接受无签名 FIT、内核能否起来、Kernel command line 是否为 UBIFS",
        "",
        "# --- 2. 写 rootfs 到 system_b，回读 CRC 比对 ---",
        f"fatload mmc 1 {load} rootfs.ubi.rv1109",
        f"crc32 {load} {size_hex}       # 必须 = {rootfs_crc}",
        f"mtd erase spi-nand0 {rootfs['offset']} {rootfs['size']}",
        f"mtd write spi-nand0 {load} {rootfs['offset']} {size_hex}",
        f"mtd read  spi-nand0 {readback} {rootfs['offset']} {size_hex}",
        f"crc32 {readback} {size_hex}   # 必须 = {rootfs_crc}（本版 cmp.b 报 Usage，用 crc32）",
        "",
        "# --- 3. 写内核 FIT 到 boot_b（size 必须 page 0x800 对齐）---",
        f"# FIT 实际 {zboot_size} B = 0x{zboot_size:X}；向上对齐到 {zboot_write_hex}（补 {zboot_pad} B 0xFF）",
        f"mw.b {load} 0xFF {zboot_write_hex}   # 先把目标缓冲区整体填 0xFF（尾部即对齐填充）",
        f"fatload mmc 1 {load} zboot.img.s4602  # 再加载，只覆盖有效数据，尾部保持 0xFF",
        f"crc32 {load} 0x{zboot_size:X}          # 必须 = {zboot_crc}",
        f"mtd erase spi-nand0 {kernel['offset']} {kernel['size']}",
        f"mtd write spi-nand0 {load} {kernel['offset']} {zboot_write_hex}",
        f"mtd read  spi-nand0 {readback} {kernel['offset']} {zboot_write_hex}",
        f"crc32 {readback} 0x{zboot_size:X}      # 必须 = {zboot_crc}",
        "",
        "# --- 4. bootargs：不要 setenv，也不要写 DTB chosen ---",
        "# 现场 7 次启动实证：U-Boot 会强制注入 root=/dev/ubiblock0_0 rootfstype=squashfs。",
        "# 唯一可靠做法：内核 CONFIG_CMDLINE_FORCE=y + 内置完整 cmdline（含 mtdparts）。",
        "# 启动后核对 'Kernel command line' 是 ubi.mtd=9 + root=ubi0:rootfs + ubifs。",
        "",
        "# --- 5. 分级验收 ---",
        "#  L1 boot_fit 认 FIT / Starting kernel / OpenIPC 内核版本",
        "#  L2 Kernel command line 正确 / ubi0 attached / UBIFS mounted / 可登录",
        "#  L3 majestic 出流 —— SC5338 驱动已编入 CI 内核，但 rkaiq IQ/3A XML 未取，",
        "#     实板出流未验证；达不到别误判为刷写失败",
        "",
        "# --- 6. 回滚 ---",
        "# SD 卡上保留 full-00..07.bin（本次全片备份，逐块 CRC 已验）。",
        "# 进 U-Boot 后 fatload + mtd write 反向写回 boot_b/system_b 即恢复原厂。",
        "# 失败则断电重启回未刷的 _a 槽，不要反复试刷",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 {args.out}（长度/CRC 均已按 firmware/ 实际镜像计算，逐条执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
