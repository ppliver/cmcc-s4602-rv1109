#!/usr/bin/env python3
"""Grab defconfig raw lines for trim candidates + confirm media/isp/mpp/flash
symbols that MUST survive. Read-only on 177 ~/fl-kernel.
"""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=120):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out, err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(BUILD[0], BUILD[1], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)

    cmds = [
        ("defconfig: display/audio/usb/debug/hdmi raw lines",
         "grep -nE '^(# )?CONFIG_(DRM|FB|SND|SOUND|USB|HDMI|DEBUG_KERNEL|VGA|CONSOLE_SELECT|FB_CMDLINE|DRM_FBDEV)' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
        ("defconfig: media/isp/mpp/flash that MUST survive",
         "grep -nE '^(# )?CONFIG_(MEDIA|VIDEO|UVC|MPP|MMLIB|RGA|RK_MPI|RK_VDEC|RK_VENC|RK_ISP|RK_ISPP|VI_|VPU|CSI|MIPI_CSI|CSI2|I2C|SPI|FLASH|SFC|UBI|UBIFS|SQUASHFS|SCSI|MMC|SD|NVME|SATA|ETHERNET|STMMAC|ETH|GMAC)' ~/fl-kernel/arch/arm/configs/rv1126_defconfig | head -80"),
        (".config: media/isp/mpp/v4l2 =y (must stay)",
         "grep -E '^CONFIG_(MEDIA|VIDEO_.*(ROCKCHIP|RK|ISC|MIPI|CSI|CAPTURE|BUF|V4L)|V4L|UVC|RK_(MPP|MMLIB|ISP|ISPP|VDEC|VENC)|VI_)' ~/fl-kernel/.config | grep '=y$' | sort"),
        (".config: is anything select-ing SND/DRM? show =y that mention audio/disp deps",
         "grep -nE '^CONFIG_(VIDEO_ROCKCHIP|MEDIA|SND_SOC_ROCKCHIP|DRM_ROCKCHIP)' ~/fl-kernel/.config | grep '=y$'"),
        ("who sets DEBUG_KERNEL / FB / HDMI in defconfig",
         "grep -nE 'CONFIG_(DEBUG_KERNEL|DEBUG_FS|FB|HDMI|USB|SND|DRM)' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd)
        sys.stdout.write(o if o.strip() else "(no match)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[:300] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
