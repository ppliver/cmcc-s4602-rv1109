# -*- coding: utf-8 -*-
"""SD 卡状态探针：确认设备、mmc 块设备、挂载状态、卡上内容、可用格式化工具。只读，不写不格。"""
import sys
import paramiko

CREDS = [("root", "admin888"), ("admin", "admin888")]
HOSTS = ["192.168.6.214", "192.168.6.5"]  # 记忆里的设备 IP，依次试

CMDS = [
    ("ident", "uname -a; ip -4 addr show | grep inet | head -5; cat /etc/version /data/www/VERSION 2>/dev/null"),
    ("mmc dev nodes", "ls -l /dev/mmc* 2>/dev/null || echo NO_MMC_NODE"),
    ("dmesg mmc", "dmesg | grep -iE 'mmc|sd card|sdcard' | tail -15"),
    ("mounts", "grep -iE 'mmc|sd[a-z]' /proc/mounts || echo NOT_MOUNTED"),
    ("fsck tool", "which fsck.fat fsck.vfat mkfs.vfat mkfs.fat mkdosfs 2>/dev/null; ls /sbin/mkfs* /usr/sbin/mkfs* 2>/dev/null; busybox 2>/dev/null | grep -oE 'mkdosfs|mkfs.vfat|mkfs.fat|fsck.vfat|fsck.fat' | sort -u"),
    ("mountable probe (ro)", "mkdir -p /tmp/sdprobe; mount -o ro /dev/mmcblk0p1 /tmp/sdprobe 2>&1; echo mount_rc=$?; ls -la /tmp/sdprobe 2>/dev/null | head -20; umount /tmp/sdprobe 2>/dev/null; true"),
]


def run(c, cmd):
    s, o, e = c.exec_command(cmd, timeout=30)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return out.strip(), err.strip(), rc


def main():
    c = None
    used = None
    for h in HOSTS:
        for user, pw in CREDS:
            try:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c.connect(h, username=user, password=pw, timeout=8,
                          look_for_keys=False, allow_agent=False)
                used = "%s (%s)" % (h, user)
                break
            except Exception as ex:
                print("[probe] %s %s fail: %s" % (h, user, ex))
            if c:
                break
        if c:
            break
    if not c:
        print("NO SSH")
        sys.exit(1)
    print("connected:", used)
    for label, cmd in CMDS:
        out, err, rc = run(c, cmd)
        print("\n=== %s ===" % label)
        print(out if out else "(empty)")
        if err:
            print("[stderr]", err[:300])
    c.close()


if __name__ == "__main__":
    main()
