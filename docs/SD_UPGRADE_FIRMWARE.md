
## 9. 文件来源与 GitHub Actions 复现（2026-11-18）

### 9.1 每个文件的来源（provenance）

| 文件 | 来源 | 状态 |
|---|---|---|
| `factory/system_b.img` | **原厂 mtd9 备份**（crc32 0x38df701f，46137344B），本次入库为唯一镜像源头 | git 已入库 |
| `flash/system_b.img`（钩子 v2） | `upgrade/build_hook_image.sh` 从 factory 全链重建（解包→注入 S99local v2→mksquashfs→ubinize→0xFF 填充） | ubireader 全绿：reserved_pebs=332 / seq 1734217813 / 尾部 0xFF；S99local 与 hook-src diff 逐字节一致；362=361 原厂+1 文件 |
| `base/bootfix.sh` | 本地编写 v7 | 真机 busybox `sh -n` + 幂等试跑全绿 |
| `base/webd` | 源码 `tools/webd.c`（本库，曾漏提交已补）→ 静态 armhf。GA 步骤重建 + artifact；`use_rebuilt_bins` 可选替换 | 实机在役验证 |
| `base/h264out_v4` | 源码 `tools/h264out.c`（本库）+ `third_party/mpp/inc-1.0.9/`（vendor MPP 头，已入库）→ PIE 动态 armhf（`-ldl -lpthread -lm`，运行时 MPP 库由 rtlib.tar.gz 提供）。GA 步骤重建 + artifact；`use_rebuilt_bins` 可选替换 | 实机在役验证 |
| `base/swapp_patched.xz` | `tools/patch_swapp.py`：对入库基底 `upgrade/factory/swapp_runtime.bin`（设备 /system/bin/swapp，md5 2b7dbb15...，**注意不是 squashfs 里 630KB 那个**）做 3 处 ARM 字补丁（0xA804C 强制线性 / 0xA7FD8、0x882AC nop 掉 HDR 帧率上限，file offset = vaddr−0x10000，小端存储字节）。**字节确定性，重建 md5 488b51c2... == 实机版 IDENTICAL**，GA 每次自动验证 | 实机在役验证 |
| `base/webd` `index.html` `h264out_v4` `rtlib.tar.gz` `swapp_patched.xz` `np_resetpasswd.sh` `shadow.md5` | 实机回拉基线（scp /data/www@192.168.6.214）；webd/h264out/swapp_patched 的可复现构建见上表 | 实机在役验证 |
| `base/stream.conf` | 本地编写（实机配置复刻） | 同款参数 |
| `base/modules.conf` | 本地编写（可选模块开关，本次新增） | bootfix v7 消费 |
| `optional/go2rtc.xz` | GA 构建（workflow build-go2rtc-patched，v1.9.15+close+flush-guard 增强 patch） | 实机 3+5 轮播放/压力 0 panic |
| `optional/go2rtc.yaml` `easytier_ltecar.toml` | 实机回拉 | 实机在役 |
| `optional/rtp2pipe` | 源码 `tools/rtp2pipe.c`（本库）经构建服务器 `arm-linux-gnueabihf-gcc -O2 -std=gnu99 -w -static -s` 编译（`tools/armbuild.py` SSH 辅助，当时未走 GA）。GA 复现：build-sd-upgrade 内 "Rebuild rtp2pipe from source" 步骤，产物单独 artifact；包内默认保留实机验证版（gcc 版本差异字节会漂移），dispatch 输入 `use_rebuilt_rtp2pipe` 可切换为新编版 | 实机在役（go2rtc exec 源） |
| `optional/rtp2pipe` `tun.ko` `easytier-mini` | 实机回拉（tun.ko=GA 构建的 vendor ABI 对齐版；easytier-mini 落 data_bk） | 实机在役 |
| `VERSION` `MANIFEST.sizes` `MANIFEST.md5` `README.txt` | `tools/make_sd_upgrade.py` 生成 | — |

### 9.2 GA 一键复现

