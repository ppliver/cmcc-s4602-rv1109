import serial, time, sys

PORT = "COM4"
BAUD = 1500000

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    ser = serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                        timeout=1, xonxoff=False, rtscts=False, dsrdtr=False)
    ser.flushInput()
    if cmd:
        ser.write((cmd + "\r\n").encode("utf-8", errors="replace"))
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < dur:
        b = ser.read(4096)
        if b:
            buf.extend(b)
        else:
            time.sleep(0.05)
    ser.close()
    sys.stdout.write(buf.decode("utf-8", errors="replace"))

if __name__ == "__main__":
    main()
