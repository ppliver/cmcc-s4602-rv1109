# S4602 原厂固件改动复盘（2026-11-16 ~ 11-17）

> 范围：**_b 槽刷回原厂之后**的全部工作。目标：在原厂系统上把硬件 ISP 画面导成 UDP H264 给 QGC（udp://:5600），并排查延迟。  
> 结论先行：**已闭环**——720p15 H264 硬件 ISP 画质推 QGC 成功，与云端流并发存活；但当前**部署全在内存里，设备已因看门狗事故重启回干净原厂态，等下次断电重启窗口重新部署（约 5 分钟）**。



---

## 1. 持久化改动（写入 flash，重启不丢）——只有一项

| 改动             | 内容                                                                                           | 风险控制                                                  |
| -------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **_b 槽刷回原厂固件** | boot_b.img(4MB) + system_b.img(44MB)，经 SD 卡 wget + U-Boot mtd erase/write 四参数 + CRC32 回读双向校验 | 只写 \_b，\_a 保留原厂；全片备份在 `backup/snapshot/full-256M.img` |

其余**全部改动都在 tmpfs / 内存态**，重启即失，属"可重复部署"性质。

## 2. 内存态改动清单（重启全丢，可脚本重建）

| 类别            | 内容                                                                                       |
| ------------- | ---------------------------------------------------------------------------------------- |
| 启动方式          | U-Boot `setenv bootargs '... init=/bin/sh'` + `boot_fit` → root shell（不写 flash）          |
| tmpfs 补挂      | /tmp /run /dev/shm devpts（init=/bin/sh 绕过 inittab 导致缺失）                                  |
| rcS 补跑        | S30mountfs + S40network（udhcpc 得 192.168.6.214）                                          |
| swapp 全栈      | `sh /system/etc/init.d/start_swapp.sh`（swsyscmd + swnetservice + swapp + guard.sh）       |
| rtlib         | /tmp/rtlib/ = armhf glibc 2.43 运行时（WSL 交叉 sysroot 打包，2.3MB）                              |
| h264out_v3/v4 | /tmp/h264out_v3(67424B) + /tmp/h264out_v4(67424B)，含 v4 延迟探针                              |
| 云开关脚本         | /tmp/cloud_off.sh（route del default）/ /tmp/cloud_on.sh（route add default gw 192.168.6.1） |

## 3. 当前设备状态（2026-11-17 04:30）

- 设备已重启回**干净原厂态**：只开 18686 私有云端口，APP 观看正常，无本地出流。
- 上次会话的 h264out / rtlib / 开关脚本**全部丢失**。
- 主机侧就绪：`.stage_http/`（h264out_v3/v4、rtlib.tar.gz）+ HTTP(8765) / FTP(21) 服务在跑。

**重新部署流程（下次断电窗口执行，~5 分钟）**：

1. 后台挂 `python tools/uboot_intercept.py --port COM4`（等上电自动截停到 `=>`）
2. `python tools/boot_to_rootsh.py`（注入 init=/bin/sh）
3. 依次 `python tools/deploy_stage1.py`（rcS 拉起系统）→ `deploy_stage2.py`（swapp 栈+网络）→ `deploy_stage7.py`（rtlib）
4. 设备 `ftpget 192.168.6.109 /tmp/h264out_v4 h264out_v4` + 启动：  
   `/tmp/rtlib/ld-linux-armhf.so.3 --library-path /tmp/rtlib:/system/lib:/lib /tmp/h264out_v4 --src nv12 --dev /dev/video21 --ip 192.168.6.88 --port 5600 --fps 15 --kbps 1500 > /tmp/h264out.log 2>&1 &`
5. 重建云开关脚本（见 §6）。

## 4. h264out 原厂适配——四个关键技术坑（复盘重点）

1. **glibc 2.42+ 默认 64-bit time_t**：v4l2_buffer 结构变大 → QUERYBUF/QBUF 的 `_IOWR` ioctl 号全错 → ENOTTY。  
   修法：编译加 **`-D_TIME_BITS=32`**。（G_FMT/S_FMT/REQBUFS 不含 timeval，不受影响。）
2. **vendor 内核采集节点 = DMABUF 导入模式**（UAPI2 rockit 设计）：QUERYBUF 不实现是特性不是 bug。  
   改造链：`REQBUFS(memory=DMABUF)` → MPP frmgrp DRM(0x3) 分配 6×1382400B → `mpp_buffer_info_get_with_caller` 拿 fd（旧 MPP b2ee155 无 mpp_buffer_get_fd）→ `QBUF(m.fd=DRM fd)` → DQBUF 直喂编码，零拷贝。
3. **stride 对齐**：g_vs 必须 16 对齐（=720），不能沿用 OpenIPC 时代的 256 对齐，否则 MPP 读 UV 错位。  
   |  |  |  
   | --- | --- |
4. **vendor MPP 全部接受 RC 精调**（v3 新增）：`--gop N`、`--mbrc 0|1`、`rc:bps_max/min = target ±10%`。SET_CFG ret=0。

编译命令（WSL）：

```
arm-linux-gnueabihf-gcc -O2 -D_TIME_BITS=32 -I out/reference/mpp/inc-1.0.9 \
  -o .stage_http/h264out_v4 tools/h264out.c -lm -ldl -lpthread
```

