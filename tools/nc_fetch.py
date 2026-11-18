#!/usr/bin/env python3
"""Fetch a device file over raw TCP (device `nc HOST PORT < FILE`), no SFTP needed."""
import os, socket, sys, threading, paramiko

IP = os.environ.get("DEV_IP", "192.168.6.5")
HOST_IP = os.environ.get("HOST_IP", "192.168.6.109")
remote = "/tmp/" + sys.argv[1] if not sys.argv[1].startswith("/") else sys.argv[1]
local = sys.argv[2]
port = int(sys.argv[3]) if len(sys.argv) > 3 else 18080

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", port))
srv.listen(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(IP, username="root", password="admin888", timeout=15, allow_agent=False, look_for_keys=False)
_, so, se = c.exec_command("cd /tmp && cat %s | nc %s %d" % (os.path.basename(remote), HOST_IP, port), timeout=90)

srv.settimeout(30)
conn, addr = srv.accept()
data = bytearray()
while True:
    chunk = conn.recv(65536)
    if not chunk:
        break
    data.extend(chunk)
conn.close()
srv.close()
c.close()
open(local, "wb").write(data)
print("fetched %s -> %s (%d bytes)" % (remote, local, len(data)))
