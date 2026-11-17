#!/usr/bin/env python3
"""Confirm RNDIS host symbol + USB host-controller symbol names on 177 before
writing the fragment (wrong name = kconfig silently drops it)."""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=180):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out, e.read().decode("utf-8", "replace")


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(*BUILD[:2], BUILD[2], BUILD[3], timeout=20,
              look_for_keys=False, allow_agent=False)
    cmds = [
        ("RNDIS host symbol + type (drivers/net/usb)",
         "grep -nE '^config USB_NET_RNDIS_HOST' ~/fl-kernel/drivers/net/usb/Kconfig; "
         "echo '--- block ---'; sed -n '/^config USB_NET_RNDIS_HOST/,/^$/p' ~/fl-kernel/drivers/net/usb/Kconfig | head -12"),
        ("confirm USB_RNDIS does NOT exist as a top-level symbol",
         "grep -rnE '^config USB_RNDIS$' ~/fl-kernel/drivers ~/fl-kernel/net 2>/dev/null || echo 'USB_RNDIS absent (RNDIS host = USB_NET_RNDIS_HOST)')"),
        ("USB host controller exact symbols (defconfig lines we restore)",
         "grep -nE '^CONFIG_(USB|USB_XHCI_HCD|USB_EHCI_HCD|USB_EHCI_HCD_PLATFORM|USB_OHCI_HCD|USB_OHCI_HCD_PLATFORM|USB_STORAGE)=y' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
        ("USB_SERIAL_WWAN is auto-selected (need not set explicitly)",
         "grep -nE 'select USB_SERIAL_WWAN' ~/fl-kernel/drivers/usb/serial/Kconfig | head"),
        ("MBIM driver (newer 4G cards) symbol if present",
         "grep -nE '^config USB_IPHIGNUP_MODEM' ~/fl-kernel/drivers/net/usb/Kconfig; sed -n '/^config USB_IPHIGNUP_MODEM/,/^$/p' ~/fl-kernel/drivers/net/usb/Kconfig | head -8 || true"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd, timeout=180)
        sys.stdout.write(o if o.strip() else "(none)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[-200:] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
