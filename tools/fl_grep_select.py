#!/usr/bin/env python3
"""Find the top-level Rockchip WLAN driver option that selects WIRELESS/MAC80211/CFG80211
and which wireless symbols are =y in defconfig / current .config.
"""
import sys
import paramiko

BUILD = ("192.168.219.177", 22, "zhang", "admin888")


def run(c, cmd, timeout=120):
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
        ("drivers/net/wireless/Kconfig top (config name + select)",
         "sed -n '1,15p' ~/fl-kernel/drivers/net/wireless/Kconfig"),
        ("rockchip_wlan/Kconfig top (config name + select)",
         "sed -n '1,12p' ~/fl-kernel/drivers/net/wireless/rockchip_wlan/Kconfig"),
        ("rkwifi/Kconfig config names",
         "grep -nE '^config ' ~/fl-kernel/drivers/net/wireless/rockchip_wlan/rkwifi/Kconfig"),
        ("which wireless symbols =y in DEFCONFIG",
         "grep -nE '^CONFIG_(ROCKCHIP|RKWIFI|WIFI|WLAN|WIRELESS|MAC80211|CFG80211|BCMDHD|RTL_|CYWDHD|MT7|ATH|QCA|_WIFI|_WLAN)=y' ~/fl-kernel/arch/arm/configs/rv1126_defconfig"),
        ("which wireless symbols =y in current .config",
         "grep -nE '^CONFIG_(ROCKCHIP_WLAN|RKWIFI|WIFI|WLAN|WIRELESS|MAC80211|CFG80211|BCMDHD|RTL_WIRELESS_SOLUTION|CYWDHD|NET_WIRELESS)=y' ~/fl-kernel/.config"),
    ]
    for title, cmd in cmds:
        print("=== %s ===" % title)
        rc, o, e = run(c, cmd)
        sys.stdout.write(o if o.strip() else "(none)\n")
        if e.strip():
            sys.stdout.write("[stderr] " + e[-300:] + "\n")
        print()
    c.close()


if __name__ == "__main__":
    sys.exit(main())
