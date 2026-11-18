#!/bin/sh
# /data/www/bootfix.sh v7 -- SD-upgrade firmware core (base + optional modules).
# Launched detached by /etc/init.d/S99local.sh (read-only system_b squashfs).
#
# Layout (2026-11-18 SD upgrade firmware):
#   base     = h264out UDP streaming + webd web console + self-heal chain
#   optional = go2rtc (enhanced patch build) + easytier TUN mesh (vendor-aligned tun.ko)
#   On every boot stage 0 syncs /data/www from the SD card "s4602_upgrade" payload
#   when its VERSION differs -> firmware upgrade = swap SD, power cycle. No serial.
#
# PROVEN ORDER (do not reshuffle):
#   fast wins (shadow/dropbear/webd/tmp, no ISP) -> wait swapp -> swap EARLY
#   (sub-second gap, hw watchdog takeover) -> 35s IQ settle -> alive re-check ->
#   h264out. Early h264out = RkLumaCoreTh page fault = 17s bootloop (serial evidence).

LOG=/data/www/bootfix.log
exec >>"$LOG" 2>&1
echo "=== bootfix v7 $(date) pid $$ ==="

# /data must be up + writable (S30mountfs runs before S99local in rcS)
i=0
while [ $i -lt 20 ]; do
  [ -w /data ] && break
  sleep 1; i=$((i+1))
done

# ---- 0) SD payload sync, gated by VERSION (upgrade = swap SD + reboot) ----
UPD=""
for d in /tmp/udisk0 /mnt/mmcblk1 /tmp/sdup; do
  [ -f "$d/s4602_upgrade/VERSION" ] && UPD="$d/s4602_upgrade" && break
done
if [ -z "$UPD" ]; then
  mkdir -p /tmp/sdup
  mount -t vfat /dev/mmcblk1p1 /tmp/sdup 2>/dev/null
  [ -f /tmp/sdup/s4602_upgrade/VERSION ] && UPD=/tmp/sdup/s4602_upgrade
fi
if [ -n "$UPD" ]; then
  SDV=$(cat "$UPD/VERSION" 2>/dev/null)
  INS=$(cat /data/www/.upgrade_version 2>/dev/null)
  if [ -n "$SDV" ] && [ "$SDV" != "$INS" ]; then
    echo "SD sync: found version $SDV (installed: ${INS:-none})"
    OK=1
    if [ -f "$UPD/MANIFEST.sizes" ]; then
      while read -r f sz; do
        [ -z "$f" ] && continue
        a=$(wc -c < "$UPD/$f" 2>/dev/null)
        [ "$a" != "$sz" ] && { echo "SD sync: size mismatch $f ($a != $sz)"; OK=0; }
      done < "$UPD/MANIFEST.sizes"
    else
      echo "SD sync: MANIFEST.sizes missing"; OK=0
    fi
    if [ "$OK" = "1" ]; then
      REPLACED=0
      while read -r f sz; do
        [ -z "$f" ] && continue
        d=${f%%/*}; n=${f#*/}
        [ "$d" = "base" ] || [ "$d" = "optional" ] || continue
        # easytier-mini (4.4MB) lives on data_bk, NOT on tight /data - handled below
        [ "$f" = "optional/easytier-mini" ] && continue
        # safe replace: temp copy + rename (running script keeps old inode)
        if cp -f "$UPD/$f" "/data/www/.new.$n" 2>/dev/null; then
          mv -f "/data/www/.new.$n" "/data/www/$n" 2>/dev/null
          echo "SD sync: installed $f"
          [ "$n" = "bootfix.sh" ] && REPLACED=1
        else
          echo "SD sync: copy failed $f"
        fi
      done < "$UPD/MANIFEST.sizes"
      # easytier-mini prefers data_bk (20.7MB, factory-empty) over tight /data
      mkdir -p /tmp/bk
      mount -t ubifs /dev/ubi13_0 /tmp/bk 2>/dev/null
      if [ -w /tmp/bk ] && [ -f "$UPD/optional/easytier-mini" ]; then
        cp -f "$UPD/optional/easytier-mini" /tmp/bk/.new.easytier-mini 2>/dev/null
        chmod +x /tmp/bk/.new.easytier-mini 2>/dev/null
        mv -f /tmp/bk/.new.easytier-mini /tmp/bk/easytier-mini 2>/dev/null
        echo "SD sync: easytier-mini -> data_bk"
      fi
      chmod +x /data/www/bootfix.sh /data/www/webd /data/www/h264out_v4 \
              /data/www/rtp2pipe /data/www/np_resetpasswd.sh 2>/dev/null
      echo "$SDV" > /data/www/.upgrade_version
      sync
      echo "SD sync: version $SDV installed"
      [ "$REPLACED" = "1" ] && exec /bin/sh /data/www/bootfix.sh
    else
      echo "SD sync: ABORTED (size gate failed), keeping installed $INS"
    fi
  fi
