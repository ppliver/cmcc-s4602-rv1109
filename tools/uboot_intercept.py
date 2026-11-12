import serial, time, sys

PORT = "COM4"
BAUD = 1500000
OUT  = "/tmp/s4602_uboot_boot.log"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 160.0

ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                    timeout=0.15, xonxoff=False, rtscts=False, dsrdtr=False)
ser.flushInput()
buf = bytearray()
interrupted = False
t0 = time.time()
last_spam = 0.0
got_prompt = False

print(f"[intercept] listening on {PORT} for {DURATION:.0f}s; power-cycle the device now", flush=True)
try:
    while time.time() - t0 < DURATION:
        b = ser.read(4096)
        if b:
            buf.extend(b)
            low = buf[-400:].decode("utf-8", errors="replace").lower()
            # 命中 autoboot 倒计时 -> 发 Ctrl-C 截停一次
            if not interrupted and ("stop autoboot" in low or "hit key" in low or "autoboot" in low):
                ser.write(b"\x03")
                interrupted = True
                print("[intercept] autoboot stop key sent (CTRL+C)", flush=True)
            # 看到 U-Boot 提示符
            if "=>" in low:
                got_prompt = True
        # 前 10s 兜底：每 0.4s 发一个空格，确保即使漏检也能截停
        now = time.time() - t0
        if not interrupted and now < 10.0 and now - last_spam > 0.4:
            ser.write(b" ")
            last_spam = now
        # 已进入 => 且已稳定 2s，提前结束释放串口
        if got_prompt and time.time() - t0 > 3.0:
            ser.write(b"\r\n")
            time.sleep(0.5)
            b2 = ser.read(4096)
            if b2:
                buf.extend(b2)
            break
except KeyboardInterrupt:
    pass
finally:
    ser.close()

with open(OUT, "wb") as f:
    f.write(buf)

text = buf.decode("utf-8", errors="replace")
lines = text.splitlines()
print(f"[intercept] done. bytes={len(buf)} lines={len(lines)} prompt_seen={got_prompt}")
print(f"[intercept] saved -> {OUT}")
# 打印关键行
for ln in lines:
    l = ln.lower()
    if any(k in l for k in ("=>", "u-boot", "autoboot", "spi-nand", "ddr", "spl", "boot", "hit key", "stop")):
        print("  >>", ln[:160])
