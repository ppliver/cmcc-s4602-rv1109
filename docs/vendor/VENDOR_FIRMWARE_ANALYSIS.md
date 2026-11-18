# S4602 原厂固件解包分析：密码机制 / 运行机制 / 隐藏功能排查

> 样本：`.stage_extract/vendor_rootfs/squashfs_root/`（squashfs 解包）+ `.stage_extract/vendor_data/fs/`（data 分区解包）
> 分析日期：2026-11-17。结论可信度：基于静态字符串/符号/脚本分析，RKRTSP 触发行为待下次部署实测确认。

---

## 一、密码机制（完整还原）

### 1.1 文件布局
| 文件 | 作用 |
|---|---|
| `/etc/passwd-` | 出厂 passwd 模板（root/sshd/admin 三账号） |
| `/etc/shadow-` | **出厂 shadow 模板（真正的密码源）** |
| `/etc/passwd -> /data/.passwd` | 运行时 passwd（data 分区，可写） |
| `/etc/shadow -> /data/.shadow` | 运行时 shadow（data 分区，可写） |
| `/etc/login.user.deny` | 内容仅一行：**`root`** —— root 禁止登录 |

### 1.2 出厂 hash（/etc/shadow-，可用于离线爆破）
```
root:$6$tBNh4kgPIMTteL7N$fTZ7hmbe...OdQq5z46q4KhHfKG.1
sshd:*                    （禁用）
admin:$6$yYUl5zTZUyx4OW/K$xnUmIhuQpezAvTKPZhTgya57ScD9B5Yq...
```
- root 与 admin 是**两个不同的 SHA512-crypt hash**（ salt 各异）——不是简单互拷。
- admin 全名注释 "Live session user,,,"。

### 1.3 防篡改逻辑（main.sh init()，这就是我们密码改不上的根因）
```sh
# /data/.passwd 与模板不一致 → 强制回滚出厂
! cmp -s /data/.passwd /etc/passwd- && cp /etc/passwd- /data/.passwd

# /data/.shadow 三选一校验：
#   与 /data/.shadow-（备份）相同 OR 与 /etc/shadow- 相同 OR 恰好 3 行且每行 9 字段
#   三个条件都不满足 → 强制回滚出厂 + sync
! cmp -s /data/.shadow /data/.shadow- && ! cmp /data/.shadow /etc/shadow- \
   || [ "$count" != "3" ] && cp -f /etc/shadow- /data/.shadow && sync
```
含义：**每次开机密码都被强制校验回滚**；想持久改密码必须同时改 `.shadow` 且造出合法的 `.shadow-` 备份（或让新 hash 也是"3 行 × 9 字段"）。这解释了之前所有改密码尝试失败的根因。

### 1.4 登录链
- `inittab`: `::respawn:/sbin/getty -L ttyFIQ0 1500000 vt100` → 串口登录
- 登录后先跑 `/etc/login.sh`：显示"所有操作将被记录，违法将依法追究"横幅，必须输 Y 才进 shell。
- `resetPasswd.sh`：一键恢复出厂密码（cp /etc/shadow- → /data/.shadow，并 `rm -rf /data/.ssh/*`）。

### 1.5 SSH（内置但默认关闭！）
- `system/bin/dropbear_sshd.sh`：完整 wrapper（`--start` / `--expire=<分钟>` / `--stop`），TAG="SSHD"。
- `/etc/dropbear/dropbear_rsa_host_key` **出厂已预置**。
- 不在启动链里 → 由云端命令远程触发（厂商可远程开 SSH 维护通道）。
- `ps` 里 swnetservice 以 `sshd` 用户身份运行，印证这套远程运维设计。

## 二、运行机制

### 2.1 启动链
```
inittab
 └ rcS: /etc/init.d/S??* (udev/urandom/mountfs/network/NPU)
        + /system/etc/init.d/S??*
        + main.sh
main.sh:  date 010100002020（无 RTC 电池，时间重置）
          → sysctl → 密码校验(§1.3) → net_init(静态 192.168.0.120，实际被 DHCP/.swdb 覆盖)
          → startup.sh → 挂 TF 卡(mmcblk0p1) → start_swapp.sh
start_swapp.sh: export RKFPS=25
          → swsyscmd & → swnetservice & → swapp & → drop_caches.sh & → guard.sh &
```

