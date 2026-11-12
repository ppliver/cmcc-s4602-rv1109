#!/usr/bin/env python3
"""一体化非破坏性 RAM 启动探针：等待上电 -> 截停 autoboot -> 从 RAM 启动 FIT -> 完整落盘。

背景：原厂 U-Boot 2017.09 无 iminfo，但 boot_fit [addr] 支持从内存启动 FIT。
     本脚本把「进 U-Boot」和「拿完整内核日志」合成一次动作，避免反复断电往返。

流程：
  1. 全程持续发截停键（空格 / CTRL+C 交替，每 0.12s），直到看见 '=>' 提示符
     —— 不依赖启动横幅检测：原厂 bootdelay 只有 1 秒，横幅一帧 2726B，
        按「最后 600 字符」检测会漏掉头部 DDR Version，导致一次都没发按键（v1 的 bug）。
        持续发送对已运行的原厂 Linux getty 无害（只刷新登录提示符）。
  2. fatload mmc 1 0x1000000 <image>（整卡 FAT 卷，不能写 1:1）
     + crc32/fdt header 校验，然后 boot_fit 0x1000000
  3. 全量捕获内核日志（不 tail 截断），写 docs/evidence/，打印关键行与 panic 上下文

用法: python3 tools/boot_fit_probe_full.py [image] [wait_sec] [capture_sec]
注意: 全程零 flash 写入；内核 panic 后 CPU 挂死，需断电复位才能回到 '=>'.
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

PORT = "COM4"
BAUD = 1500000
ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "docs" / "evidence"

image = sys.argv[1] if len(sys.argv) > 1 else "zboot.img.s4602"
wait_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
capture_sec = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0

ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                    timeout=0.15, xonxoff=False, rtscts=False, dsrdtr=False)
ser.reset_input_buffer()
log = bytearray()


def pump(seconds, spam=False, label=""):
    """读取 seconds 秒，实时回显；spam=True 时周期性发空格以截停 autoboot。"""
    t0 = time.time()
    last_spam = 0.0
    while time.time() - t0 < seconds:
        b = ser.read(4096)
        if b:
            log.extend(b)
            prev = b.decode("utf-8", errors="replace").replace("\r", "\\r")
            print(f"[{label} rx +{len(b)}] {prev[:180]}", flush=True)
        if spam and time.time() - t0 - last_spam > 0.3:
            ser.write(b" ")
            ser.flush()
            last_spam = time.time() - t0
        else:
            time.sleep(0.02)
    return bytes(log)


def hit(text, keys):
    low = text.lower()
    return any(k in low for k in keys)


try:
    # ---- 1. 每帧检测启动横幅, 命中后高频发截停键, 直到 '=>' ----
    # v1 教训: bootdelay=1s, 启动横幅一帧 2726B, 按累积文本「最后 600 字符」检测会漏掉
    # 头部 DDR Version -> boot_seen 恒 False -> 一次按键都没发 -> 设备直接启动进 Linux。
    # v2: 直接在本帧字节里找横幅; 命中后每 0.06s 发一次按键, 1s 倒计时内有 ~16 次机会。
    # 未命中时不发任何字节, 不骚扰正在运行的原厂 Linux getty。
    prompt = False
    booting = False
    t0 = time.time()
    last_send = 0.0
    toggle = 0
    print(f"[1] 等待上电（最长 {wait_sec:.0f}s）；命中启动横幅后自动截停 ...", flush=True)
    while time.time() - t0 < wait_sec:
        b = ser.read(4096)
        if b:
            log.extend(b)
            prev = b.decode("utf-8", errors="replace").replace("\r", "\\r")
            print(f"[1 rx +{len(b)}] {prev[:180]}", flush=True)
            low = b.decode("utf-8", errors="replace").lower()
            if not booting and any(k in low for k in ("ddr version", "u-boot spl", "spl:", "bootrom", "maskrom")):
                booting = True
                print("[1] boot banner seen -> start spamming stop keys", flush=True)
            if booting and "=>" in log.decode("utf-8", errors="replace")[-600:]:
                prompt = True
                break
            if booting and "booting fit image" in low:
                print("[1] WARN: 已开始加载 FIT，本次未截住；继续等待下一次上电", flush=True)
                booting = False
        now = time.time()
        if booting and now - last_send > 0.06:
            ser.write(b"\x03" if toggle % 3 == 0 else b" ")
            ser.flush()
            toggle += 1
            last_send = now
        time.sleep(0.01)

    # 确认提示符：停止发送，发回车清空命令行缓冲
    if prompt:
        time.sleep(0.4)
        ser.write(b"\r\n")
        ser.flush()
        pump(1.5, spam=False, label="prompt-check")
        text = log.decode("utf-8", errors="replace")
        prompt = "=>" in text[-600:]
    print(f"[1] prompt_seen = {prompt}", flush=True)

    # ---- 2. RAM 加载 FIT 并启动（零 flash 写入）----
    if not prompt:
        print("[!] 未进入 U-Boot '=>'，跳过 boot_fit；日志已保留供诊断", flush=True)
    else:
        cmd = ("fatload mmc 1 0x1000000 " + image + " ; "
               "crc32 0x1000000 0x339b14 ; "
               "fdt addr 0x1000000 ; fdt header ; "
               "boot_fit 0x1000000")
        print("[2] " + cmd, flush=True)
        ser.write((cmd + "\r\n").encode("utf-8", errors="replace"))
        ser.flush()
        pump(capture_sec, spam=False, label="kernel")
        print(f"[2] capture done, total bytes = {len(log)}", flush=True)
finally:
    ser.close()

# ---- 3. 落盘 + 关键行 / panic 上下文 ----
text = log.decode("utf-8", errors="replace")
EVID.mkdir(parents=True, exist_ok=True)
out = EVID / ("live-bootfit-full-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
out.write_text(text, encoding="utf-8", errors="replace")
print("=== saved full log:", out, "bytes:", len(log), "===", flush=True)

KEY = re.compile(
    r"Kernel command line|Linux version|Booting Linux|Starting kernel|"
    r"Fatal exception|Kernel panic|Oops|Unhandled fault|Unable to handle|"
    r"Call trace|PC is at|LR is at|bad mode|Internal error|"
    r"ubi[0-9]?:|UBIFS|VFS:|mtdparts|OF:|reserved-memory|"
    r"sc5338|crc32 for|bytes read|Not fit|fit magic|Error|failed|Failed",
    re.IGNORECASE,
)
lines = text.splitlines()
hits = [(i, ln) for i, ln in enumerate(lines) if KEY.search(ln)]
print(f"=== key lines ({len(hits)}) ===", flush=True)
for i, ln in hits[:150]:
    print(f"{i:5d}: {ln[:240]}", flush=True)

PANIC = re.compile(r"Fatal exception|Kernel panic|Unable to handle|Internal error|Unhandled fault", re.I)
pidx = [i for i, ln in enumerate(lines) if PANIC.search(ln)]
if pidx:
    p = pidx[0]
    print(f"=== panic context (line {p}, {max(0, p - 25)}..{min(len(lines), p + 45)}) ===", flush=True)
    for i in range(max(0, p - 25), min(len(lines), p + 45)):
        print(f"{i:5d}: {lines[i][:240]}", flush=True)
