#!/usr/bin/env python3
"""串口原始字节探针：按给定波特率各监听若干秒，打印字节分布与十六进制头部。

用途：区分「真启动日志」「波特率错乱乱码」「TX 浮空噪声(0x00/0xFF 为主)」。
用法: python3 tools/serial_hex_probe.py [PORT] [BAUDS] [SECONDS]
  python3 tools/serial_hex_probe.py COM4 1500000 8
  python3 tools/serial_hex_probe.py COM4 1500000,115200,921600 6
"""
import collections
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM4"
bauds = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["1500000"])]
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

for baud in bauds:
    print(f"\n===== {port} @ {baud} 8N1, listen {secs}s =====", flush=True)
    try:
        ser = serial.Serial(port, baud, timeout=0.2, bytesize=8, parity="N", stopbits=1)
    except Exception as e:
        print("OPEN FAILED:", e, flush=True)
        continue
    try:
        ser.reset_input_buffer()
        data = bytearray()
        t0 = time.time()
        while time.time() - t0 < secs:
            chunk = ser.read(4096)
            if chunk:
                data.extend(chunk)
    finally:
        ser.close()

    n = len(data)
    print(f"bytes = {n}")
    if n == 0:
        print(">>> 完全静默（TX 无输出或线断）")
        continue
    hist = collections.Counter(data)
    print("top bytes:", [(f"0x{k:02x}", v) for k, v in hist.most_common(10)])
    printable = sum(1 for x in data if 32 <= x < 127 or x in (10, 13, 9))
    print(f"printable ratio = {printable / n:.3f}")
    print("head hex  :", bytes(data[:96]).hex())
    print("head repr :", repr(bytes(data[:160])))
    # 判定
    zeros = hist.get(0x00, 0) + hist.get(0xFF, 0)
    if zeros / n > 0.8:
        print(">>> 判定: 0x00/0xFF 占比 >80% -> 持续 break 电平 / TX 浮空 / 接触不良（硬件侧）")
    elif printable / n > 0.7:
        print(">>> 判定: 可读 ASCII 为主 -> 这是真日志，该波特率正确")
    else:
        print(">>> 判定: 随机乱码 -> 波特率不匹配或信号完整性差")
