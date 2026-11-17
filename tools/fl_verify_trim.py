#!/usr/bin/env python3
"""Verify the S4602 slim fragment resolves cleanly on 177 (kconfig-only, fast).

Does NOT build zImage. Checks:
  - merge_config(rv1126_defconfig + fragment) + olddefconfig succeeds (rc=0)
  - trim groups are OFF: DRM, DRM_ROCKCHIP, SND, SOUND, USB (+ cascaded)
  - capture chain stays ON: MEDIA_SUPPORT, VIDEO_ROCKCHIP_CIF/ISP/ISPP,
    STMMAC, UBI, UBIFS, SQUASHFS, I2C, SPI, and injected SC5338
  - no "value assigned to symbol X not found" in olddefconfig stderr
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
]
ON = [
    "CONFIG_MEDIA_SUPPORT", "CONFIG_VIDEO_ROCKCHIP_CIF",
    "CONFIG_VIDEO_ROCKCHIP_ISP", "CONFIG_VIDEO_ROCKCHIP_ISPP",
    "CONFIG_VIDEO_SC5338", "CONFIG_STMMAC_ETH", "CONFIG_MOTORCOMM_PHY",
    "CONFIG_MTD_UBI", "CONFIG_UBIFS_FS", "CONFIG_SQUASHFS",
    "CONFIG_I2C_RK3X", "CONFIG_SPI_ROCKCHIP", "CONFIG_CMDLINE_FORCE",
]


def run(c, cmd, timeout=300, stdin=None):
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
    print("uploading %s (%d bytes)" % (os.path.basename(FRAG_PATH), len(frag)))

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BUILD[0], BUILD[1], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)

    # drop the FULL repo fragment into the tree (idempotent, matches what GA uses)
    rc, o, e = run(c,
                   "rm -f ~/fl-kernel/arch/arm/configs/s4602_fragment.firefly.config && "
                   "cat > ~/fl-kernel/arch/arm/configs/s4602_fragment.firefly.config",
                   stdin=frag)
    print("write fragment rc=%d" % rc)
    rc, o, e = run(c, "wc -l ~/fl-kernel/arch/arm/configs/s4602_fragment.firefly.config")
    print("fragment lines on 177:", o.strip())

    # kconfig-only: defconfig + merge(full fragment) + olddefconfig
    build = (
        "cd ~/fl-kernel && export HOSTLDFLAGS='-Wl,--allow-multiple-definition' && "
        "make ARCH=arm rv1126_defconfig >/tmp/d.log 2>&1; echo defconfig_rc=$?; "
        "MC=scripts/kconfig/merge_config.sh; [ -f $MC ] || MC=scripts/merge_config.sh; "
        "bash $MC -m .config arch/arm/configs/s4602_fragment.firefly.config >/tmp/m.log 2>&1; echo merge_rc=$?; "
        "make ARCH=arm olddefconfig >/tmp/old.log 2>&1; echo old_rc=$?"
    )
    rc, o, e = run(c, build, timeout=300)
    print(o)
    if e.strip():
        print("[stderr] " + e[:400])

    # any unresolved-symbol complaint?
    rc, o, e = run(c, "grep -iE 'not found|value assigned' ~/fl-kernel/.config 2>/dev/null; "
                     "grep -iE 'not found|value assigned' /tmp/old.log 2>/dev/null | head")
    if o.strip():
        print("=== olddefconfig complaints ===\n" + o)
    else:
        print("=== olddefconfig: no unresolved-symbol complaints ===")

    # report OFF/ON resolution
    print("=== trim groups (expect =n / absent) ===")
    for sym in OFF:
        r2, o2, _ = run(c, "grep -E '^%s=' ~/fl-kernel/.config" % sym)
        print("  %-28s %s" % (sym, (o2.strip() if o2.strip() else "absent")))
    print("=== capture chain (expect =y) ===")
    for sym in ON:
        r2, o2, _ = run(c, "grep -E '^%s=' ~/fl-kernel/.config" % sym)
        mark = "OK " if o2.strip().endswith("=y") else "MISS"
        print("  %-28s %s  %s" % (sym, mark, o2.strip() if o2.strip() else "absent"))

    c.close()


if __name__ == "__main__":
    sys.exit(main())