else
  echo "SD sync: no s4602_upgrade payload on SD, skip"
fi

# ---- 1) fast wins: shadow, faillock, dropbear, webd, DNS (no ISP involvement) ----
# log hygiene: /data is a 17.5MB UBIFS partition - runaway logs fill it 100%,
# then EVERY write (incl. the shadow restore below) fails and files truncate
# to 0 bytes (seen 2026-11-18: easytier_ltecar.log grew to 18MB -> root/admin888
# "wrong" because /data/.shadow was an un-writeable empty file). Rotate old logs.
for lf in /data/www/*.log; do
  [ -f "$lf" ] || continue
  [ "$(wc -c < "$lf")" -gt 262144 ] && : > "$lf"
done
cp -f /data/.shadow /data/.shadow.factory 2>/dev/null
printf '%s\n' \
  'root:$1$s4602ab$BvWIoXqRYt0y6IxsQvccW/::0:99999:7:::' \
  'sshd:*::0:99999:7:::' \
  'admin:$1$s4602ab$BvWIoXqRYt0y6IxsQvccW/::0:99999:7:::' > /data/.shadow
sync
echo "shadow -> $(grep -c '^root:\$1' /data/.shadow) md5 lines"
rm -f /var/run/faillock/* 2>/dev/null
mount -o bind /data/www/np_resetpasswd.sh /etc/init.d/resetPasswd.sh 2>/dev/null
mkdir -p /data/dropbear
[ -f /data/dropbear/dropbear_rsa_host_key ] || \
  cp /etc/dropbear/dropbear_rsa_host_key /data/dropbear/ 2>/dev/null
if ! ps w | grep '[d]ropbear' >/dev/null 2>&1; then
  /usr/sbin/dropbear -r /data/dropbear/dropbear_rsa_host_key -p 22 >/dev/null 2>&1 &
  echo "dropbear started"
fi
if ! ps w | grep '[w]ebd' >/dev/null 2>&1; then
  /data/www/webd >/tmp/webd.log 2>&1 &
  echo "webd started"
fi
# /etc/resolv.conf -> /tmp/resolv.conf (tmpfs, lost on reboot); easytier peers need DNS
if [ ! -s /tmp/resolv.conf ]; then
  printf 'nameserver 223.5.5.5\nnameserver 114.114.114.114\n' > /tmp/resolv.conf 2>/dev/null
  echo "DNS nameservers written"
fi

# ---- module switches (optional layer) ----
go2rtc=1
easytier=1
[ -f /data/www/modules.conf ] && . /data/www/modules.conf 2>/dev/null

# ---- 1b) easytier mesh VPN TUN mode ----
# tun.ko = vendor-ABI-aligned rebuild (CONFIG_IPV6=y, net_device mtu@0x140).
# Verified stable 2026-11-18: TUNSETIFF/SIOCSIFMTU/write OK, 7 peers, no hardlock.
if [ "$easytier" = "1" ]; then
  mkdir -p /dev/net /tmp/bk
  [ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200
  if ! grep -q '^tun ' /proc/modules 2>/dev/null; then
    if [ -f /data/www/tun.ko ] && [ ! -f /data/www/.tun_insmod_failed ]; then
      touch /data/www/.tun_insmod_failed
      if insmod /data/www/tun.ko 2>/dev/null; then
        rm -f /data/www/.tun_insmod_failed
        echo "tun.ko (vendor-aligned) insmod ok"
      else
        echo "tun.ko insmod FAILED (flag set, skip next boots)"
      fi
    fi
  fi
  mount -t ubifs /dev/ubi13_0 /tmp/bk 2>/dev/null
  ETBIN=""
  for p in /tmp/bk/easytier-mini /data/.swdb_bk/easytier-mini /data/www/easytier-mini; do
    [ -x "$p" ] && ETBIN="$p" && break
  done
  if grep -q '^tun ' /proc/modules 2>/dev/null && [ -n "$ETBIN" ] && \
     [ -f /data/www/easytier_ltecar.toml ]; then
    if ! ps w | grep '[e]asytier-mini' >/dev/null 2>&1; then
      cd /data/www
      # RUST_LOG=off: easytier-mini has no --log-level flag; without this it
      # writes INFO/WARN reconnect spam (~400B/s when a peer is dead) which
      # filled the 17.5MB /data partition to 100% on 2026-11-18 and silently
      # broke every write (shadow truncated -> root/admin888 auth failure).
      RUST_LOG=off "$ETBIN" -c /data/www/easytier_ltecar.toml > /tmp/easytier_ltecar.log 2>&1 &
      echo $! > /tmp/easytier.pid
      echo "easytier-mini TUN started (LTE_CAR) from $ETBIN (RUST_LOG=off)"
    else
      echo "easytier-mini already running, skip"
    fi
    # hard stop: cap the log at 512K. In-place truncate is useless (fd offset
    # keeps growing on tmpfs), so restart easytier with a fresh log.
    (while true; do
      sleep 60
      if [ "$(wc -c < /tmp/easytier_ltecar.log 2>/dev/null || echo 0)" -gt 524288 ]; then
        echo "$(date) easytier log >512K, restart+truncate" >> /data/www/bootfix.log
        [ -s /tmp/easytier.pid ] && kill "$(cat /tmp/easytier.pid)" 2>/dev/null
        : > /tmp/easytier_ltecar.log
        sleep 1
        cd /data/www
        RUST_LOG=off "$ETBIN" -c /data/www/easytier_ltecar.toml >> /tmp/easytier_ltecar.log 2>&1 &
        echo $! > /tmp/easytier.pid
      fi
    done) &
  else
    echo "SKIP easytier: tun module or mini/config unavailable"
  fi
else
  echo "easytier disabled by modules.conf"
fi

# ---- 2) rebuild /tmp runtime assets (lost on power-off, tmpfs) ----
if [ ! -x /tmp/rtlib/rtlib/ld-linux-armhf.so.3 ]; then
  mkdir -p /tmp/rtlib && tar xzf /data/www/rtlib.tar.gz -C /tmp/rtlib 2>/dev/null
fi
mkdir -p /tmp/factory/lib
ln -sf /system/lib/librockchip_mpp.so.1 /tmp/factory/lib/librockchip_mpp.so.1
cp /data/www/h264out_v4 /tmp/h264out_v4 2>/dev/null
chmod +x /tmp/h264out_v4 2>/dev/null
if [ -f /data/www/swapp_patched.xz ] && [ ! -x /tmp/swapp_patched ]; then
  xz -dkc /data/www/swapp_patched.xz > /tmp/swapp_patched
  chmod +x /tmp/swapp_patched
fi

# ---- 3) wait for factory chain: any swapp process (original or patched) ----
i=0
while [ $i -lt 45 ]; do
  ps w | grep '[s]wapp' >/dev/null 2>&1 && break
  sleep 2; i=$((i+1))
done
echo "swapp seen after ~$((i*2))s of waiting"

# ---- 4) swap EARLY: original -> patched (sub-second gap, watchdog takeover) ----
if [ -x /tmp/swapp_patched ]; then
  if ps w | grep '[s]wapp' | grep -v 'swapp_patched' | grep -v grep >/dev/null 2>&1; then
    P=$(ps w | grep '[s]wapp' | grep -v 'swapp_patched' | grep -v grep | head -1 | awk '{print $1}')
    kill -9 "$P" 2>/dev/null
    RKFPS=25 LIBRNNN_RUNTIME_PATH=/system/lib/librknn_runtime.so \
    LD_LIBRARY_PATH=/system/lib/algorithms:/system/lib:/system/lib \
    /tmp/swapp_patched > /tmp/swapp.log 2>&1 &
    echo "swapped original(pid $P) -> patched(pid $!)"
  else
    echo "patched-only (or none); no swap needed"
  fi
else
  echo "WARN: /tmp/swapp_patched missing; h264out will be skipped"
fi

# ---- 5) IQ settle window: deploy-proven 30s (+5s margin over deploy's flow) ----
sleep 35
if ! ps w | grep '[s]wapp_patched' >/dev/null 2>&1; then
  echo "SKIP h264out: swapp_patched died during IQ settle window"
  echo "=== bootfix done (degraded) ==="
  exit 0
fi
echo "IQ settle done, swapp_patched alive"

# ---- 6) h264out per /data/www/stream.conf (only if not already running) ----
if ! ps w | grep '[h]264out_v4' >/dev/null 2>&1; then
  . /data/www/stream.conf 2>/dev/null
  if [ "$w" = "1920" ]; then CAPW="--capw 2560 --caph 1440"; else CAPW=""; fi
  rm -f /tmp/h264out.log
  /tmp/rtlib/rtlib/ld-linux-armhf.so.3 \
    --library-path /tmp/rtlib/rtlib:/system/lib:/lib \
    /tmp/h264out_v4 --src nv12 --dev /dev/video21 \
    --ip ${ip:-192.168.6.88} --port ${port:-5600} \
    --w ${w:-1280} --h ${h:-720} --fps ${fps:-25} \
    --kbps ${kbps:-1500} --gop ${gop:-25} \
    --codec ${codec:-h264} --pace ${pace:-3} --cir ${cir:-2} $CAPW \
    --mirror 127.0.0.1:5602 \
    > /tmp/h264out.log 2>&1 &
  echo "h264out started (conf: ${w:-1280}x${h:-720} ${codec:-h264} ${kbps:-1500}k)"
else
  echo "h264out already running, skip"
fi

# ---- 7) go2rtc optional module (run from /tmp; xz source if present) ----
if [ "$go2rtc" = "1" ]; then
  if [ ! -x /tmp/go2rtc_linux_arm ]; then
    if [ -f /data/www/go2rtc.xz ]; then
      xz -dkc /data/www/go2rtc.xz > /tmp/go2rtc_linux_arm
      chmod +x /tmp/go2rtc_linux_arm
    elif [ -f /data/www/go2rtc_linux_arm ]; then
      cp /data/www/go2rtc_linux_arm /tmp/go2rtc_linux_arm
      chmod +x /tmp/go2rtc_linux_arm
    fi
  fi
  if [ -x /tmp/go2rtc_linux_arm ] && ! ps w | grep '[g]o2rtc' >/dev/null 2>&1; then
    cd /data/www
    /tmp/go2rtc_linux_arm > /tmp/go2rtc.log 2>&1 &
    echo "go2rtc started"
  fi
  # go2rtc watchdog (leftover from 2026-11-18 mp4 panic saga): probe every 20s,
  # restart on silent death, max 10 restarts per boot (counter in /tmp).
  (while true; do
    sleep 20
    g=1
    [ -f /data/www/modules.conf ] && . /data/www/modules.conf 2>/dev/null
    [ "${go2rtc:-1}" = "1" ] || continue
    [ -x /tmp/go2rtc_linux_arm ] || continue
    if ! ps w | grep '[g]o2rtc' >/dev/null 2>&1; then
      R=$(cat /tmp/.go2rtc_restarts 2>/dev/null); R=${R:-0}
      if [ "$R" -lt 10 ]; then
        echo $((R+1)) > /tmp/.go2rtc_restarts
        echo "$(date) go2rtc watchdog: restart #$((R+1))" >> /data/www/go2rtc_watchdog.log
        cd /data/www
        /tmp/go2rtc_linux_arm > /tmp/go2rtc.log 2>&1 &
      fi
    fi
  done) &
  echo "go2rtc watchdog armed"
else
  echo "go2rtc disabled by modules.conf"
fi

echo "=== bootfix v7 done ==="

# ---- v4 guard: restore shadow if reverted (safety net) ----
(while true; do
  if ! grep -q '^root:$1$' /data/.shadow 2>/dev/null; then
    echo "$(date) shadow reverted -> restore" >> /data/www/shadow_guard.log
    cp /data/www/shadow.md5 /data/.shadow 2>/dev/null
  fi
  sleep 30
done) &
