#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4602 bootm 控制器（后台）。

阶段 1（arm）：每 0.1s 发 Ctrl+C 抢 U-Boot autoboot 窗口（最长 300s），
             检测到 "=>" 提示符后自动发 `fatls mmc 1` 验卡。
阶段 2：转为监听模式 —— 捕获全部输出到 docs/evidence/，
             轮询 tools/serial_send.txt 注入命令（逐行发送后删除）。
"""
import time
from datetime import datetime
from pathlib import Path

import serial

PORT = "COM4"
BAUD = 1500000
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"
SEND = ROOT / "tools" / "serial_send.txt"

CTRL_C = b"\x03"
PROMPT = b"=>"

log = EVIDENCE / ("bootm-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
f = open(log, "a", encoding="utf-8", errors="replace")
print("BOOTM CTRL START -> %s" % log, flush=True)

ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                    timeout=0.1, xonxoff=False, rtscts=False, dsrdtr=False)
buf = bytearray()


def emit(b):
    buf.extend(b)
    f.write(b.decode("utf-8", errors="replace"))
    f.flush()


# ---- 阶段 1: 抢 autoboot ----
t0 = time.time()
acquired = False
while time.time() - t0 < 300:
    ser.write(CTRL_C)
    time.sleep(0.1)
    b = ser.read(4096)
    if b:
        emit(b)
        if PROMPT in bytes(buf[-256:]):
            acquired = True
            break
print("PROMPT_ACQUIRED=%s (%.1fs)" % (acquired, time.time() - t0), flush=True)

if acquired:
    time.sleep(0.3)
    ser.write(b"fatls mmc 1\r\n")
    ser.flush()
    # 收 fatls 输出
    t1 = time.time()
    mark = len(buf)
    while time.time() - t1 < 8:
        b = ser.read(4096)
        if b:
            emit(b)
        time.sleep(0.05)
    print("FATLS_DONE, bytes=%d" % (len(buf) - mark), flush=True)

# ---- 阶段 2: 监听 + 注入 ----
while True:
    b = ser.read(4096)
    if b:
        emit(b)
    try:
        if SEND.exists() and SEND.stat().st_size > 0:
            cmds = SEND.read_text(encoding="utf-8", errors="replace")
            SEND.unlink()
            for line in cmds.splitlines():
                line = line.strip()
                if not line:
                    continue
                f.write("\n>>> SEND: %s\n" % line)
                f.flush()
                ser.write((line + "\r\n").encode("utf-8", errors="replace"))
                ser.flush()
                time.sleep(0.3)
    except Exception as ex:
        f.write("\n[send-err] %s\n" % ex)
        f.flush()
    time.sleep(0.05)
