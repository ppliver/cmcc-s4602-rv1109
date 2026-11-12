#!/usr/bin/env python3
"""
Inject the S4602 board support into an OpenIPC/linux tree (rockchip-rv11xx).

Two independent fixes are applied before `make`:

1. SC5338 sensor driver -- OpenIPC/linux does not ship it, so we copy
   sc5338.c + sc5338-tables.h into drivers/media/i2c/ and wire them into the
   Makefile + Kconfig. This makes CONFIG_VIDEO_SC5338 a real tristate symbol
   so the fragment survives olddefconfig.

2. S4602 SPI-NAND id -- the Rockchip rkflash driver's spi_nand_tbl[] has no
   entry for the board's flash id `e5 f2 e5`, so sfc_nand_init() returns
   FTL_UNSUPPORTED_FLASH, no MTD device is created, and UBI fails with
   "cannot open mtd 9, error -19". We inject the exact table entry recovered
   byte-for-byte from the vendor 4.19.111 vmlinux (spi_nand_tbl @0xb3f674).

Usage:
  python3 tools/apply_kernel_patch.py --linux /opt/linux [--patch-dir src/kernel-patches]

Idempotent: re-running does not duplicate Makefile/Kconfig/table entries.
"""
import argparse
import os

DRIVER_C = "sc5338.c"
DRIVER_H = "sc5338-tables.h"

MAKEFILE_OBJ = "obj-$(CONFIG_VIDEO_SC5338) += sc5338.o"

KCONFIG_BLOCK = """config VIDEO_SC5338
\ttristate "SmartSens SC5338 sensor support"
\tdepends on I2C && VIDEO_V4L2
\thelp
\t  Support for the SmartSens SC5338 CMOS image sensor (chip id 0xce50,
\t  2-lane MIPI CSI-2). Enables linear 2880x1620@30fps and HDR_X2
\t  2880x1620@15fps on the China Mobile S4602 (RV1109) camera. To compile
\t  this driver as a module, choose M here. If unsure, say N.
"""

# Recovered from vendor 4.19.111 vmlinux (docs/evidence/vendor-vmlinux.bin),
# spi_nand_tbl entry @0xb3f674:
#   e5 f2 00 00 | 04 00 40 00 02 00 00 04 | 0c 13 08 01 | 04 14 ff ff | ptr
# id=e5 f2 00, sec_per_page=4, page_per_blk=64, plane_per_die=2,
# blk_per_plane=1024, feature=0x0C, density=19 (=256 MiB), max_ecc_bits=8,
# has_qe_bits=1, meta={04,14,FF,FF}.
# The function pointer 0xb05f15b0 is shared with id 0x2C,0x24, which maps to
# &sfc_nand_get_ecc_status6 in the OpenIPC tree.
NAND_TABLE_ENTRY = (
    "\t/* CMCC S4602 SPI-NAND: id e5 f2 e5, 256 MiB, 2 KiB page, 128 KiB block,\n"
    "\t * 2 planes. Recovered byte-exact from vendor 4.19.111 vmlinux\n"
    "\t * spi_nand_tbl @0xb3f674; upstream rockchip-rv11xx lacks this id and\n"
    "\t * sfc_nand_init() otherwise returns FTL_UNSUPPORTED_FLASH.\n"
    "\t */\n"
    "\t{ 0xE5, 0xF2, 0x00, 4, 0x40, 2, 1024, 0x0C, 19, 0x8, 1,\n"
    "\t  { 0x04, 0x14, 0xFF, 0xFF }, &sfc_nand_get_ecc_status6 },\n"
)

NAND_TABLE_MARKER = "static struct nand_info spi_nand_tbl[] = {"


def copy_driver(patch_dir, i2c_dir):
    os.makedirs(i2c_dir, exist_ok=True)
    for fn in (DRIVER_C, DRIVER_H):
        src = os.path.join(patch_dir, fn)
        if not os.path.isfile(src):
            raise SystemExit("ERROR: missing patch file: %s" % src)
        dst = os.path.join(i2c_dir, fn)
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        try:
            rel = os.path.relpath(dst)
        except ValueError:
            rel = dst
        print("  copied %s -> %s" % (fn, rel))


