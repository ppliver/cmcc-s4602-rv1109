#!/usr/bin/env python3
"""Probe USB + 4G-USB-network-card driver state on 177 rv1126_defconfig.

Goal: user needs a USB port for a 4G wireless card in RNDIS mode.
- Confirm which USB host controllers defconfig keeps (XHCI/EHCI/OHCI).
- Confirm exact Kconfig symbol names for USB network drivers
  (USB_USBNET / USB_RNDIS / USB_NET_CDC_NCM / USB_NET_CDCETHER / USB_SERIAL_QUALCOMM).
- See which are ON vs absent in defconfig, so the fragment restores USB
  AND adds the missing 4G-card drivers.
"""
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
        ("defconfig: ALL USB* lines",
         "grep -nE '^CONFIG_USB' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
        ("defconfig: USB network/serial drivers (4G card needs)",
         "grep -nE '^(CONFIG_USB_USBNET|CONFIG_USB_RNDIS|CONFIG_USB_NET_|CONFIG_USB_SERIAL)' "
         "~/fl-kernel/arch/arm/configs/rv1126_defconfig; echo '(absent = default off)'"),
        ("Kconfig: exact symbol names in drivers/net/usb",
         "grep -nE '^config (USB_USBNET|USB_RNDIS|USB_NET_AX88179_178|USB_NET_CDCETHER|USB_NET_CDC_NCM|USB_NET_LAN78XX|USB_RTL8152|USB_NET_PEGASUS)' "
         "~/fl-kernel/drivers/net/usb/Kconfig"),
        ("Kconfig: USB serial symbols",
         "grep -nE '^config (USB_SERIAL|USB_SERIAL_QUALCOMM|USB_SERIAL_FTDI_SIO|USB_SERIAL_WWAN)' "
         "~/fl-kernel/drivers/usb/serial/Kconfig"),
        ("confirm USB_USBNET is the umbrella (selects / depends)",
         "sed -n '/^config USB_USBNET/,/^$/p' ~/fl-kernel/drivers/net/usb/Kconfig | head -25"),
        ("current .config: USB state after round3 fragment (expect USB off)",
         "grep -nE '^CONFIG_(USB|USB_XHCI_HCD|USB_EHCI_HCD|USB_OHCI_HCD|USB_USBNET|USB_RNDIS)=y' ~/fl-kernel/.config || echo '(none = USB currently off)')"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd, timeout=180)
        sys.stdout.write(o if o.strip() else "(none)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[-300:] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
