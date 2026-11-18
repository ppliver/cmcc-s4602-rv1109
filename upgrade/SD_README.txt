S4602 SD 卡升级固件 v20261118.1
================================

在原厂移动固件（Linux 4.19.111 #47, 千里眼）上的增强包。
原厂 boot_b / U-Boot / 内核 / system_a 完全不动，只刷 system_b(mtd9) 一个分区。

功能分层
--------
base（基础增强，随首刷自动安装）:
  - h264out UDP 推流: H264 720p25 -> QGC/PixelPilot (udp://<目标IP>:5600)
  - webd WEB 后台: http://<设备IP>:8080  (推流状态/参数/云链路开关)
  - 自愈框架: root/admin=admin888 SSH(dropbear:22) + swapp 换血 + 看门狗接管
  - swapp_patched: 原厂主程序增强版（喂狗 + 配套 UDP 管线）

optional（可选模块，SD 里有就装，可用 modules.conf 开关）:
  - go2rtc v1.9.15(增强补丁版): RTSP 8554 / WebRTC 8555 / HTTP 1984
      流源 = h264out --mirror 5602 -> rtp2pipe
  - easytier-mini v2.7.0 TUN 组网: 10.123.123.77/24 @ LTE_CAR
      tun.ko = vendor ABI 对齐版（CONFIG_IPV6=y, net_device 布局修复）

全新原厂机安装（一次性，需要串口 + 主机）
----------------------------------------
1. 主机打包: python tools/make_sd_upgrade.py
2. SD 卡用 Windows 格式化为 FAT32（全新卡，别用已损坏的卡），
   把 out/s4602_sd_upgrade/ 里的内容整个拷到 SD 根目录：
     system_b.img          <- 必须在 FAT 根（U-Boot fatload 用）
     s4602_upgrade/...     <- 升级载荷
3. 串口 1500000 8N1 接好，断电 -> 上电，运行：
     python tools/sd_upgrade_flash.py          # 截停 + 只读校验（默认安全）
     python tools/sd_upgrade_flash.py --go     # 写 mtd9 + 回读 CRC + reset
4. 首次启动（约 60s）自动完成：
     S99local 从 SD 引导 bootfix.sh -> bootfix 阶段0 按 VERSION+尺寸门控
     同步 base+optional 到 /data/www -> 自愈链拉起全部服务
5. 验证: http://<设备IP>:8080 WEB 后台；UDP 端口收流；
     go2rtc: http://<设备IP>:1984 ；easytier: /data/www/easytier_ltecar.log

后续升级（不用串口）
--------------------
改 SD 卡里的 s4602_upgrade（更新 VERSION + 文件）-> 重新上电即可，
bootfix 阶段0 检测到新 VERSION 自动同步。SD 卡升级完可以拔掉。

回滚
----
U-Boot 下用原厂 system_b.img (crc 0x38df701f, 458bc1e356c67e1a6109e670b6974f87)
fatload + mtd write spi-nand0 <addr> 0x03A80000 0x02C00000 即回出厂。

凭据
----
SSH: root/admin888 或 admin/admin888 (dropbear:22)
WEB: http://<设备IP>:8080 (/api/exec 为 root 通道，仅限内网实验设备)

本包镜像: flash/system_b.img = 46137344B, crc32 0x45bcc5e9,
reserved_pebs=332 与原厂逐字段一致（UBI PEB 预算红线）。