### 2.2 swapp 框架血统
- 厂商 = **朝歌科技**（defaultparam.txt：`manufacturer=朝歌科技, device_name=朝歌IPC`）。
- 框架从其 **IPTV 机顶盒产品线移植**：字符串里有 `EC2108V5`（华为 IPTV 机顶盒）、`HNCTCIPTVDHCPAAA`（河南有线 IPTV）、`DftHaiSi`（默认配置，**海思遗留**）。
- 媒体层 = rockit UAPI2 + librkaiq（RV1109），业务层 = libsw* 系列（swbase/swmodules/swparameter/swos...）。

### 2.3 参数体系（可改帧率的地方！）
- `system/etc/defaultparam.txt`：出厂默认（`main_stream_size=1440, main_stream_fps=15, sub_stream_fps=15, gop=30`，主/子/三码流架构）。
- **`/data/.swdb/{para,often}`**：运行时 KV 参数库（文本格式），实测内容含
  `main_stream_fps = 15`、`main_stream_gop = 30`、`main_stream_size = 1440`、`access_Key/access_Secret`（云端凭据）等。
- swapp 经 libswparameter 读写 .swdb；APP 改设置就是改这里。
- **我们的 scale2 卡 15fps 的最可能根因就在这**：主码流配置 15fps 拖着整条管线。改法（理论）：改 para 后重启 swapp——但撞看门狗（§四）；或在 APP 云端设置里改码流档位。

### 2.4 环境变量开关（swapp 里确认读 4 个）
| 变量 | 作用 |
|---|---|
| `RKFPS` | start_swapp.sh 里设 25（VI/采集帧率上限） |
| **`RKRTSP`** | ~~RTSP 开关~~ **实测非启动开关**（触发的是通道停止逻辑，见 §3.1） |
| `RKAULOG` / `RKMDLOG` | 音频/算法日志开关 |

### 2.5 双看门狗（事故教训）
1. `guard.sh`：进程级，3 秒轮询 swapp/swnetservice/swsyscmd，缺一 reboot。**可 kill 绕过**。
2. `/dev/watchdog` 硬件看门狗：**由 swapp 自己打开并喂狗**，内核 NOWAYOUT（启用后无法停止）。杀 swapp → 无人喂狗 → **~40 秒后硬重启**。
   ⚠️ 规则：root shell 会话里永远不要 kill swapp。

## 三、隐藏功能排查结果（核心问题）

### 3.1 RTSP：⚠️ 代码存在，但 RKRTSP 实测不是启动开关
**swapp 静态编入了 Rockchip rockit 的 rtsp_demo.c 完整实现**（字符串证据见下）。2026-11-17 部署实测：

- swapp 以 `RKRTSP=1` 拉起（`/proc/<pid>/environ` 已验证变量确实传入）。
- 结果：**无 554/8554 监听、swapp 日志（/tmp/log/cmiot_sdk_log）无任何 rtsp 字样**。
- 逆向确认（objdump）：`getenv("RKRTSP")` → `strtol != 0` → 调用 0x2a204（参数=配置体+8）。
  **0x2a204 的语义是"停止/销毁通道"**：拿句柄 [obj+140] → 调内部销毁函数 0x6a10c → 句柄清零 → 遍历会话数组 → mutex 解锁。没有创建动作。
- 结论：RKRTSP ≠ RTSP enable 开关。rtsp_demo 的**启动路径**另有门控——同一状态机里有 `[obj+12] == 7` 跳过检查、状态值 5/6/7 的流转（疑似"流协议模式"，7 可能就是 RTSP 模式，由 .swdb/para 或云端命令设置）。启动代码在 0x27b50 起的大函数内（`create rtsp demo failed!` 引用自 0x2820c 附近，sw_log 等级3，源码行 331）。
- 字符串证据（仍然成立，代码确实链接进去了）：
  - `rtsp_demo.c`、`[INFO] rtsp server demo starting on port %d`
  - `create rtsp demo failed!`、`create rtsp new session failed!`
  - `id %d is larger than MAX_RTSP_NUM`、会话路径 `sub_stream0/1`、`track1/2`、`rtsp://`
