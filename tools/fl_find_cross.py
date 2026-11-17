#!/usr/bin/env python3
"""Locate an arm cross gcc on 177 (for full zImage build)."""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=60, stdin=None):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    if stdin is not None:
        s.write(stdin)
        s.flush()
    s.close()
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out, err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(*BUILD[:2], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)
    cmds = [
        ("PATH cross gcc",
         "for g in $(compgen -c | grep -E '^(arm|aarch64)-.*-gcc$' | sort -u); do p=$(command -v $g); [ -n \"$p\" ] && echo \"$g -> $p\"; done; echo '--- which ---'; which arm-linux-gnueabihf-gcc aarch64-linux-gnu-gcc 2>/dev/null"),
        ("Firefly prebuilt toolchain on 177?",
         "ls -d ~/*/prebuilts ~/*/gcc-linaro* /opt/*fl* /opt/*gcc* 2>/dev/null; ls -l ~/fl-tc/bin/arm-linux-gnueabihf-gcc 2>/dev/null; ls -l /opt/fl-tc/bin/arm-linux-gnueabihf-gcc 2>/dev/null"),
        ("any arm gcc under home/opt",
         "find ~ /opt -maxdepth 5 -name 'arm-linux-gnueabihf-gcc' 2>/dev/null | head"),
        ("gcc version of found arm gcc",
         "arm-linux-gnueabihf-gcc --version 2>/dev/null | head -1 || echo 'arm-linux-gnueabihf-gcc not in PATH'"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd, timeout=120)
        sys.stdout.write(o if o.strip() else "(none)\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
