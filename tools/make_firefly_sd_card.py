#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4602 Firefly 固件 SD 卡交付包打包器。

把 Firefly SDK 编译出的内核 + rootfs 镜像组装成一张 FAT32 SD 卡可直接
使用的根目录内容。产物只做静态校验，**不碰设备、不写任何 NAND**。

输入  out/firefly_firmware/   （boot_b.img / system_b.img /
                              s4602-firefly_uImage.bin / vendor-kernel.dtb）
输出  out/firefly_sd_card/    （整目录拷入 FAT32 卡根即可）
        ├── system_b.img                <- mtd9  (0x03A80000, 0x2C00000)
        ├── boot_b.img                  <- mtd6  (0x00A00000, 0x400000)
        ├── s4602-firefly_uImage.bin    <- SD 三参 bootm 内核（不写 NAND）
        ├── vendor-kernel.dtb           <- SD 三参 bootm DTB（不写 NAND）
        ├── MANIFEST.sha256             <- 主机侧完整性
        ├── MANIFEST.crc32              <- U-Boot crc32 回读比对用
        ├── MANIFEST.sizes              <- fatload 尺寸门控
        └── README.txt                  <- 上机操作卡（U-Boot 命令逐条）

用法: python tools/make_firefly_sd_card.py            # 打包并自校验
      python tools/make_firefly_sd_card.py --verify   # 仅校验已打包输出
"""
import hashlib
import shutil
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "out" / "firefly_firmware"
OUT = ROOT / "out" / "firefly_sd_card"

# name, expected_size, magic@0 (big endian bytes or None), note
# 2026-11-20: DRM restored in kernel -> boot_b FIT and uImage grow. Those two use
# range gates (exact=0); system_b geometry and DTB stay exact. See check().
FILES = [
    ("boot_b.img",               0, b"\xd0\x0d\xfe\xed", "boot_b  FIT 内核"),
    ("system_b.img",            46137344, None,                "system_b squashfs-in-UBI rootfs"),
    ("s4602-firefly_uImage.bin", 0, b"\x27\x05\x19\x56", "SD 三参 bootm 内核(raw+uImage 头)"),
    ("vendor-kernel.dtb",          85937, b"\xd0\x0d\xfe\xed", "SD 三参 bootm DTB (FDT)"),
]
BOOT_B_MAX = 0x400000      # mtd6 boot_b = 4 MiB hard ceiling (CI also gates this)
MANAGED = {n for n, _, _, _ in FILES} | {
    "MANIFEST.sha256", "MANIFEST.crc32", "MANIFEST.sizes", "README.txt"}


def digest(b):
    return (hashlib.sha256(b).hexdigest(), zlib.crc32(b) & 0xFFFFFFFF)


def check(src: Path, errors):
    """校验单个镜像的硬门：尺寸 + magic + system_b 尾部 0xFF。"""
    name = src.name
    spec = next((s for s in FILES if s[0] == name), None)
    if spec is None:
        return
    _, size, magic, _ = spec
    data = src.read_bytes()
    if size and len(data) != size:
        errors.append(f"{name}: size {len(data)} != {size}")
    if magic and data[:4] != magic:
        errors.append(f"{name}: magic {data[:4].hex()} != {magic.hex()}")
    # DRM-grown images: only sane range, exact size tracked in manifest instead.
    if name == "boot_b.img" and len(data) > BOOT_B_MAX:
        errors.append(f"{name}: size {len(data)} > boot_b 4MiB ({BOOT_B_MAX})")
    if name == "boot_b.img" and len(data) < 0x100000:
        errors.append(f"{name}: size {len(data)} implausibly small")
    if name == "s4602-firefly_uImage.bin" and len(data) < 0x100000:
        errors.append(f"{name}: size {len(data)} implausibly small")
    if name == "system_b.img" and not all(b == 0xFF for b in data[-4096:]):
        errors.append(f"{name}: tail 4096B not all 0xFF")


def readme_text(rows):
    """生成 SD 卡根的上机操作卡；rows = [(name, size, sha256, crc, note)]。"""
    table = "\n".join(
        f"  {n:<26} {s:>10} B  crc32=0x{c:08x}  sha256={h[:16]}…  {note}"
        for n, s, h, c, note in rows)
    return f"""S4602 Firefly SDK 固件 SD 卡交付包
