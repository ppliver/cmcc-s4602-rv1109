#!/usr/bin/env python3
"""Diagnose why `make zImage` / dtb failed on 177 after slim fragment."""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=600, stdin=None):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    if stdin is not None:
        for i in range(0, len(stdin), 48 * 1024):
            s.write(stdin[i:i + 48 * 1024])
        s.flush()
    s.close()
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out, err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BUILD[0], BUILD[1], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)

    cmds = [
        ("tail /tmp/zimg.log (errors)",
         "grep -nE 'error|Error|undefined|No rule|missing|inconsistent|unsatisfied' /tmp/zimg.log | tail -40; echo '--- last 20 lines ---'; tail -20 /tmp/zimg.log"),
        ("tail /tmp/dtb.log",
         "tail -20 /tmp/dtb.log"),
        ("is .config still valid? (olddefconfig dry)",
         "export PATH=$HOME/.local/bin:$PATH; cd ~/fl-kernel && export HOSTLDFLAGS='-Wl,--allow-multiple-definition' && make ARCH=arm olddefconfig >/tmp/old2.log 2>&1; echo rc=$?; grep -iE 'not found|unsatisfied|inconsistent' /tmp/old2.log | head"),
        ("count: does trim accidentally kill a media dep?",
         "cd ~/fl-kernel && grep -E '^CONFIG_(MEDIA_SUPPORT|VIDEO_ROCKCHIP_CIF|VIDEO_ROCKCHIP_ISP|VIDEO_ROCKCHIP_ISPP|VIDEO_V4L2|VIDEO_V4L2_SUBDEV_API)=y' .config"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd, timeout=600)
        sys.stdout.write(o if o.strip() else "(empty)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[-400:] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