## 5. 网络与运维通道（实测结论）

| 事项            | 结论                                                                                                                |
| ------------- | ----------------------------------------------------------------------------------------------------------------- |
| 登录            | **shell 密码路彻底关闭**：main.sh 开机 cmp .shadow 与 /etc/shadow-，不等则重置；唯一入口 = U-Boot init=/bin/sh                          |
| 传文件           | **设备 wget 是坏的**（/usr/bin/wget 存在但 applet not found）；只有 **ftpget** 可用 → 主机 `tools/ftpserve.py`（极简匿名 FTP，必须实现 PASV） |
| 串口长命令         | 必须 `MSYS_NO_PATHCONV=1`（否则 /tmp/rtlib/... 被 Git Bash 改写成 Windows 路径）                                              |
| 进程查找          | 串口里 `grep '[h]264out'` 括号模式偶尔失灵，用 `grep 264out` 稳妥                                                                |
| 内核无 netfilter | iptables 存在但 filter 表未编译、无模块可 insmod → 防火墙方案不可行                                                                   |

## 6. 云传输开关（已验证双向可用）

- **云端视频 = swapp 自持一条 TCP 直连移动云 36.213.184.74:50661**（swnetservice 只是信令，无独立 socket）。
- 开关 = 默认路由：
  - `/tmp/cloud_off.sh`：`route del default` → 云断，swapp ~50s 内自动重连尝试
  - `/tmp/cloud_on.sh`：`route add default gw 192.168.6.1` → 实测重连成功
- guard.sh 只查进程存在性，不受影响。进程零损伤。
- 带宽实测：**APP 不观看时云端几乎零带宽**（断云前后 TX 均 ≈3.3Mbps）；云端拉流时的争抢需在 APP 观看中测。

## 7. 延迟定位结论（v4 探针，1500kbps@15fps）

| 环节                | 耗时         | 说明                        |
| ----------------- | ---------- | ------------------------- |
| drv（rkispp 管线深度）  | **59 ms**  | 驱动时间戳→DQBUF，≈1 帧周期，硬件固有   |
| put + get（MPP 编码） | ≈18 ms     | 正常                        |
| tot（DQBUF→RTP 发出） | 17 ms      | 很快                        |
| **设备端合计**         | **≈78 ms** | 码率 3M→1.5M 无感差异的原因：设备端占比小 |

- **25fps 实测仍 14.94fps**：scale2 被限制。两个假设待验：① swapp 占 scale0/1 的 ISPP 吞吐挤占；② swapp 场景配置的通道限速（defaultparam.txt `main_stream_fps=15` 佐证）。**下一步先试无损路径：`v4l2-ctl -d /dev/video21 --set-parm=25`，不动任何进程。**
- **剩余延迟大头 = QGC 侧 rtpjitterbuffer（GStreamer 默认 200ms）**。建议在自己 fork 里降 latency 到 20-50ms 或去掉（LAN 0 丢包）。
- 注意：`--fps` 参数必须与实际帧率一致（15），否则 RTP 时钟错位反而加重 QGC 缓冲。

## 8. 看门狗事故（教训级，勿再犯）

- **现象**：root shell 会话里 `kill swapp` → swapp 退出时关闭 /dev/watchdog → 原厂内核 NOWAYOUT（看门狗一旦启用停不掉）→ 无人喂狗 → **约 40 秒后硬件看门狗重启整机**，内存态部署全丢。
- **触发链**：guard.sh 是进程级看门狗（kill 它即可绕过），但 **/dev/watchdog 由 swapp 自己持有并喂狗**——这是两层独立看门狗，之前只防了第一层。
- **规则**：**root shell 会话里永远不要 kill swapp**。改 swapp 行为（如 RKFPS）的替代路径：
  1. 无损：v4l2 层调帧率（set-parm）、云开关走路由；
  2. 若必须重启 swapp：极短窗口 kill+立即重启（同一命令行内完成，swapp init 约 10s，看门狗超时约 30-40s，仍有风险），或接受重启后重新部署。
- 顺带发现：**SD 卡 FAT 已损坏**（mmcblk1 持续报 clusters badly computed），持久化用 SD 前需主机重格。

## 9. 遗留待办

| 优先级 | 事项                                                        |
| --- | --------------------------------------------------------- |
| P0  | 等断电窗口重新部署，恢复 720p15@1500kbps 推流                           |
| P1  | QGC fork 降 rtpjitterbuffer latency（预期砍掉 150ms+ 感知延迟，收益最大） |
| P2  | `--set-parm=25` 无损试提帧率；成功则同步改 --fps                       |
| P2  | 持久化方案讨论：sdcard_upgrade 官方通道（需先重格 SD）或固化部署脚本一键化            |
| P3  | 云开关 A/B（需 APP 正在拉流时测）                                     |

## 10. 一句话总结

原厂固件上我们**没有改任何原厂文件**：系统层只有"刷回原厂"这一次持久动作；运行层是"init=/bin/sh + tmpfs 自建运行时"的可重复部署模式，原厂 swapp 云端流全程存活。画质/稳定性由原厂硬件 ISP 保证，我们只做旁路采集→MPP 硬编→UDP。真正的延迟瓶颈已定位到 QGC 抖动缓冲，不在设备端。
