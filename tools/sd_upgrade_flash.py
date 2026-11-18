#!/usr/bin/env python3
"""S4602 SD 升级固件烧写器（2026-11-18）：只刷 mtd9(system_b) = 钩子镜像 v2
（SD 引导 S99local + bootfix v7），全新原厂机首装专用。

与 flash_from_sd.py 的区别：
  * 只写 system_b (mtd9, 0x03A80000, 0x02C00000=44MB)，完全不碰 boot_b / U-Boot / 内核；
    boot_b 保持当前能配套启动的状态（云端正常），新 rootfs 挂上即触发 S99local.sh。
  * 镜像 = upgrade/s4602_upgrade/flash/system_b.img（钩子 v2：S99local 从 SD 引导
    bootfix.sh；刷完后首启自动装 base/optional，无需再串口推文件）。

安全设计（沿用全项目红线）：
  * 默认 DRY-RUN：等上电截停 U-Boot + 只读校验，绝不写 flash；显式 --go 才写。
  * 写前 3 项只读校验（SD 文件大小 / fatload 字节数 / 源 crc32）任一不过即 ABORT。
  * 写后 mtd read + crc32 双向比对，不符即 ABORT（绝不继续）。
  * 只写 mtd9，system_a/mtd8 原厂保留作回滚。
  * 全程原始字节日志 + 状态文件，任何异常都释放 COM4。

用法：
  python tools/flash_mtd9_only.py                # 只截停 + 只读校验（DRY-RUN）
  python tools/flash_mtd9_only.py --go           # 截停 + 写 mtd9 + 回读校验 + reset 抓启动
  python tools/flash_mtd9_only.py --go --duration 1800

断电失败回滚：python tools/flash_from_sd.py 不适用；用 backup/snapshot 全片
在 U-Boot 下 `mtd write spi-nand0 <full-system_b-orig> 0x03A80000` 回写，
或 SD 卡三参 bootm 临时启动。
"""
import argparse
import json
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "docs" / "evidence"
EVID.mkdir(parents=True, exist_ok=True)

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="COM4")
ap.add_argument("--baud", type=int, default=1500000)
ap.add_argument("--duration", type=float, default=1800.0, help="等上电最长秒数")
ap.add_argument("--go", action="store_true", help="真正执行 mtd erase/write（默认不写）")
args = ap.parse_args()

TS = datetime.now().strftime("%Y%m%d-%H%M%S")
START_ISO = datetime.now().isoformat(timespec="seconds")
LOG = EVID / ("flash-sdup-" + TS + ".log")
STATUS = EVID / "flash-sdup.status.json"

IMG = ROOT / "upgrade" / "s4602_upgrade" / "flash" / "system_b.img"
IMG_SIZE = IMG.stat().st_size
IMG_CRC = f"{zlib.crc32(IMG.read_bytes()) & 0xffffffff:08x}"
M9_OFF = "0x03A80000"
M9_LEN = "0x02C00000"          # 44MB = mtd9 整块
LOAD = "0x1000000"
RDCK = "0x2000000"

# 只读校验：SD 里有镜像 + 能加载 + 源 CRC 对
VERIFY = [
    ("fatls mmc 1", 25, [f"{IMG_SIZE:>7}"] if False else [str(IMG_SIZE)]),
    (f"fatload mmc 1 {LOAD} system_b.img", 40, [f"{IMG_SIZE} bytes read"]),
    (f"crc32 {LOAD} {M9_LEN}", 40, [IMG_CRC]),
]
# 写阶段：只 mtd9
WRITE = [
    (f"mtd erase spi-nand0 {M9_OFF} {M9_LEN}", 240, []),
    (f"mtd write spi-nand0 {LOAD} {M9_OFF} {M9_LEN}", 240, []),
    (f"mtd read  spi-nand0 {RDCK} {M9_OFF} {M9_LEN}", 240, []),
    (f"crc32 {RDCK} {M9_LEN}", 40, [IMG_CRC]),
]

BOOT_KEYS = ("UBI: attached", "ubi0: attached", "UBIFS: mounted",
            "VFS: Mounted root", "Kernel command line", "Kernel panic",
            "Unable to mount root", "ubi: attaching mtd", "Welcome", "login:")

S = {"phase": "INIT", "step": "", "bytes": 0, "checks": [], "boots": [],
     "result": "", "note": "", "img": {"size": IMG_SIZE, "crc": IMG_CRC}}


def status(**kw):
    d = dict(S)
    d.update({"started_at": START_ISO, "updated_at": datetime.now().isoformat(timespec="seconds"),
              "go": args.go, "log": str(LOG), "img": S["img"]})
    d.update(kw)
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS)


def phase(p, note=""):
    S["phase"] = p
    S["note"] = note
    status()
    print(f"[sdup] === {p} {note} ===", flush=True)


try:
    ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1,
                        timeout=0.02, write_timeout=1.0, xonxoff=False, rtscts=False, dsrdtr=False)
except Exception as e:
    phase("PORT_ERROR", repr(e))
    S["result"] = "cannot open " + args.port
    status(); sys.exit(3)

ser.reset_input_buffer()
logf = LOG.open("wb")


def rx(b):
    if b:
        logf.write(b); logf.flush(); S["bytes"] += len(b)


