# -*- coding: utf-8 -*-
"""通过 SSH 把本地文件传到 S4602 设备（原始二进制，不走 base64，避免体积膨胀）。

用法: python tools/ssh_push.py <local_path> <remote_path>
"""
import sys, os, paramiko

HOST, PORT, USER, PASS = "192.168.6.214", 22, "root", "admin888"


def push(local, remote):
    data = open(local, "rb").read()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=15, look_for_keys=False, allow_agent=False)
    try:
        cmd = "cat > %s" % remote
        stdin, stdout, stderr = c.exec_command(cmd, timeout=120)
        stdin.write(data)
        stdin.flush()
        stdin.channel.shutdown_write()
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        print("[*] pushed %d bytes -> %s (rc=%d)" % (len(data), remote, rc))
        if err.strip():
            print("[!] stderr:", err[:400])
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    push(sys.argv[1], sys.argv[2])