==================================
从 FAT32 SD 卡根直接启动/刷写。只动 boot_b(mtd6) + system_b(mtd9) 两个 _b 槽，
_a 槽、U-Boot、loader、GPT 全部保留原厂。内核 = Firefly RV1109 4.19.111
（注入 SC5338 / SPI-NAND / YT8512B / 4G RNDIS / TUN+IPv6）；
rootfs = Firefly Buildroot + rkaiq/rkmedia/mpp，SC5338 IQ 已放入 /etc/iqfiles/。

状态: 镜像已产出并静态校验，尚未上机。上机顺序: 先第 1 步零风险验证 -> 再第 2 步写 NAND。

文件清单（全部位于 SD 卡 FAT32 根目录）
--------------------------------------
{table}

第 0 步 准备
-----------
- 一张重新格式化为 FAT32 的**好** SD 卡（原实验卡 FAT 已损坏，勿用）。
- 把本目录**所有文件**拷到 SD 卡根目录（system_b.img 必须在根，U-Boot fatload 用）。
- 串口 1500000 8N1（ttyFIQ0 / COM 口），U-Boot `=>` 可截停。
- 全片备份在 backup/snapshot/full-256M.img（写 NAND 前确认存在）。

第 1 步（零风险）SD 三参 bootm 验证内核 —— 不写任何 NAND，断电即回原厂
--------------------------------------------------------------------
=> fatload mmc 1 0x02008000 s4602-firefly_uImage.bin
=> fatload mmc 1 0x08300000 vendor-kernel.dtb
=> bootm 0x02008000 - 0x08300000
起来后 dmesg 看: sc5338 `Detected SC00ce50` / sfc_nand id `e5 f2 e5` /
14 个 GPT 分区 / 插 4G 卡出 usb0 / YT8512B 出 eth0。
（此时 rootfs 来自 NAND 上的原厂 system_a/b，并非本包 rootfs——本步只验内核与硬件描述。）

第 2 步（第 1 步通过后）刷 _b 槽 —— 四参数齐全 + 逐分区回读 CRC
------------------------------------------------------------
=> fatload mmc 1 0x1000000 boot_b.img
=> mtd write spi-nand0 0x1000000 0x00A00000 0x400000
=> mtd write spi-nand0 0x1000000 0x00A00000 0x400000   # 重复确认（可选）
=> fatload mmc 1 0x1000000 system_b.img
=> mtd write spi-nand0 0x1000000 0x03A80000 0x2C00000
=> crc32 0x1000000 0x400000        # 回读后比对 boot_b 清单值
=> reset
说明:
- 0x1000000 为实测可用加载地址（避开 FIT kernel 的 0x02008000）。
- 写长度用真实文件字节数；system_b 必须全片 0x2C00000（含 0xFF 尾部）。
- 回读比对: 先在 U-Boot 用 `sf read 0x1000000 <分区偏移> <长度>` 读出再 crc32。

第 3 步 验收
-----------
- hostname = S4602-FIREFLY；root/s4602ab 可登录。
- cmdline 应为 root=/dev/ubiblock0_0 rootfstype=squashfs ubi.mtd=9 ubi.block=0,rootfs。
- 出图: startup_app_ipc + rkmedia（/etc/iqfiles/），验证 VI->VENC->RTSP。
- 4G RNDIS 网卡 usb0 / 有线 YT8512B eth0 正常。

回滚
----
任何一步失败: _a 槽仍是原厂。从全片备份回写 _b 槽:
=> fatload mmc 1 0x1000000 <backup-system_b>
=> mtd write spi-nand0 0x1000000 0x03A80000 0x2C00000
=> fatload mmc 1 0x1000000 <backup-boot_b>
=> mtd write spi-nand0 0x1000000 0x00A00000 0x400000
或用 SD 三参 bootm 临时启动原厂 uImage/DTB。