def run(cmd, timeout):
    S["step"] = cmd
    status()
    print(f"[sdup] $ {cmd}", flush=True)
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    buf = bytearray(); t0 = time.time()
    while time.time() - t0 < timeout:
        b = ser.read(4096)
        if b:
            buf.extend(b); rx(b)
            if b"=>" in bytes(buf[-200:]) and time.time() - t0 > 0.3:
                break
        else:
            time.sleep(0.02)
    return buf.decode("utf-8", errors="replace"), time.time() - t0


def check(cmd, timeout, expects):
    text, secs = run(cmd, timeout)
    low = text.lower(); ok = True; reasons = []
    for e in expects:
        if e.lower() not in low:
            ok = False; reasons.append("missing: " + e)
    if "=>" not in text:
        ok = False; reasons.append("no U-Boot prompt")
    for bad in ("failed", "usage:", "error"):
        if bad in low:
            ok = False; reasons.append("error word: " + bad)
    S["checks"].append({"cmd": cmd, "ok": ok, "secs": round(secs, 2), "reasons": reasons, "out": text[-300:]})
    print(f"[sdup]    -> {'OK' if ok else 'FAIL'} ({secs:.2f}s) {reasons}", flush=True)
    status()
    return ok


def abort(why):
    S["phase"] = "ABORT"; S["result"] = why
    S["note"] = "已中止。若已写 mtd9，请用 pristine system_b.img 回写：U-Boot 下 fatload+ mtd write spi-nand0 <addr> 0x03A80000 0x02C00000"
    status(); print("[sdup] ABORT:", why, flush=True)


# ---------------------------------------------------------------- 1. 截停（必须见到新一轮 SPL/DDR 横幅）
phase("INTERCEPT", "waiting for a FRESH power cycle (SPL/DDR banner)")
ser.write(b"\r"); time.sleep(0.3)
pre = ser.read(4096); rx(pre)
banner_seen = any(k in pre.decode("utf-8", "replace").lower()
                  for k in ("ddr version", "u-boot spl", "bootrom", "maskrom", "verified-boot"))
at_prompt = False; t0 = time.time(); last_send = 0.0; toggle = 0
boot_buf = bytearray()
if banner_seen:
    print("[sdup] boot banner already on wire", flush=True)
else:
    print(f"[sdup] armed for {args.duration:.0f}s; POWER-CYCLE the device now", flush=True)
while not at_prompt and time.time() - t0 < args.duration:
    b = ser.read(4096)
    if b:
        rx(b); boot_buf.extend(b)
        low = b.decode("utf-8", errors="replace").lower()
        if any(k in low for k in ("ddr version", "u-boot spl", "bootrom", "maskrom", "verified-boot")):
            if not banner_seen:
                banner_seen = True
                phase("CAPTURING", "boot banner seen, hammering stop keys")
        if banner_seen and b"=>" in b:
            at_prompt = True; break
    now = time.time()
    if now - last_send > 0.035:
        ser.write(b"\x03" if toggle % 3 == 0 else b" "); toggle += 1; last_send = now
    time.sleep(0.005)

if not at_prompt:
    logf.close(); ser.close()
    abort("TIMEOUT: 没等到 U-Boot 提示符，未执行任何校验或写操作")
    sys.exit(2)

time.sleep(0.3); ser.write(b"\r"); time.sleep(0.4); rx(ser.read(4096))
phase("INTERCEPTED", "stopped at U-Boot '=>', COM4 held by this script")

# ---------------------------------------------------------------- 2. 只读校验
phase("VERIFY", "read-only checks (SD size / fatload / source crc), no flash writes")
for cmd, to, exp in VERIFY:
    if not check(cmd, to, exp):
        logf.close(); ser.close()
        abort("只读校验未通过，未执行任何 mtd erase/write")
        sys.exit(4)

if not args.go:
    logf.close(); ser.close()
    S["phase"] = "DRY_RUN_OK"
    S["result"] = "截停 + 只读校验全部通过；未写 flash（加 --go 才刷）"
    status(); print("[sdup] DRY_RUN_OK, COM released", flush=True)
    sys.exit(0)

# ---------------------------------------------------------------- 3. 写 mtd9（仅此一个分区）
phase("WRITE", "writing mtd9 (system_b) ONLY; boot_b/U-Boot/kernel untouched")
for cmd, to, exp in WRITE:
    if not check(cmd, to, exp):
        logf.close(); ser.close()
        abort("mtd9 写/回读校验未通过，停止")
        sys.exit(5)
S["result"] = "mtd9(system_b) 写入完成，源/回读 crc32 双向一致"
phase("WRITTEN", S["result"])

# ---------------------------------------------------------------- 4. reset + 抓启动日志
phase("RESET", "reboot from flashed mtd9, capturing 90s boot log")
ser.write(b"reset\r")
boot = bytearray(); t0 = time.time()
while time.time() - t0 < 90:
    b = ser.read(4096)
    if b:
        boot.extend(b); rx(b)
    else:
        time.sleep(0.02)
text = boot.decode("utf-8", errors="replace")
for line in text.splitlines():
    if any(k.lower() in line.lower() for k in BOOT_KEYS):
        S["boots"].append(line.strip()[:220])

logf.close(); ser.close()
S["phase"] = "DONE"
S["note"] = "mtd9 已写；COM4 已释放。若 webd:8080 与 SSH:22 无守护恢复 = S99local.sh 钩子生效"
status()
print(f"[sdup] DONE bytes={S['bytes']}", flush=True)
for l in S["boots"][:40]:
    print("   ", l, flush=True)
sys.exit(0)
