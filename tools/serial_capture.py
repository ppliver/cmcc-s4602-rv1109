import serial, time, sys

PORT = "COM4"          # 本机物理串口（Git Bash 里即 /dev/ttyS3）
BAUD = 1500000         # 相机 UART：1500000 8N1 / 3.3V / ttyFIQ0
OUT  = "/tmp/s4602_com4_boot.log"

ser = serial.Serial(
    PORT, BAUD,
    bytesize=8, parity="N", stopbits=1,
    timeout=1, xonxoff=False, rtscts=False, dsrdtr=False,
)
buf = bytearray()
t0 = time.time()

# Phase 1: 纯监听 25s，不写任何字节，避免打断正在进行的启动
ser.flushInput()
while time.time() - t0 < 25:
    b = ser.read(4096)
    if b:
        buf.extend(b)
    else:
        time.sleep(0.05)

# Phase 2: 若 25s 内完全无输出，发一个回车探测（shell/登录提示会回显）
if len(buf) == 0:
    ser.write(b"\r\n")
    while time.time() - t0 < 45:
        b = ser.read(4096)
        if b:
            buf.extend(b)
        else:
            time.sleep(0.05)

ser.close()
with open(OUT, "wb") as f:
    f.write(buf)

# 解码预览（非打印字符替换），便于直接阅读
text = buf.decode("utf-8", errors="replace")
lines = text.splitlines()
print(f"captured bytes: {len(buf)}  ->  {OUT}")
print(f"decoded lines : {len(lines)}")
print("=" * 72)
for ln in lines:
    print(ln[:200])
