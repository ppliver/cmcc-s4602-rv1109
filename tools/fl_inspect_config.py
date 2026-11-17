#!/usr/bin/env python3
"""Inspect Firefly rv1126 kernel on 177 to find what bloats zImage.

Read-only. 192.168.219.177 zhang/admin888, tree at ~/fl-kernel
(shallow clone of tag rv1126_rv1109/linux_release_v2.2.5g).
No Python %-formatting on shell commands (literal paths) to avoid % escaping bugs.
"""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=120):
    sin, sout, serr = c.exec_command(cmd, timeout=timeout)
    out = sout.read().decode("utf-8", "replace")
    err = serr.read().decode("utf-8", "replace")
    rc = sout.channel.recv_exit_status()
    return rc, out, err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BUILD[0], BUILD[1], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)

    cmds = [
        ("HEAD + artifacts",
         "cd ~/fl-kernel && git log -1 --format='HEAD %H %ci'; ls -l arch/arm/boot/zImage arch/arm/boot/dts/rv1109-38-v10-spi-nand.dtb | awk '{print $5, $NF}'"),
        ("defconfig counts",
         "cd ~/fl-kernel/arch/arm/configs && echo lines=$(wc -l < rv1126_defconfig) enabled_y=$(grep -c '=y$' rv1126_defconfig) module_m=$(grep -c '=m$' rv1126_defconfig)"),
        ("last .config counts",
         "cd ~/fl-kernel && echo lines=$(wc -l < .config) enabled_y=$(grep -c '=y$' .config) module_m=$(grep -c '=m$' .config)"),
        ("group counts (defconfig vs .config)",
         "cd ~/fl-kernel; for g in FB FBDEV DRM MPP RGA RGA2 RGA3 VPU VDEC VENC ISP ISPP RKCIF "
         "HDMI USB_XHCI USB_OHCI USB_UHCI SATA NVME MMC_SDHCI MMC_SDIO SOUND SND BLUETOOTH WIFI "
         "DEBUG_KERNEL LOCKDEP KGDB XZ_DEC LZO_LZO LZ4_COMP; do "
         "d=$(grep -cE \"^CONFIG_${g}[=_]\" arch/arm/configs/rv1126_defconfig 2>/dev/null); "
         "cc=$(grep -cE \"^CONFIG_${g}=y\" .config 2>/dev/null); "
         "printf '%-16s defcfg=%s .config_y=%s\\n' \"$g\" \"$d\" \"$cc\"; done"),
        ("final .config: display/storage/sound/debug/compression =y",
         "cd ~/fl-kernel && grep -E '^(CONFIG_(FB|FBDEV|DRM|MPP|RGA|SND|SOUND|USB_XHCI|USB_OHCI|USB_UHCI|SATA|NVME|DEBUG_KERNEL|LOCKDEP|KGDB|XZ_DEC|LZO_COMPRESS|LZ4_COMP|KERNEL_))' .config | grep -E '=y$' | sort"),
        ("kernel compression method",
         "cd ~/fl-kernel && grep -E '^CONFIG_(KERNEL_GZIP|KERNEL_LZ4|KERNEL_LZO|KERNEL_XZ|XZ_DEC|XZ_DEC_BCJ|LZO_COMPRESS|LZ4_COMP|GZIP)' .config | sort"),
    ]

    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd)
        sys.stdout.write(o)
        if e.strip():
            sys.stdout.write("[stderr] " + e[:400] + "\n")
        print()

    c.close()


if __name__ == "__main__":
    sys.exit(main())
