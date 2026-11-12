#!/usr/bin/env python3
"""S4602 一次上电完成刷写（2026-11-12）。

流程：等上电 -> 截停 U-Boot -> 只读校验 -> 刷 _b 槽 -> 回读 CRC -> reset 抓启动日志。

安全设计：
  * 默认 --dry-run：只截停 + 只读校验，绝不写 flash；显式 --go 才进入写阶段。
  * 写阶段前的 5 项只读校验（fatls 文件大小 / fatload 字节数 / 源 CRC / FIT magic）
    任何一项不过就 ABORT，绝不执行任何 mtd erase/write。
  * 严格按 uboot/02-flash.generated.txt 逐条下发，禁止整段粘贴（历史变砖原因）。
  * 每条 mtd write 四参数齐全；写后 mtd read + crc32 双向比对，CRC 不符即 ABORT。
  * 只刷 _b（boot_b 0x00A00000 / system_b 0x03A80000），_a 原厂保留作回滚。
  * 全程原始字节日志 + 原子状态文件，任何异常都释放 COM4。

用法：
  python tools/flash_from_sd.py                 # 只截停 + 只读校验
  python tools/flash_from_sd.py --go           # 截停 + 完整刷写 + 重启验证
  python tools/flash_from_sd.py --go --duration 1800
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
LOG = EVID / ("live-flash-" + TS + ".log")
STATUS = EVID / "flash.status.json"

ZBOOT = ROOT / "firmware" / "zboot.img.s4602"
ROOTFS = ROOT / "firmware" / "rootfs.ubi.rv1109"
ZBOOT_SIZE = ZBOOT.stat().st_size
ROOTFS_SIZE = ROOTFS.stat().st_size
ZBOOT_CRC = f"{zlib.crc32(ZBOOT.read_bytes()) & 0xffffffff:08x}"
ROOTFS_CRC = f"{zlib.crc32(ROOTFS.read_bytes()) & 0xffffffff:08x}"
ZBOOT_WRITE = ((ZBOOT_SIZE + 0x7FF) // 0x800) * 0x800

# 与 uboot/02-flash.generated.txt 逐条一致
VERIFY = [
    ("fatls mmc 1", 20, [str(ZBOOT_SIZE), str(ROOTFS_SIZE)]),
    (f"fatload mmc 1 0x1000000 zboot.img.s4602", 30, [f"{ZBOOT_SIZE} bytes read"]),
    (f"crc32 0x1000000 0x{ZBOOT_SIZE:X}", 20, [ZBOOT_CRC]),
    ("fdt addr 0x1000000", 20, []),
    ("fdt header", 20, ["0xd00dfeed"]),
]
WRITE = [
    ("fatload mmc 1 0x1000000 rootfs.ubi.rv1109", 30, [f"{ROOTFS_SIZE} bytes read"]),
    (f"crc32 0x1000000 0x{ROOTFS_SIZE:X}", 20, [ROOTFS_CRC]),
    ("mtd erase spi-nand0 0x03A80000 0x02c00000", 180, []),
    ("mtd write spi-nand0 0x1000000 0x03A80000 0x780000", 180, []),
    ("mtd read  spi-nand0 0x2000000 0x03A80000 0x780000", 180, []),
    ("crc32 0x2000000 0x780000", 20, [ROOTFS_CRC]),
    (f"mw.b 0x1000000 0xFF 0x{ZBOOT_WRITE:X}", 60, []),
    ("fatload mmc 1 0x1000000 zboot.img.s4602", 30, [f"{ZBOOT_SIZE} bytes read"]),
    (f"crc32 0x1000000 0x{ZBOOT_SIZE:X}", 20, [ZBOOT_CRC]),
    ("mtd erase spi-nand0 0x00A00000 0x00400000", 180, []),
    (f"mtd write spi-nand0 0x1000000 0x00A00000 0x{ZBOOT_WRITE:X}", 180, []),
    (f"mtd read  spi-nand0 0x2000000 0x00A00000 0x{ZBOOT_WRITE:X}", 180, []),
    (f"crc32 0x2000000 0x{ZBOOT_SIZE:X}", 20, [ZBOOT_CRC]),
]

BOOT_KEYS = ("UBIFS: mounted UBI device", "ubi0: attached", "VFS: Mounted root",
             "Kernel command line", "Kernel panic", "Unable to mount root",
             "Detected SC", "sc5338", "Welcome", "login:", "OpenIPC", "majestic")

S = {"phase": "INIT", "step": "", "bytes": 0, "checks": [], "boots": [],
     "result": "", "note": ""}


def status(**kw):
    d = {
        "phase": S["phase"], "step": S["step"], "bytes": S["bytes"],
        "started_at": START_ISO,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "go": args.go, "log": str(LOG),
        "zboot": {"size": ZBOOT_SIZE, "crc": ZBOOT_CRC,
                  "aligned_write": ZBOOT_WRITE},
        "rootfs": {"size": ROOTFS_SIZE, "crc": ROOTFS_CRC},
        "checks": S["checks"], "boot_keys": S["boots"],
        "result": S["result"], "note": S["note"],
    }
    d.update(kw)
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS)


def phase(p, note=""):
    S["phase"] = p
    S["note"] = note
    status()
    print(f"[flash] === {p} {note} ===", flush=True)


try:
    ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1,
                        timeout=0.02, write_timeout=1.0, xonxoff=False,
                        rtscts=False, dsrdtr=False)
except Exception as e:
    phase("PORT_ERROR", repr(e))
    S["result"] = "cannot open " + args.port
    status()
    sys.exit(3)

ser.reset_input_buffer()
logf = LOG.open("wb")


def rx(b):
    if b:
        logf.write(b)
        logf.flush()
        S["bytes"] += len(b)


def run(cmd, timeout, wait_prompt=True):
    """发一条命令，收完输出；返回文本。"""
    S["step"] = cmd
    status()
    print(f"[flash] $ {cmd}", flush=True)
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = ser.read(4096)
        if b:
            buf.extend(b)
            rx(b)
        if wait_prompt and b"=>" in bytes(buf[-200:]) and time.time() - t0 > 0.3:
            break
        if not b:
            time.sleep(0.02)
    return buf.decode("utf-8", errors="replace"), time.time() - t0


def check(cmd, timeout, expects):
    text, secs = run(cmd, timeout)
    low = text.lower()
    ok = True
    reasons = []
    for e in expects:
        if e.lower() not in low:
            ok = False
            reasons.append("missing: " + e)
    if "=>" not in text:
        ok = False
        reasons.append("no U-Boot prompt (command did not finish)")
    for bad in ("failed", "usage:"):
        if bad in low:
            ok = False
            reasons.append("error word: " + bad)
    S["checks"].append({"cmd": cmd, "ok": ok, "secs": round(secs, 2),
                        "reasons": reasons, "out": text[-400:]})
    print(f"[flash]    -> {'OK' if ok else 'FAIL'} ({secs:.2f}s) {reasons}", flush=True)
    status()
    return ok


def abort(why):
    S["phase"] = "ABORT"
    S["result"] = why
    S["note"] = ("已中止。若已执行过部分写命令，请勿反复试刷；"
                 "用 backup/snapshot/full-00..07.bin 在 U-Boot 下回写 boot_b/system_b 恢复原厂")
    status()
    print("[flash] ABORT:", why, flush=True)


# ---------------------------------------------------------------- 1. 截停
# 安全：必须看到「新一轮上电」的 SPL/DDR 横幅后才认可 '=>'，
# 避免把上一次会话残留的 '=>' 直接拿来刷写（违背「一次上电」流程）。
phase("INTERCEPT", "waiting for a FRESH power cycle (SPL/DDR banner)")
ser.write(b"\r")
time.sleep(0.3)
pre = ser.read(4096)
rx(pre)
banner_seen = any(k in pre.decode("utf-8", "replace").lower()
                  for k in ("ddr version", "u-boot spl", "bootrom",
                            "maskrom", "verified-boot"))
at_prompt = False   # 只在 banner_seen 之后见到 '=>' 才算截停成功
t0 = time.time()
last_send = 0.0
toggle = 0
boot_buf = bytearray()

if banner_seen:
    print("[flash] boot banner already on wire; continuing capture", flush=True)
else:
    print(f"[flash] armed for {args.duration:.0f}s; power-cycle the device now", flush=True)

while not at_prompt and time.time() - t0 < args.duration:
    b = ser.read(4096)
    if b:
        rx(b)
        boot_buf.extend(b)
        low = b.decode("utf-8", errors="replace").lower()
        if any(k in low for k in ("ddr version", "u-boot spl", "bootrom",
                                  "maskrom", "verified-boot")):
            if not banner_seen:
                banner_seen = True
                phase("CAPTURING", "boot banner seen, hammering stop keys")
        if banner_seen and b"=>" in b:
            at_prompt = True
            break
    now = time.time()
    if now - last_send > 0.035:
        ser.write(b"\x03" if toggle % 3 == 0 else b" ")
        toggle += 1
        last_send = now
    time.sleep(0.005)

if not at_prompt:
    logf.close()
    ser.close()
    abort("TIMEOUT: 没等到 U-Boot 提示符，未执行任何校验或写操作")
    sys.exit(2)

time.sleep(0.3)
ser.write(b"\r")
time.sleep(0.4)
rx(ser.read(4096))
phase("INTERCEPTED", "stopped at U-Boot '=>', COM4 held by this script")

# ---------------------------------------------------------------- 2. 只读校验
phase("VERIFY", "read-only checks, no flash writes")
for cmd, to, exp in VERIFY:
    if not check(cmd, to, exp):
        logf.close()
        ser.close()
        abort("只读校验未通过，未执行任何 mtd erase/write")
        sys.exit(4)

if not args.go:
    logf.close()
    ser.close()
    S["phase"] = "DRY_RUN_OK"
    S["result"] = "截停 + 只读校验全部通过；未写 flash（加 --go 才刷）"
    status()
    print("[flash] DRY_RUN_OK, COM released", flush=True)
    sys.exit(0)

# ---------------------------------------------------------------- 3. 写 _b 槽
phase("WRITE", "writing system_b + boot_b (slot _b only)")
for cmd, to, exp in WRITE:
    if not check(cmd, to, exp):
        logf.close()
        ser.close()
        abort("刷写命令/回读 CRC 校验未通过，停止后续写命令")
        sys.exit(5)

S["result"] = "system_b + boot_b 写入完成，源/回读 CRC 双向一致"
phase("WRITTEN", S["result"])

# ---------------------------------------------------------------- 4. reset + 抓启动日志
phase("RESET", "reboot from flashed _b, capturing 70s boot log")
ser.write(b"reset\r")
boot = bytearray()
t0 = time.time()
while time.time() - t0 < 70:
    b = ser.read(4096)
    if b:
        boot.extend(b)
        rx(b)
    else:
        time.sleep(0.02)
text = boot.decode("utf-8", errors="replace")
for line in text.splitlines():
    if any(k.lower() in line.lower() for k in BOOT_KEYS):
        S["boots"].append(line.strip()[:220])

logf.close()
ser.close()
S["phase"] = "DONE"
S["note"] = "flash 写入已完成；COM4 已释放。启动日志见 live-flash 日志尾部"
status()
print(f"[flash] DONE bytes={S['bytes']}", flush=True)
for l in S["boots"][:40]:
    print("   ", l, flush=True)
sys.exit(0)
