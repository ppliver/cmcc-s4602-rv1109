#!/bin/sh
# /etc/init.d/S99local.sh v2 -- SD-upgrade bootstrap + bootfix launcher.
# Shipped inside the read-only system_b squashfs (hook image v2, 2026-11-18).
#
# rcS sources /etc/init.d/S??* inside a ( subshell ): fire bootfix.sh detached.
# NOTE: this busybox has NO nohup applet -> plain "&" + init reparent is correct.
#
# v2 addition: on a STOCK device /data/www is empty, so bootstrap bootfix.sh
# (and only bootfix.sh - the rest is synced by its stage 0) from the SD card
# upgrade payload. After the first install, upgrades are pure SD swaps.

echo "$(date) S99local fired" >> /data/www/bootfix.log 2>/dev/null

if [ ! -f /data/www/bootfix.sh ]; then
    UPD=""
    for d in /tmp/udisk0 /mnt/mmcblk1 /tmp/sdup; do
        [ -f "$d/s4602_upgrade/base/bootfix.sh" ] && UPD="$d/s4602_upgrade" && break
    done
    if [ -z "$UPD" ]; then
        mkdir -p /tmp/sdup
        mount -t vfat /dev/mmcblk1p1 /tmp/sdup 2>/dev/null
        [ -f /tmp/sdup/s4602_upgrade/base/bootfix.sh ] && UPD=/tmp/sdup/s4602_upgrade
    fi
    if [ -n "$UPD" ]; then
        mkdir -p /data/www
        cp -f "$UPD/base/bootfix.sh" /data/www/bootfix.sh 2>/dev/null
        chmod +x /data/www/bootfix.sh 2>/dev/null
        echo "$(date) S99local: bootfix.sh bootstrapped from $UPD" >> /data/www/bootfix.log 2>/dev/null
    fi
fi

if [ -f /data/www/bootfix.sh ]; then
    sh /data/www/bootfix.sh >/dev/null 2>&1 &
fi
exit 0
