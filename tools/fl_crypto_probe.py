#!/usr/bin/env python3
"""Probe whether top-level CRYPTO can be turned off on this 4.19 tree, and find
the remaining safe trim candidates (PM/SERIAL/XZ/DEBUG/ROCKCHIP misc).
"""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=180):
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
        ("who SELECTs CRYPTO (top-level) in full tree",
         "grep -rnE 'select +CRYPTO\\b' ~/fl-kernel --include=Kconfig 2>/dev/null | head -30; "
         "echo '--- count ---'; grep -rlE 'select +CRYPTO\\b' ~/fl-kernel --include=Kconfig 2>/dev/null | wc -l"),
        ("crypto top-level defconfig lines",
         "grep -nE '^CONFIG_(CRYPTO|CRYPTO_(ALGAPI|ALGAPI2|MANAGER|WORKQUEUE|NULL|NULL2|RNG|RNG2))' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
        ("remaining =y in PM / SERIAL / XZ / DEBUG / ROCKCHIP-misc / MTD",
         "grep -E '^CONFIG_(PM_|SERIAL_|UART_|XZ|DEBUG|ROCKCHIP_|MTD_|ARM_)=y' ~/fl-kernel/.config | sort"),
        ("how many =y total now (round3)",
         "grep -cE '^CONFIG_.*=y$' ~/fl-kernel/.config"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd, timeout=180)
        sys.stdout.write(o if o.strip() else "(none)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[-300:] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
