# -*- coding: utf-8 -*-
"""经 SSH 在设备上执行命令并打印输出（webd /api/exec 抖动时的可靠替代通道）。

用法: python tools/ssh_run.py "shell command" ["another command" ...]
"""
import sys, paramiko

HOST, PORT, USER, PASS = "192.168.6.214", 22, "root", "admin888"


def main():
    cmds = sys.argv[1:]
    if not cmds:
        print("usage: ssh_run.py <cmd> [cmd...]")
        return 1
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=20, look_for_keys=False, allow_agent=False)
    try:
        for cmd in cmds:
            print("$ " + cmd)
            i, o, e = c.exec_command(cmd, timeout=120)
            out = o.read().decode("utf-8", "replace")
            err = e.read().decode("utf-8", "replace")
            if out:
                print(out.rstrip())
            if err.strip():
                print("  [stderr] " + err.strip())
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