未验证项（上机前须知）
---------------------
1. 内核 FIT + Firefly rootfs 从未真机同槽跑过，本包只做静态校验。
2. vendor DTB 与 S4602 硬件（DDR3 vs LPDDR4 / PHY / audio）逐项匹配待 dmesg 确认。
3. startup_app_ipc 在 SC5338 上的实际画质/帧率未知（路线收益点，也是最大不确定项）。
4. rootfs 精简后未做运行时依赖回归（readelf -d 静态核对通过，动态 dlopen 插件未穷举）。

校验清单见 MANIFEST.sha256 / MANIFEST.crc32 / MANIFEST.sizes。
打包来源与可追溯说明见 out/firefly_firmware/README.md。
"""


def build():
    errors = []
    if not SRC.is_dir():
        print(f"ERROR: source dir missing: {SRC}")
        return 1
    if OUT.exists():
        found = {p.name for p in OUT.iterdir()}
        extra = found - MANAGED
        if extra:
            print(f"ERROR: {OUT} has unmanaged files, refusing to overwrite: {sorted(extra)}")
            return 1
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    rows = []
    for name, _, _, note in FILES:
        src = SRC / name
        if not src.is_file():
            errors.append(f"missing source: {name}")
            continue
        check(src, errors)
        b = src.read_bytes()
        sha, crc = digest(b)
        shutil.copy2(src, OUT / name)
        rows.append((name, len(b), sha, crc, note))

    if errors:
        print("SOURCE CHECK ERRORS:")
        for e in errors:
            print("  -", e)
        return 1

    (OUT / "MANIFEST.sha256").write_text(
        "".join(f"{h}  {n}\n" for n, _, h, _, _ in rows), encoding="utf-8", newline="\n")
    (OUT / "MANIFEST.crc32").write_text(
        "".join(f"{c:08x}  {n}\n" for n, _, _, c, _ in rows), encoding="utf-8", newline="\n")
    (OUT / "MANIFEST.sizes").write_text(
        "".join(f"{n} {s}\n" for n, s, _, _, _ in rows), encoding="utf-8", newline="\n")
    (OUT / "README.txt").write_text(readme_text(rows), encoding="utf-8", newline="\n")

    total = sum(r[1] for r in rows)
    print(f"packed -> {OUT}")
    print(f"files  : {len(rows)}  total {total/1048576:.1f} MiB")
    for n, s, h, c, _ in rows:
        print(f"  {n:<26} {s:>10} B  crc32=0x{c:08x}")
    return 0


def verify():
    errors = []
    if not OUT.is_dir():
        print(f"ERROR: output dir missing: {OUT}")
        return 1
    sha_mf = (OUT / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    crc_mf = (OUT / "MANIFEST.crc32").read_text(encoding="utf-8").splitlines()
    sha_map = dict((line.split()[1], line.split()[0]) for line in sha_mf if line.strip())
    crc_map = dict((line.split()[1], line.split()[0]) for line in crc_mf if line.strip())
    for name, _, _, _ in FILES:
        p = OUT / name
        if not p.is_file():
            errors.append(f"missing: {name}")
            continue
        check(p, errors)
        sha, crc = digest(p.read_bytes())
        if sha_map.get(name) != sha:
            errors.append(f"{name}: sha256 mismatch")
        if crc_map.get(name) != f"{crc:08x}":
            errors.append(f"{name}: crc32 mismatch")
    for extra in (set(p.name for p in OUT.iterdir()) - MANAGED):
        errors.append(f"unmanaged file: {extra}")
    if errors:
        print("VERIFY FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print(f"VERIFY OK: {OUT}  ({len(FILES)} images + 4 manifests, all sha256/crc32 match)")
    return 0


if __name__ == "__main__":
    sys.exit(verify() if "--verify" in sys.argv else build())