def patch_makefile(i2c_dir):
    path = os.path.join(i2c_dir, "Makefile")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if any(MAKEFILE_OBJ in ln for ln in lines):
        print("  Makefile already references %s (skip)" % DRIVER_C)
        return
    # Append after the last existing 'obj-$(CONFIG_VIDEO_' line to keep grouping.
    insert_at = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("obj-$(CONFIG_VIDEO_"):
            insert_at = i + 1
            break
    if insert_at is None:
        insert_at = len(lines)
    lines.insert(insert_at, MAKEFILE_OBJ + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print("  Makefile: added '%s'" % MAKEFILE_OBJ)


def patch_kconfig(i2c_dir):
    path = os.path.join(i2c_dir, "Kconfig")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if "config VIDEO_SC5338" in content:
        print("  Kconfig already has VIDEO_SC5338 (skip)")
        return

    lines = content.splitlines(keepends=True)
    insert_at = None

    # Preferred anchor: right after the SC500AI block (both SmartSens sensors).
    for i, ln in enumerate(lines):
        if ln.strip() == "config VIDEO_SC500AI":
            for j in range(i + 1, len(lines)):
                s = lines[j].strip()
                if s.startswith("config ") or s == "endmenu":
                    insert_at = j
                    break
            break

    # Fallback: before the last 'endmenu' in the file.
    if insert_at is None:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "endmenu":
                insert_at = i
                break

    if insert_at is None:
        insert_at = len(lines)

    # Ensure a blank line separator before the block.
    if insert_at > 0 and lines[insert_at - 1].strip() != "":
        lines.insert(insert_at, "\n")
        insert_at += 1
    lines.insert(insert_at, KCONFIG_BLOCK)
    if lines[insert_at + 1].strip() != "":
        lines.insert(insert_at + 1, "\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print("  Kconfig: added 'config VIDEO_SC5338'")


def patch_spi_nand_tbl(linux):
    """Add the S4602 SPI-NAND id to drivers/rkflash/sfc_nand.c."""
    path = os.path.join(linux, "drivers", "rkflash", "sfc_nand.c")
    if not os.path.isfile(path):
        raise SystemExit("ERROR: %s not found (is --linux the linux tree?)" % path)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if "0xE5, 0xF2" in content or "0xe5, 0xf2" in content:
        print("  sfc_nand.c already has 0xE5,0xF2 entry (skip)")
        return
    start = content.find(NAND_TABLE_MARKER)
    if start < 0:
        raise SystemExit("ERROR: cannot find spi_nand_tbl in %s" % path)
    end = content.find("\n};", start)
    if end < 0:
        raise SystemExit("ERROR: cannot find end of spi_nand_tbl in %s" % path)
    # `end+1` points at the '};' line; insert our entry just before it.
    patched = content[:end + 1] + NAND_TABLE_ENTRY + content[end + 1:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    print("  sfc_nand.c: injected 0xE5,0xF2 (S4602 SPI-NAND) table entry")


def main():
    ap = argparse.ArgumentParser(description="Inject S4602 board support into OpenIPC/linux")
    ap.add_argument("--linux", required=True, help="path to OpenIPC/linux tree")
    ap.add_argument("--patch-dir", default=None, help="dir containing sc5338.c / sc5338-tables.h")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    patch_dir = args.patch_dir or os.path.join(here, "..", "src", "kernel-patches")
    patch_dir = os.path.abspath(patch_dir)
    linux = os.path.abspath(args.linux)
    i2c_dir = os.path.join(linux, "drivers", "media", "i2c")

    if not os.path.isdir(i2c_dir):
        raise SystemExit("ERROR: %s does not exist (is --linux the linux tree?)" % i2c_dir)

    print("Patching OpenIPC/linux at %s" % linux)
    print("Using patch dir %s" % patch_dir)
    copy_driver(patch_dir, i2c_dir)
    patch_makefile(i2c_dir)
    patch_kconfig(i2c_dir)
    patch_spi_nand_tbl(linux)
    print("Done.")


if __name__ == "__main__":
    main()
