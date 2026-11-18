# 路线 A 执行计划：原厂栈 + UDP 视频传输

> 2026-11-16。用户选定：先试原厂栈，补 UDP 视频传输（QGC 生态）。
> 核心思路：**让原厂 swapp 原生跑硬件 ISP/3A，用 h264out 从 rkispp_scale0 取 NV12 发 UDP**。

## 1. 已就位的东西（取证确认）

| 组件 | 状态 | 位置 |
|---|---|---|
| 原厂完整媒体栈 | ✅ 已解包 | `.stage_extract/vendor_rootfs/squashfs_root/system/{bin,lib}/` |
| swapp (glibc armhf) | ✅ | `system/bin/swapp`，30 个 NEEDED 全在 system/lib |
| COMMON_sc5338.xml | ✅ 原厂定制版 | `system/resource/scene_param/` |
| librkaiq / libeasymedia / MPP | ✅ | `system/lib/` |
| UDP 编码器 | ✅ 现成 | `tools/h264out.c --src nv12` → /dev/video19 → MPP → RFC3984 PT96 → UDP |
| 全片备份 | ✅ | `backup/snapshot/full-256M.img` |
| 原厂分区镜像 | ✅ 14 个 | `移动千里眼S4602原厂固件备份/` |

## 2. 两种启动方式（关键决策）

### 方式 ①：SD 卡加载原厂 kernel+DTB+rootfs（零 NAND 写入，推荐先试）
- 从 `boot_a.img` FIT 提取原厂 kernel(zImage 3.25MB) + DTB(85937B) —— 已有 `tools/inspect_vendor_backup.py --extract`。
- squashfs rootfs 需转成可挂载形态：原厂是 `squashfs in UBI volume rootfs, ubiblock0_0`（`ubi.mtd=9`）。SD 卡 FAT 无法直接放 UBI。
- **难点**：原厂 U-Boot 强制注入 `root=/dev/ubiblock0_0 ubi.mtd=9`，要挂载 squashfs 必须让 rootfs 落在 NAND 的 system 分区 UBI 卷里 → 绕不开写 NAND。
- 结论：纯 SD 卡加载 rootfs 走不通（rootfs 依赖 UBI 卷），除非重打包一个能直接从 SD 挂载的 initramfs（复杂）。

### 方式 ②：把 _b 槽恢复原厂（一次 NAND 写，可逆）
- 用 `tools/flash_from_sd.py` 的框架，把 `boot_a.img` → boot_b(0xA00000)、`system_a.img` → system_b(0x3A80000)。
- 写完 _b 槽就是纯原厂固件，断电重启走正常 autoboot，swapp 原生跑。
- **可逆**：_a 槽仍是原厂；全片备份在；随时能把 OpenIPC 刷回。
- **代价**：当前 OpenIPC 软件出图（8.7fps）会暂时不可用，直到刷回。

## 3. 执行步骤（方式 ②）

1. **准备 SD 卡**：把 `boot_a.img`、`system_a.img` 复制到 SD 卡根目录（FAT）。
2. **写刷机命令脚本**：基于 `flash_from_sd.py`，改成刷原厂镜像（保留四参数 + CRC 回读 + 只刷 _b）。
3. ** dry-run**：串口截停 + 只读校验，确认文件大小/CRC/magic 全对。
4. **--go 实际写入**：mtd erase + write boot_b/system_b，逐条下发，写完 CRC 比对。
5. **重启验证**：断电重启（原厂 FIT 无 OpenIPC 的 DTB 重叠 bug，应能正常 autoboot），抓启动日志确认 `Linux version 4.19.111` + swapp 起来。
6. **确认出图**：`swapp` 跑后，用 `media-ctl`/`v4l2-ctl`（原厂自带）看 rkispp_scale0/video19 是否出 NV12 帧。
7. **接 UDP**：在原厂 rootfs 里部署 `h264out --src nv12 --dev /dev/video19 --ip <QGC_IP> --port 5600`，验证 QGC 收到画面。
   - 若 swapp 独占 video19 导致 busy → 需改为 swapp 只跑 3A、h264out 从 video5(mainpath) 取帧，或让 swapp 输出复用其 VENC 码流。

## 4. 风险与回退

| 风险 | 等级 | 回退方法 |
|---|---|---|
| 写坏 _b 槽变砖 | 中 | _a 槽原厂 + 全片备份；U-Boot 未动，可 SD 恢复 |
| swapp 强依赖云端鉴权不出图 | 中 | 抓包看是否有本地 H264 出口；否则转路线 B |
| swapp 与 h264out 抢 video19 | 中 | 改取 video5 或复用 swapp VENC 码流 |
| 原厂 FIT autoboot 挂死 | 低 | 原厂 FIT 无 DTB 重叠 bug；真挂死则 SD 三参 bootm |

## 5. 待用户确认
- **是否现在执行方式 ②（写 _b 槽）？** 这是唯一不可逆动作（虽可回退）。
- QGC 目标 IP（默认 192.168.6.88:5600）。
- 设备当前是否上电、串口接在哪个 COM 口（历史用 COM4，user memory 另提到 COM3/串口桥 192.168.6.195:2000，需确认）。