- **下一步候选**（需 swapp 重启/云端配合，谨慎）：① 查 .swdb/para 是否有 proto/mode 字段可设 7；② 研究状态机 0x2cf2c-0x2d110 的模式流转；③ 云命令协议抓包（swnetservice 50661）。

### 3.2 ONVIF：❌ 完全没有
- 全库 grep `onvif`（system/lib 全部 .so + usr + bin + sbin）：**零命中**。
- 没有 gsoap/libonvif/WS-Discovery(3702) 任何痕迹。厂商走私有云协议，不提供标准 ONVIF。

### 3.3 Web 后台：❌ 没有
- swbusybox 无 httpd/telnetd/tftp/inetd applet。
- 无 lighttpd/boa/goahead/nginx 等任何 web 服务器。
- 唯一的"管理面"= 云端私有协议（TCP 36.213.x.x:50661，本地 18686）+ §1.5 的可远程拉起 dropbear SSH。

### 3.4 其他媒体能力
- ffmpeg 全家桶（libavcodec 58 / avformat / avfilter）+ libcurl（含 RTSP 客户端支持）→ swapp 有拉流/转封装能力（用于云回传等）。
- librockx / librknn（NPU AI）、libzbar（二维码）、libOpenVX。

### 3.5 H.265：✅ 三层全支持
| 层 | 证据 |
|---|---|
| 硬件 | RV1109 VEPU 原生支持 HEVC（我们的 h264out 也有 `--codec h265`，RTP PT97） |
| swapp 业务层 | 参数体系按码流独立配置：`vcodec_type`（主）/`sub_vcodec_type`/`third_vcodec_type`；录像 muxer 有 `_H264`/`_H265` 文件后缀分支；云端可改（有 "not allow to set vcodec when enable" 保护）；还有 H265 iframe 加密接口（`iframe_h265_crypt_set_key/iv/open`，移动版权加密用） |
| RTSP demo | SDP 动态载荷 `m=video 0 RTP/AVP %d`，rockit rtsp_demo 支持 RTSP_CODEC_ID_VIDEO_H265 |

当前配置：`/data/.swdb/para` 里 `vcodec_type = 1`、`sub_vcodec_type = 1`。**1 映射 H264 还是 H265 静态无法确定**（枚举在编译代码里）——下次部署可从 swapp 日志（`m_video_cfg.vcodec_type = %d`）+ 云端流 NALU 头实测确认。

意义：① 想省带宽可让 h264out 用 `--codec h265`（码率约省 40%，QGC 端需 h265 解码支持）；② 若 RKRTSP 验证成功且 swapp 输出 H265，QGC 同样需 H265 解码；③ H265 单帧编码耗时略高，但码率减半对缓冲/延迟有利。

## 四、对我们路线的意义

| 发现 | 影响 |
|---|---|
| swapp 内嵌 RTSP 服务 + RKRTSP 开关 | **可能零开发获得标准 RTSP 码流**——下次部署第一优先验证 |
| ONVIF/Web 都没有 | "改成标准摄像头"只能靠 RTSP 路线或自建（h264out 已覆盖 UDP 直推） |
| main_stream_fps=15 在 /data/.swdb/para | 提帧率的正规入口；但改动需 swapp 重启（看门狗风险），或试 APP 设置里的码流档位 |
| 密码校验是三条件回滚 | 想持久改密码需同时伪造 .shadow-，或接受每次开机被回滚（无意义，init=/bin/sh 路线已绕过） |
| dropbear 云命令远程拉起 | 逆向 18686 协议可远程开 SSH，但成本高于现有 init=/bin/sh 方案，暂不做 |
| shadow- 出厂 hash 已提取 | 可上 P104-100 跑 hashcat 离线爆破（低优先级，意义有限） |

## 五、下次部署时的验证清单

1. ~~RKRTSP=1 实验~~ **已完成（2026-11-17）：阴性结果**——RKRTSP 触发的是通道停止逻辑，RTSP 未起。见 §3.1。
2. RTSP 后续候选：查 /data/.swdb/para 的 proto/mode 字段（状态值 7 疑似 RTSP 模式）；或逆向云端命令协议。
3. 帧率实验：改 .swdb/para 的 main_stream_fps=25 → 快速重启 swapp（本 boot 无 guard.sh 进程级看门狗，硬件看门狗由 swapp 持有，重启窗口 <40s 安全）。
