#!/usr/bin/env python3
"""Probe kernel-compression choice options on 177 (4.19) + current .config.

Confirms exact symbol names (KERNEL_LZ4 vs KERNEL_XZ), their select deps,
and whether XZ_DEC is present so we can switch compression to shrink zImage.
"""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=120):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out, e.read().decode("utf-8", "replace")


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(*BUILD[:2], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)
    cmds = [
        ("init/Kconfig: kernel compression choice block",
         "awk '/^choice/{f=1} f&&/Kernel compression/{p=NR} p&&p<=NR{print} /^endchoice/{if(f){f=0}}' ~/fl-kernel/init/Kconfig 2>/dev/null | head -60"),
        ("grep KERNEL_ comp symbols + select/depends in init/Kconfig",
         "grep -nE '^config KERNEL_(GZIP|LZMA|XZ|LZ4|LZO|NONE|ZSTD)|select +XZ_DEC|select +CRYPTO_LZ4|select +LZO|depends on XZ' ~/fl-kernel/init/Kconfig"),
        ("current .config: which KERNEL_* comp is on",
         "grep -E '^CONFIG_KERNEL_(GZIP|LZMA|XZ|LZ4|LZO|NONE|ZSTD)' ~/fl-kernel/.config"),
        ("XZ_DEC availability (ARM) in .config",
         "grep -E '^CONFIG_XZ_DEC' ~/fl-kernel/.config"),
        ("defconfig: compression lines",
         "grep -nE '^CONFIG_(KERNEL_|XZ_DEC|LZ4|LZO)' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd, timeout=120)
        sys.stdout.write(o if o.strip() else "(none)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[-300:] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
