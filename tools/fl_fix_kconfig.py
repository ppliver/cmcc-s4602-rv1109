#!/usr/bin/env python3
"""Diagnose + fix kconfig on 177 (host tools not generated on clean clone).

rc=2 from `make rv1126_defconfig` / `olddefconfig` means the kconfig host
tools (scripts/kconfig/conf) weren't built. Fix: python symlink + make hostprogs,
then redo defconfig+merge+olddefconfig and verify trim/capture resolution.
"""
import os
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAG_PATH = os.path.join(ROOT, "board", "s4602", "firefly", "s4602_fragment.firefly.config")

OFF = [
    "CONFIG_DRM", "CONFIG_DRM_ROCKCHIP", "CONFIG_DRM_KMS_HELPER",
    "CONFIG_SND", "CONFIG_SND_SOC", "CONFIG_SND_SOC_ROCKCHIP",
    "CONFIG_SOUND", "CONFIG_USB", "CONFIG_USB_XHCI_HCD",
    "CONFIG_USB_EHCI_HCD", "CONFIG_USB_OHCI_HCD", "CONFIG_USB_DWC3",
    "CONFIG_USB_GADGET", "CONFIG_USB_CONFIGFS", "CONFIG_FB",
    # round 2: wireless / local block / input (incl. select-sources WLAN/WL_ROCKCHIP)
    "CONFIG_WLAN", "CONFIG_WL_ROCKCHIP", "CONFIG_AP6XXX", "CONFIG_WIRELESS",
    "CONFIG_MAC80211", "CONFIG_CFG80211", "CONFIG_BT",
    "CONFIG_BCMDHD", "CONFIG_RTL_WIRELESS_SOLUTION", "CONFIG_BCMA",
    "CONFIG_SCSI", "CONFIG_MMC",
    "CONFIG_INPUT_KEYBOARD", "CONFIG_INPUT_TOUCHSCREEN",
    "CONFIG_INPUT_MOUSE", "CONFIG_INPUT_LEDS", "CONFIG_INPUT_MISC",
    "CONFIG_INPUT_POLLDEV", "CONFIG_HID",
]
ON = [
    "CONFIG_MEDIA_SUPPORT", "CONFIG_VIDEO_ROCKCHIP_CIF",
    "CONFIG_VIDEO_ROCKCHIP_ISP", "CONFIG_VIDEO_ROCKCHIP_ISPP",
    "CONFIG_VIDEO_SC5338", "CONFIG_STMMAC_ETH", "CONFIG_MOTORCOMM_PHY",
    "CONFIG_MTD_UBI", "CONFIG_UBIFS_FS", "CONFIG_SQUASHFS",
    "CONFIG_I2C_RK3X", "CONFIG_SPI_ROCKCHIP", "CONFIG_CMDLINE_FORCE",
]


def run(c, cmd, timeout=600, stdin=None):
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
    with open(FRAG_PATH, "r", encoding="utf-8") as f:
        frag = f.read()

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BUILD[0], BUILD[1], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)

    print("=== diagnose rc=2 (tail of kconfig logs) ===")
    rc, o, e = run(c, "for f in /tmp/d.log /tmp/m.log /tmp/old.log; do echo \"-- $f --\"; tail -5 $f 2>/dev/null; done")
    print(o)

    print("=== fix env: python symlink + hostprogs + fragment ===")
    fix = (
        "ln -sf $(which python3) ~/.local/bin/python 2>/dev/null; "
        "export PATH=$HOME/.local/bin:$PATH; "
        "cd ~/fl-kernel && export HOSTLDFLAGS='-Wl,--allow-multiple-definition' && "
        "make ARCH=arm hostprogs >/tmp/hostprogs.log 2>&1; echo hostprogs_rc=$?; "
        "tail -3 /tmp/hostprogs.log"
    )
    rc, o, e = run(c, fix, timeout=300)
    print(o, e[:400] if e else "")

    # write fragment
    rc, o, e = run(c,
                   "rm -f ~/fl-kernel/arch/arm/configs/s4602_fragment.firefly.config && "
                   "cat > ~/fl-kernel/arch/arm/configs/s4602_fragment.firefly.config",
                   stdin=frag)
    print("write fragment rc=%d" % rc)

    print("=== kconfig: defconfig + merge(full fragment) + olddefconfig ===")
    build = (
        "export PATH=$HOME/.local/bin:$PATH; cd ~/fl-kernel && "
        "export HOSTLDFLAGS='-Wl,--allow-multiple-definition' && "
        "make ARCH=arm rv1126_defconfig >/tmp/d.log 2>&1; echo defconfig_rc=$?; "
        "MC=scripts/kconfig/merge_config.sh; [ -f $MC ] || MC=scripts/merge_config.sh; "
        "bash $MC -m .config arch/arm/configs/s4602_fragment.firefly.config >/tmp/m.log 2>&1; echo merge_rc=$?; "
        "make ARCH=arm olddefconfig >/tmp/old.log 2>&1; echo old_rc=$?; "
        "grep -iE 'not found|value assigned' /tmp/old.log | head"
    )
    rc, o, e = run(c, build, timeout=300)
    print(o, e[:400] if e else "")

    print("=== trim groups (expect =n / absent after olddefconfig) ===")
    for sym in OFF:
        r2, o2, _ = run(c, "grep -E '^%s=' ~/fl-kernel/.config" % sym)
        v = o2.strip() if o2.strip() else "absent"
        mark = "OK(off)" if v.endswith("=") and not v.endswith("=y") and v.startswith("CONFIG") else ("absent=ok" if v == "absent" else "STILL_ON!!")
        print("  %-28s %-10s %s" % (sym, mark, v))
    print("=== capture chain (expect =y) ===")
    allon = True
    for sym in ON:
        r2, o2, _ = run(c, "grep -E '^%s=' ~/fl-kernel/.config" % sym)
        v = o2.strip() if o2.strip() else "absent"
        ok = v.endswith("=y")
        if not ok:
            allon = False
        print("  %-28s %-6s %s" % (sym, "OK" if ok else "MISS!!", v))
    print("CAPTURE_ALL_ON =", allon)
    c.close()


if __name__ == "__main__":
    sys.exit(main())
