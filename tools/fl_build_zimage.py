#!/usr/bin/env python3
"""Build zImage + dtb on 177 from the already-resolved .config, report sizes.

Assumes tools/fl_fix_kconfig.py already ran (fragment written, olddefconfig done).
Just `make zImage` + dtb, then print byte sizes vs boot_b 4MiB. Long-running.
"""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=1800, stdin=None):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    if stdin is not None:
        for i in range(0, len(stdin), 48 * 1024):
            s.write(stdin[i:i + 48 * 1024])
        s.flush()
    s.close()
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out, err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BUILD[0], BUILD[1], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)

    # NOTE: 177 has arm-linux-gnueabihf-gcc (Ubuntu 9.5.0, /usr/local/bin) in PATH.
    # Full zImage build needs CROSS_COMPILE set; kconfig-only paths did not, so
    # earlier `make zImage` rc=2 was "host gcc got ARM flags", not a trim problem.
    # 9.5 is NOT the GA toolchain (gcc-linaro 6.3.1), so treat this size as a
    # PROXIES estimate (same order of magnitude, a few KB off); the authoritative
    # number is the GA artifact (clean tree + injection + 6.3.1).
    cmd = (
        "export PATH=$HOME/.local/bin:$PATH; cd ~/fl-kernel && "
        "export HOSTLDFLAGS='-Wl,--allow-multiple-definition' && "
        "export CROSS_COMPILE=arm-linux-gnueabihf- && "
        "$CROSS_COMPILE-gcc --version | head -1 && "
        "echo '>>> make zImage' && make ARCH=arm -j$(nproc) zImage >/tmp/zimg.log 2>&1; echo zimg_rc=$?; "
        "echo '>>> make dtb' && make ARCH=arm rv1109-38-v10-spi-nand.dtb >/tmp/dtb.log 2>&1; echo dtb_rc=$?; "
        "echo '--- sizes ---'; "
        "ls -l arch/arm/boot/zImage arch/arm/boot/dts/rv1109-38-v10-spi-nand.dtb 2>&1 | awk '{print $5, $NF}'; "
        "python3 - <<'PY'\n"
        "import os\nz=os.path.getsize('arch/arm/boot/zImage'); d=os.path.getsize('arch/arm/boot/dts/rv1109-38-v10-spi-nand.dtb')\n"
        "BOOT_B=0x400000\n# zboot ≈ zImage + dtb + small FIT header\nprint('zImage=%d B  dtb=%d B'%(z,d))\n"
        "print('est zboot=%d B (%.2f MiB)'%(z+d+4096,(z+d+4096)/1048576.0))\n"
        "print('boot_b = %d B (%.1f MiB)'%(BOOT_B,BOOT_B/1048576.0))\n"
        "print('FITS boot_b:', z+d+4096 < BOOT_B, '  over-by:', z+d+4096-BOOT_B)\nPY"
    )
    print("building zImage + dtb on 177 (this can take a while)...")
    rc, o, e = run(c, cmd, timeout=3600)
    print(o)
    if e.strip():
        print("[stderr tail]", e[-800:])
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
