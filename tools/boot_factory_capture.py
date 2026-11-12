#!/usr/bin/env python3
"""Reboot device, stop U-Boot, boot factory slot _a, capture kernel console.

Non-destructive: only sets android_slotsufix in RAM (no saveenv); a later
power-cycle reverts to the default slot (_b, our OpenIPC). Captures ~55s of
the factory boot console to --log so we can read which PHY driver it attaches.
"""
import argparse, serial, sys, time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=1500000)
    ap.add_argument("--log", default="docs/evidence/factory-boot.log")
    ap.add_argument("--capture", type=float, default=55.0)
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.15)
    time.sleep(0.4)
    cap = bytearray()
    at_prompt = False

    def drain(t):
        t0 = time.time()
        while time.time() - t0 < t:
            b = ser.read(4096)
            if b:
                cap.extend(b)
                if b"root@openipc" in b or b"=>" in b:
                    pass
            else:
                time.sleep(0.02)

    # 1) we expect to be at the OpenIPC shell; issue reboot
    ser.write(b"\r")
    drain(1.0)
    ser.write(b"reboot\r")
    print("[cap] reboot issued, waiting for U-Boot...", flush=True)

    # 2) hammer stop keys until we see the U-Boot prompt '=>'
    t0 = time.time()
    last = 0.0
    while time.time() - t0 < 25:
        b = ser.read(4096)
        if b:
            cap.extend(b)
            if b"=>" in b:
                at_prompt = True
                print("[cap] U-Boot prompt seen", flush=True)
                break
        else:
            now = time.time()
            if now - last > 0.05:
                ser.write(b"\x03")          # Ctrl-C
                ser.write(b" ")             # space
                last = now
            time.sleep(0.02)
    if not at_prompt:
        print("[cap] WARN: never reached U-Boot prompt; dumping what we have", flush=True)
        Path(args.log).write_bytes(cap)
        return

    # 3) select factory slot _a and boot it
    time.sleep(0.3)
    ser.write(b"setenv android_slotsufix _a\r")
    drain(1.0)
    ser.write(b"boot_fit\r")
    print("[cap] booting factory slot _a, capturing", args.capture, "s...", flush=True)

    # 4) capture factory kernel console
    t0 = time.time()
    while time.time() - t0 < args.capture:
        b = ser.read(4096)
        if b:
            cap.extend(b)
        else:
            time.sleep(0.02)

    ser.close()
    Path(args.log).write_bytes(cap)
    print("OK wrote", args.log, len(cap), "bytes", flush=True)


if __name__ == "__main__":
    main()
