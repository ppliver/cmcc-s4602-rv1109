# S4602 / RV1109 文档索引

> 更新：2026-11-18。文档按**路线**分为三组：
> - **`openipc/`** — OpenIPC 移植线（在 OpenIPC 4.19 内核上移植驱动/出图，2026-11-11 ~ 11-16）
> - **`vendor/`** — 原厂移动固件修改线（刷回原厂 swapp 栈后在其上增强，2026-11-16 起，**当前主线**）
> - **`shared/`** — 两线共享的救砖/备份/引导链基础文档（2026-06-29 变砖起）

## 路线演进（先读这段再看任何一篇）

```
06-29 变砖(半砖) ──> 11-11 评估 OpenIPC 可行性 ──> 11-13 OpenIPC 首出图(h264 软编)
                                                        |
                                        11-16 画质死结(librkaiq 缺 camera_info,无解)
                                                        |
                                                        v
                            11-16 转向 vendor 路线: 刷回原厂 _b 槽, 原厂 swapp 原生出图
                                                        |
                            11-17 原厂栈上 UDP 推流(QGC) + bootfix 自愈 + SSH 修复
                                                        |
                                                        v
                            11-18 SD 卡升级固件 v20261118.1(当前交付物, 详见 vendor/SD_UPGRADE_FIRMWARE.md)
```

## openipc/ — OpenIPC 移植线（11 篇，已收尾，结论：画质死结）

| 文档 | 内容 | 状态 |
|---|---|---|
| [PORTING.md](openipc/PORTING.md) | 原厂→OpenIPC 移植差距矩阵与总报告 | 离线取证完成 |
| [BOOT_FEASIBILITY.md](openipc/BOOT_FEASIBILITY.md) | 能不能刷 OpenIPC / U-Boot 引导三层判定 | 判定：可启动，启动红线见 MEMORY |
| [SC5338.md](openipc/SC5338.md) | sensor 资料包（离线） | 已完成 |
| [FIRST_LIGHT.md](openipc/FIRST_LIGHT.md) | 首次出图 | 11-13 达成 |
| [MPP_H264.md](openipc/MPP_H264.md) | 硬件 H.264 编码实证（软件链路 3.5fps 上限） | 已实证 |
| [DIRECT_H26X.md](openipc/DIRECT_H26X.md) | 设备端直发 H.264/H.265 RTP（免 Windows 中继） | 11-13 闭环 |
| [OUTGOING_RTP.md](openipc/OUTGOING_RTP.md) | outgoing(RTP over UDP) 方案评估 | 已实现 |
| [P0_RUNTIME_ABI.md](openipc/P0_RUNTIME_ABI.md) | 原厂运行时部署 + easymedia ABI 实证 | 全线打通, 唯一阻塞 IQ |
| [P1_IQ_INJECTION.md](openipc/P1_IQ_INJECTION.md) | IQ 注入 + 实时出流 | 死结, 勿再试 |
| [WHALETX_REFERENCE.md](openipc/WHALETX_REFERENCE.md) | WhaleTX 参考架构评估 | 评估完成 |
| [H264OUT_COLOR_SAGA.md](openipc/H264OUT_COLOR_SAGA.md) | h264out 软件 RAW→NV12→MPP 出图 + 色彩破案（Bayer 相位/色度折中） | 11-16 收口，OpenIPC 线终点 |

**线末结论**：OpenIPC 预编译 librkaiq 缺 sc5338 camera_info，硬件 ISP 死锁（详见 MEMORY.md "硬件 ISP 出图=已闭环死结"）；软件 h264out 出图可用但画质到顶。→ 用户 11-16 决定转 vendor 线。

## vendor/ — 原厂移动固件修改线（9 篇，当前主线）

| 文档 | 内容 | 状态 |
|---|---|---|
| [VENDOR_BSP_ROUTE.md](vendor/VENDOR_BSP_ROUTE.md) | 转向 vendor 路线的取证与分叉评估 | 11-16 |
| [VENDOR_ROUTE_A_PLAN.md](vendor/VENDOR_ROUTE_A_PLAN.md) | 路线 A：原厂栈 + UDP 视频传输（QGC）执行计划 | 已执行 |
| [VENDOR_FIRMWARE_ANALYSIS.md](vendor/VENDOR_FIRMWARE_ANALYSIS.md) | 原厂 rootfs/data 解包分析（密码机制/运行机制） | 静态分析完成 |
| [VENDOR_RUNTIME_REVIEW.md](vendor/VENDOR_RUNTIME_REVIEW.md) | _b 槽刷回原厂后的改动复盘 | 11-16~17 闭环 |
| [SD_UPGRADE_FIRMWARE.md](vendor/SD_UPGRADE_FIRMWARE.md) | **SD 卡升级固件 v20261118.1（当前交付物）**：base(h264out+webd+自愈) + optional(go2rtc+easytier TUN) | 待好 SD 卡实测全链 |
| [GO2RTC_MP4_PANIC.md](vendor/GO2RTC_MP4_PANIC.md) | go2rtc mp4/MSE 播放 SIGSEGV 根治 | 已闭环 |
| [TUN_ABI_SAGA.md](vendor/TUN_ABI_SAGA.md) | tun.ko 对 vendor 内核 ABI 排障（EasyTier 组网） | 已闭环 |
| [ETH_LINK_DIAGNOSIS.md](vendor/ETH_LINK_DIAGNOSIS.md) | 有线网卡(YT8512B)折腾全程 + 坑速查 | 根因已修, 现场验证受内核回归阻塞 |
| [FIREFLY_BSP_BUILD_PLAN.md](vendor/FIREFLY_BSP_BUILD_PLAN.md) | Firefly 公开 BSP 重建计划（路线 P2, 备用） | 计划 |

## shared/ — 救砖/备份/引导（4 篇，两线共用）

| 文档 | 内容 |
|---|---|
| [INCIDENT.md](shared/INCIDENT.md) | 06-29 变砖复盘（半砖根因、备份清单） |
| [RECOVERY.md](shared/RECOVERY.md) | 救砖流程（实证版） |
| [FULL_BACKUP.md](shared/FULL_BACKUP.md) | 完整备份 SOP（分通道、双来源） |
| [UBOOT.md](shared/UBOOT.md) | U-Boot 要不要编译/引导链决策 |

## 其他

- `docs/evidence/` — 现场抓取原始证据（启动日志、probe 输出、CRC 回读等），各文档内相对引用，勿移动。
- 长期记忆在仓库外：`.workbuddy/memory/MEMORY.md`（两线结论都在这里，本文档索引是其文档侧投影）。
