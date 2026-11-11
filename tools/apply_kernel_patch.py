#!/usr/bin/env python3
"""
Inject the SC5338 sensor driver into an OpenIPC/linux tree (rockchip-rv11xx).

OpenIPC/linux does not ship a SC5338 driver, so before building we copy
sc5338.c + sc5338-tables.h into drivers/media/i2c/ and wire them into the
Makefile + Kconfig.  This makes CONFIG_VIDEO_SC5338 a real tristate symbol so
the fragment (board/s4602/s4602_kernel_fragment.config) survives olddefconfig.

Usage:
  python3 tools/apply_kernel_patch.py --linux /opt/linux [--patch-dir src/kernel-patches]

Idempotent: re-running does not duplicate Makefile/Kconfig entries.
"""
import argparse
import os
import sys

DRIVER_C = "sc5338.c"
DRIVER_H = "sc5338-tables.h"

MAKEFILE_OBJ = "obj-$(CONFIG_VIDEO_SC5338) += sc5338.o"

KCONFIG_BLOCK = """config VIDEO_SC5338
\ttristate "SmartSens SC5338 sensor support"
\tdepends on I2C && VIDEO_V4L2 && V4L2_SUBDEV_API
\thelp
\t  Support for the SmartSens SC5338 CMOS image sensor (chip id 0xce50,
\t  2-lane MIPI CSI-2). Enables linear 2880x1620@30fps and HDR_X2
\t  2880x1620@15fps on the China Mobile S4602 (RV1109) camera. To compile
\t  this driver as a module, choose M here. If unsure, say N.
"""


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
            # Find the next top-level 'config ' or 'endmenu' after this block.
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


def main():
    ap = argparse.ArgumentParser(description="Inject SC5338 driver into OpenIPC/linux")
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
    print("Done.")


if __name__ == "__main__":
    main()
