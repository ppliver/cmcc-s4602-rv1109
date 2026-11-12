import serial, time, sys

PORT = "COM4"
BAUD = 1500000

def open_port():
    return serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                         timeout=1, xonxoff=False, rtscts=False, dsrdtr=False)

def attempt(ser, user, pwd, wait=4):
    ser.flushInput()
    ser.write((user + "\r\n").encode())
    time.sleep(1.2)
    ser.write((pwd + "\r\n").encode())
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < wait:
        b = ser.read(4096)
        if b:
            buf.extend(b)
        else:
            time.sleep(0.05)
    return buf.decode("utf-8", errors="replace")

ser = open_port()
# 清掉上一轮遗留的 "Password:" 提示（发个空密码让它回到 login:）
ser.write(b"\r\n")
time.sleep(1.0)

combos = [("root", ""), ("root", "root"), ("admin", "admin"), ("root", "password")]
got_shell = False
for u, p in combos:
    out = attempt(ser, u, p, 4)
    print(f"===== try user={u!r} pass={p!r} =====")
    for ln in out.splitlines()[-14:]:
        print(ln[:160])
    low = out.lower()
    if "login incorrect" not in low and ("# " in out or "$ " in out or "root@" in out
                                         or "busybox" in low or "welcome" in low):
        print(">>> SHELL LIKELY OBTAINED with", (u, p))
        got_shell = True
        break
    if "login incorrect" in low:
        print("  (login incorrect, retry)")
ser.close()
print("RESULT:", "shell" if got_shell else "no-shell-yet")