workflow：`.github/workflows/build-sd-upgrade.yml`（手动 dispatch + upgrade/** 推送触发），
ubuntu-latest 上从 git 仓库零外部依赖复现整包：

```
apt squashfs-tools mtd-utils + pip ubi-reader
→ bash upgrade/build_hook_image.sh        # factory 镜像 → 钩子 v2
→ ubireader 几何校验（332/seq/0xFF）
→ python3 tools/make_sd_upgrade.py        # 组装 base/optional + MANIFEST
→ 布局断言（system_b.img 在根、base=10、optional=6、bootfix 语法）
→ artifact: s4602_sd_upgrade_<run>（= SD 卡 FAT32 根内容，可直接拷卡）
```

**CRC 漂移说明**：mksquashfs 确定性打包，但输入树的文件 mtime 等元数据随解包环境变化，
不同环境产物 crc32 会与参考值 0x45bcc5e9 不同（等价镜像）。正确性以硬门为准：
46137344B + 尾部 0xFF + reserved_pebs=332 + seq 1734217813 + S99local 逐字节一致
（本地复现版 crc32=0x7d4915db 已过全部内容校验）。`make_sd_upgrade.py` 的 CRC 检查为
提示性软门，几何门为硬门。

### 9.3 交叉编译 time ABI 陷阱（2026-11-19 实机踩坑，务必遵守）

**症状**：用新版工具链编出的 `h264out_v4` 一启动就死，日志：

```
[v4l2] REQBUFS DMABUF got 6
QBUF: Inappropriate ioctl for device
[FATAL] STREAMON failed
```

**根因**：armhf 的 **time ABI**。glibc 2.34+ 的 armhf 头默认 `_TIME_BITS=64`，
于是 `ioctl`/`clock_gettime`/`select` 全部改走 64 位 time 版本
（`__ioctl_time64` / `__clock_gettime64` / `__select64`）。而 v4l2 的 ioctl
请求码是宏展开的**编译期常量**（`_IOWR('V', 0x12, ...)` 里嵌了 `sizeof(struct timespec)`），
time 结构体从 8 字节变 16 字节后请求码整体改变 → 内核不认 → `VIDIOC_STREAMON` 返回 EINVAL。

**验证**：设备已验证版（gcc 9.5）链接 `ioctl`/`clock_gettime`/`select`；
新版 gcc 编出的链接 `__ioctl_time64`/`__clock_gettime64`/`__select64` → 启动即挂。

**修法（不要用 `-D_TIME_BITS=32`）**：实测在 gcc 9.5 上加
`-D_TIME_BITS=32 -D__USE_TIME_BITS64=0` 反而**会打开** time64（与直觉相反）。
正确做法是**用工具链默认行为编译，再用符号门控卡住**：

```bash
arm-linux-gnueabihf-gcc -O2 -std=gnu99 -w -s -o h264out_v4 tools/h264out.c \
  -Ithird_party/mpp/inc-1.0.9 -ldl -lpthread -lm
# 门控（已内置进 .github/workflows/build-sd-upgrade.yml）：
strings h264out_v4 | grep -qE '^__ioctl_time64$|^__clock_gettime64$' && { echo "FATAL: 64-bit time ABI"; exit 1; }
strings h264out_v4 | grep -qE '^ioctl$' || { echo "FATAL: 缺 32-bit ioctl"; exit 1; }
```

`base/webd`（静态）同样加了这道门。若 CI 换到新工具链导致门控失败，
需要换回 gcc ≤ 12 的 armhf 工具链，不要靠加 `-D` 硬压。

### 9.4 `--mirror` 参数的显示假象（2026-11-19，勿再误判）

旧版 `h264out.c` 解析 `--mirror IP:PORT` 时用 `*colon = 0` **原地改写 argv**，
把 `"127.0.0.1:5602"` 的冒号替换成 NUL。于是 `/proc/<pid>/cmdline` 读出来
天然是 **两个** NUL 分隔的 token：

```
--mirror
127.0.0.1
5602
```

`ps w` 进一步把它显示成 `--mirror 127.0.0.1 5602`（空格形式），
看起来像"参数形式写错了 → mirror 失效"。**这是假象**：实际传进去的
就是单 token `127.0.0.1:5602`，mirror 一直生效。

已修：改为拷到局部 buffer 再解析，`/proc/<pid>/cmdline` 现在正确显示
`--mirror` → `127.0.0.1:5602`（单 token 带冒号）。

**判据纪律**：判断 mirror 是否工作**不要看 cmdline**，要看
**rtp2pipe 是否真的收到字节**（`/data/www/rtp2pipe 5602 > /tmp/r2p.out`，
5 秒内应有 ~1.7MB）。诊断脚本 `out/mirr.sh` 已按此重写。

### 9.5 camera1 播放链路验证方法（go2rtc + WebRTC）

**信令 API 在 1984，不在 8555**。8555 是 WebRTC **媒体端口**（只走 DTLS-SRTP 加密 RTP），
对它发 HTTP/TLS 会被直接 RST（"Recv failure: Connection was reset"）。正确端点：

```
POST http://<device>:1984/api/webrtc?src=camera1     (Content-Type: application/sdp)
→ 201 Created + answer SDP
```

**端口矩阵**：1984=API/WebUI（信令）、8554=RTSP、8555=WebRTC 媒体(TCP+UDP)。

**验证脚本**（本库）：
- `tools/check_camera1.py` — RTSP DESCRIBE/PLAY 抓码流 + WebRTC offer
- `tools/check_webrtc_e2e.py` — offer→answer→codec 协商→ICE candidate 可达性

**通过的判据**：
| 项 | 期望 |
|---|---|
| RTSP DESCRIBE | `a=rtpmap:96 H264/90000` + `sprop-parameter-sets` |
| RTSP PLAY | 抓到 >60KB interleaved RTP |
| WebRTC offer | HTTP 201，answer 含 `a=rtpmap:96 H264/90000` |
| HLS 主列表 | `CODECS="avc1.42001F"`（H264 Baseline） |
| HLS 分片 | TS 同步字节 0x47 + PES `00 00 01 E0` |

**浏览器不认 H265**：Chrome/Edge 的 WebRTC 只支持 H264/VP8/VP9/AV1。
`stream.conf` 里 `codec=h265` 时报 `codecs not matched: video:H265,...`。
**必须 `codec=h264`**（用 WEB `/api/stream?codec=h264` 切，或改 `/data/www/stream.conf`）。

**注意**：`rtp2pipe` 是 go2rtc 的 `exec:` producer，**只在有消费者时才被拉起**，
`/api/streams` 里 `consumers:null` 时看不到它是正常的，不是故障。
`/api/frame.jpeg` 报 `ffmpeg: executable file not found` 也与播放无关
（设备未装 ffmpeg，快照功能不可用而已）。

