#!/usr/bin/env python3
"""临时重启 h264out 抓一帧编码前 NV12/Y 平面，拉回本机转 PNG，然后恢复推流。

用法: python tools/capture_live_frame.py [output_prefix]
"""
import os, sys, time, paramiko
from PIL import Image
import numpy as np

IP = os.environ.get("DEV_IP", "192.168.6.5")
USER = os.environ.get("DEV_USER", "root")
PW = os.environ.get("DEV_PASS", "admin888")
prefix = sys.argv[1] if len(sys.argv) > 1 else "out/captures/live_now"
W, H = 640, 360

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(IP, username=USER, password=PW, timeout=15, allow_agent=False, look_for_keys=False)

def run(cmd, timeout=60):
    _, so, se = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode("utf-8", "replace")
    err = se.read().decode("utf-8", "replace")
    return out, err

print("[1] stop sender")
print(run("sh /tmp/start_h264out.sh stop", 30)[0][-500:])

cmd = (
    "rm -f /tmp/live.nv12 /tmp/live.pgm /tmp/live.raw; "
    "LD_LIBRARY_PATH=/tmp/factory/lib nohup /tmp/%s --src raw --dev /dev/video0 "
    % os.environ.get("BIN", "h264out") +
    "--ip 192.168.6.88 --port 5600 --codec h264 --w 640 --h 360 --box %s "
    % os.environ.get("BOX", "4") +
    "--chroma %s --cbox %s --threads 2 --fps 25 --kbps 1500 --frames 0 "
    % (os.environ.get("CHROMA", "100"), os.environ.get("CBOX", "3")) +
    "--vbufs 3 --nfrmbuf 1 --dump /tmp/live.pgm --nv12dump /tmp/live.nv12 "
    "--rawdump /tmp/live.raw --dumpdelay 120"
    + (" --nowb" if os.environ.get("NOWB") == "1" else "")
    + " > /tmp/h264out.dump.log 2>&1 < /dev/null &"
)
print("[2] start with dump")
run(cmd, 30)
time.sleep(14)
print(run("grep -E 'dump|FATAL' /tmp/h264out.dump.log | head -10; ls -l /tmp/live.nv12 /tmp/live.pgm 2>&1", 20)[0])
print(run("pkill -9 -x h264out 2>/dev/null; true", 20)[0])

# 设备没有 SFTP 子系统；用 nc_fetch.py 单独拉取。
print("[3] files ready on device, fetch with: python tools/nc_fetch.py live.nv12 out/captures/live.nv12")

print("[4] restore sender")
print(run("sh /tmp/start_h264out.sh start", 40)[0][-800:])
c.close()

# 注意：设备 dump 出的 NV12 行宽是 MPP 对齐后的 g_hs=768（不是 640），
# UV 平面按 (VS/2, HS/2, 2) 交错。必须用 nv12_to_png.py 解析，
# 否则会把正常画面读成条纹噪声（2026-11-16 踩过这个坑）。
print("[5] use tools/nv12_to_png.py on the fetched .nv12 to build PNG")
