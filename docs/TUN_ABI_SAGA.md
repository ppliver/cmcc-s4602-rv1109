# S4602 / RV1109 TUN.ko ABI 排障全记录

> 目标：为中国移动 S4602（RV1109，vendor 内核 `4.19.111 #47`）编译一个可供 EasyTier 使用的 `tun.ko`，让 EasyTier 以 TUN 模式组网。
> 结论：**已解决并闭环**。最终方案 = 用与 vendor **对齐的 kernel config**（关键是 `CONFIG_IPV6=y`）从头编译，零二进制 patch。裸重启后 bootfix 自动加载 tun.ko + 启动 EasyTier，7 peer 双向收发稳定。
> 本文档记录完整踩坑链，供复现 / 同步 GitHub / 同类平台借鉴。

---

## 0. 结果与交付物（先看结论）

| 项 | 值 |
|---|---|
| 最终模块 | `.stage_tun/tun-vendor-aligned-final.ko`（473144 B） |
| vermagic | `4.19.111 SMP preempt mod_unload ARMv7 p2v8`（精确匹配，无需 patch） |
| 编译方式 | Firefly BSP 4.19.111 源码 + `board/s4602/tun/vendor-aligned.config` + 外部模块方式 |
| 二进制 patch | **无**（早期 p120/p88/p1500 全部废弃） |
| 设备侧落位 | `/data/www/tun.ko`、`/data/www/bootfix.sh`、`/data/www/easytier_ltecar.toml` |
| 实测 | tun0 `10.123.123.77` MTU 1360 UP；7 peer；RX/TX 双向；裸重启自动恢复；无 panic |
| 根因一句话 | 编译 tun.ko 的 config 缺 `CONFIG_IPV6`，导致 `struct net_device` 里 `ndisc_ops` 指针缺失，mtu 字段簇整体前移 4 字节 → 写错偏移 → MTU=0 → 发包失败/硬锁 |

---

## 1. 背景与约束

### 1.1 为什么需要外编 tun.ko
- vendor 内核 `4.19.111 #47 (lunwenkang@sw, gcc 6.3.1 20170404 Linaro)` **没有编入 `CONFIG_TUN`**：kallsyms 零 `tun_` 符号，`/dev/net/tun` 打开返回 ENODEV。
- 但 `CONFIG_MODULES=y` 且 **`CONFIG_MODVERSIONS=n`**（内核无 `__crc_*` 符号）→ 外编模块只要 **vermagic 匹配** 即可 insmod，**不校验符号 CRC**。
- 这既是"能装"的原因，也是后面所有 ABI 灾难的根源：**CRC 不校验结构体字段偏移**，vermagic 匹配 ≠ 结构体布局匹配。

### 1.2 目标 vermagic
从 `/proc/version` + vendor-vmlinux 反推：`4.19.111 SMP preempt mod_unload ARMv7 p2v8`。
（SMP + PREEMPT + ARMv7 + p2v8 = vendor 内核的构建特征。）

### 1.3 拿不到 vendor 源码 / config
- 设备无 `/proc/config.gz`（IKCONFIG 被裁）。
- 固件镜像和 vendor-vmlinux.bin 里搜 `IKCFG_ST` magic 零命中。
- 用户确认无原厂 SDK 渠道。
- → 只能从 vendor 二进制反推 config，用最接近的 Firefly BSP 4.19.111 源码重建。

---

## 2. 完整时间线（踩坑链）

按"尝试 → 现象 → 判断 → 下一步"组织。每一步都是真实实测，不是推理。

### 阶段 A：OpenIPC 编译 —— vermagic 骗人，ABI 踩内存
- GA `build-tun.yml`（OpenIPC linux rockchip-rv11xx + gcc 13.3 + rv1126.generic.config）编出 tun.ko，run `44217983346`，434852 B。
- vermagic 带 `+`（`4.19.111+ ...`，LOCALVERSION dirty），本地 python 二进制左移一字节 patch 掉。
- insmod 成功（dmesg `Universal TUN/TAP driver 1.6`），但 **EasyTier 启动约 1 分钟后整机网络栈僵死**（SSH/webd 全 TCP 超时，仅 ping 活）。
- 判断：OpenIPC 编的 tun.ko 与 vendor 内核 struct ABI 不匹配、踩内存。
- 抢救：`rescue_rm_tun.py` 轮询 SSH，设备恢复瞬间删掉 `/data/www/tun.ko` + `easytier.conf`，阻断 bootfix 自动重载，断电重启救回。

### 阶段 B：Firefly rv1126_defconfig —— vermagic 完美，TUNSETIFF 直接 panic
- 新 GA `build-tun-firefly.yml`：Firefly BSP 4.19.111（tag `linux_release_v2.2.5g`）+ prebuilt gcc-linaro 6.3.1（vendor 同款）+ `rv1126_defconfig`，命令行 `CONFIG_TUN=m KCFLAGS="-DCONFIG_TUN_MODULE"`。run `44222992810`，471396 B。
- **vermagic 完美匹配，无需 patch**；4/4 关键符号在 kallsyms。
- 分步验证：insmod 后观察 90s 稳定（比 A 进步）→ **一启动 EasyTier（哪怕无 peer 的隔离配置，只要 TUNSETIFF 创建 tun 设备）就 panic** → watchdog 硬件重启（uptime 归零）。
- 当时错误结论："同内核源+同工具链也救不了，TUN 不可行"——后被推翻（见阶段 F/G）。

### 阶段 C：抓 panic 栈 —— 锁定 netdev_ops 偏移差 0xc
- 释放串口（COM4 @ 1500000 8N1），抓到决定性 Oops：
  ```
  PC is at register_netdevice+0x78
  Modules linked in: tun galcore(O)
  Comm: tun_probe
  ```
  调用链 `register_netdevice ← tun_chr_ioctl+0x1090 [tun] ← do_vfs_ioctl ← ksys_ioctl`。
- 关键指令 `e594c120` = `ldr r12,[r4,#0x120]` → **vendor 内核认为 `dev->netdev_ops` 在 `dev+0x120`**。
- 反汇编 tun.ko `tun_setup`：`str r5,[r0,#0x2c0?]` → 写 netdev_ops 到 **`dev+0x12c`**（二进制里 `2c 51 80 e5`）。
- **根因铁证：struct net_device 布局差 12 字节（0xc）**，模块写 0x12c、vendor 从 0x120 读 → NULL → panic。
- 二进制 patch `p120`：文件偏移 `0x368c`，`2c 51 80 e5` → `20 51 80 e5`（`str r5,[r0,#0x12c]` → `#0x120`）。

### 阶段 D：p120 —— panic 消失，但 refcount 死锁
- p120 后 tun_probe 全部步骤跑通 `ALL STEPS DONE - kernel survived`（TUNSETIFF/配IP/启用/write 全 OK），致命 panic 消失。
- 但出现 `refcount_t: increment on 0; use-after-free @ tun_attach+0x3d4`，watchdog 仍重启。
- 反汇编 `sock_init_data`：
  - vendor 在 `sk+0x88` 写 1（初始化 `sk_refcnt`）。
  - tun.ko 读 `sk+0x44`（读到 0）→ refcount underflow → close 时 `__tun_detach` 内核态死锁（**D 进程 `kill -9` 都杀不掉**）。
- 二进制 patch `p88`：7 处 `add rX,rY,#0x44` → `#0x88`，字节模式 `44608ae2×2 / 446080e2×1 / 44008ae2×1 / 440084e2×3` 全替换。
- 另一个发现：`sizeof(struct net_device)` vendor 是 `0x500`（1280，从 `alloc_netdev_mqs` 反推 ALIGN 32），tun.ko 认为 4096（`netdev_priv=dev+0x1000`）——但 tun_probe 自洽，暂未致命。

### 阶段 E：p88 —— "up 模式稳 8 分钟"是个陷阱
- p88 + tun_probe2 `up` 模式（创建 tun + 配IP + 启用 + 持有 200s）稳定 8 分钟，无 panic、无 watchdog。
- 当时判断："p88 修好了，可以上 EasyTier"——**错误**。
- EasyTier TUN 模式仍硬锁：串口 `tun0: mtu less than device minimum` 后整机硬锁（ping 丢包、SSH/webd 全挂、串口无输出、sysrq 救不了，只能物理断电）。
- 教训：**up 模式只创建+配IP+持有，MTU=0 从没暴露、没有任何真实流量**。不发包 ≠ 收发包路径对。

### 阶段 F：tun_probe2 write —— MTU=0 死锁，推翻"p88 已修好"
- 关键实验：`tun_probe2 write`，创建 tun + 配IP/MTU + 拉起 + **真实写入一个 IP 包**（走 `tun_get_user`）。
- 结果（p88）：open/TUNSETIFF/SIOCSIFADDR/NETMASK/IFFLAGS UP 全 OK，但 **SIOCSIFMTU FAIL**、`write tun fd n=-1`、dmesg `mtu less than device minimum`。
- **设备这次没硬锁**（uptime 持续增长）——回答了"小程序走同一条路是否崩"：不崩，但功能全废。
- 决定性证据：`ifconfig tunprobe0` 显示 **MTU:0 且永久锁死**，`ifconfig tunprobe0 mtu 1500` → `Invalid argument`，设任何值都不进去。
- → 问题从"神秘硬锁"收敛成一个具体的 ABI 错位：`dev->mtu` / `min_mtu` 字段偏移没对上。

### 阶段 G：反汇编定位 —— 从打地鼠到系统性布局差异
- 工具：本地 Python **capstone 5.0.7** 直接反汇编，无需构建服务器。tun.ko 是 ELF32（.text shndx=2）；vendor-vmlinux.bin 是裸 dump，公式 `VA = file_offset + 0xb00080f0`。
- 早期误判：`dev_set_mtu`(0xb0649a98) 里读写 `dev+0x250` 当 mtu；`__dev_set_mtu`(0xb063e898) 里 `str r1,[r0,#0xac]`。两套偏移一度以为矛盾。
- 尝试 `p1500`：把 tun_setup 里 `mov r4,#0x1f4(500)`→`movw r4,#1500`、`str r4,[r0,#0x250]`→`str r4,[r0,#0xac]`。**结果 FAIL，MTU 仍 0**——因为两处都不是 vendor 真正的 mtu 字段。
- 真正的 mtu 在反汇编 `dev_set_mtu_ext`(0xb0649910) 时才浮出水面：
  - `dev+0x140` = mtu（`ldr r0,[r5,#0x140]` / `str r6,[r5,#0x140]`）
  - `dev+0x144` = min_mtu，`dev+0x148` = max_mtu，`dev+0x120` = netdev_ops，`dev+0x2c` = flags。
- **单点 patch 彻底失败 → 意识到是整个结构体布局系统性错位，不是一两个字段。**

### 阶段 H：布局探测模块 —— 4 字节差异现形
- 写 `layout_probe.c`：把 `offsetof(struct net_device, ...)` 作为编译期常量放进 `.rodata`，用 kbuild 外部模块方式编译后 **离线 objdump 读出**，不用 insmod、不用重启。
- fl-kernel 极简 config 实测偏移 vs vendor 反推：

  | 字段 | fl-kernel（失败 config） | vendor #47 | 差 |
  |---|---|---|---|
  | netdev_ops | 0x120 | 0x120 | 0 |
  | **mtu** | **0x13c** | **0x140** | **+4** |
  | min_mtu | 0x140 | 0x144 | +4 |
  | max_mtu | 0x144 | 0x148 | +4 |

- 差异**恰好 4 字节**：vendor 在 netdev_ops 之后多一个 4 字节指针。

### 阶段 I：根因锁定 —— CONFIG_IPV6
- 查 4.19 `include/linux/netdevice.h`，`netdev_ops` → `mtu` 之间的条件编译字段：
  ```c
  netdev_ops;             // 0x120
  ethtool_ops;
  #ifdef CONFIG_NET_SWITCHDEV   switchdev_ops;
  #ifdef CONFIG_NET_L3_MASTER_DEV l3mdev_ops;
  #if IS_ENABLED(CONFIG_IPV6)   ndisc_ops;    // ← 就是这 4 字节
  #ifdef CONFIG_XFRM_OFFLOAD    xfrmdev_ops;
  #if IS_ENABLED(CONFIG_TLS_DEVICE) tlsdev_ops;
  header_ops;
  ...
  mtu;
  ```
- 铁证：
  - vendor kallsyms 大量 `inet6_*` / `ipv6_*` 符号 → **vendor 开了 IPV6**。
  - fl-kernel `.config` 是 `# CONFIG_IPV6 is not set`。
- p1500 失败也解释通了：之前写到 `0xac`（被 `__dev_set_mtu` 误导），vendor 真正 mtu 在 `0x140`。

### 阶段 J：同树 config 重建 —— 零 patch 成功
- 在构建服务器（192.168.219.177，zhang/admin888，`~/fl-kernel` = 4.19.111 完整源码）上：
  1. `sed -i 's/# CONFIG_IPV6 is not set/CONFIG_IPV6=y/' .config`
  2. **`make syncconfig ARCH=arm`**（关键坑：必须重生成 `include/generated/autoconf.h`，否则编译仍用旧 header，偏移不变）
  3. 外部模块方式：`make M=/home/zhang/tbuild modules ARCH=arm CROSS_COMPILE=/usr/bin/arm-linux-gnueabihf- KCFLAGS="-DCONFIG_TUN_MODULE -march=armv7-a"`
- 产物 `tun-ipv6-aligned.ko`，473144 B，**vermagic 精确匹配，零二进制 patch**。
- 布局探测复核：打开 IPV6 后 mtu 从 0x13c → **0x140**，min 0x144、max 0x148，全部对齐 vendor。

### 阶段 K：上机实测 —— 四层全通
1. `tun_probe2 write`：`SIOCSIFMTU` ✅、`write tun fd n=64` ✅（之前 n=-1）、MTU=1380（之前锁死 0）、probe 90s 自然结束、进程可退出、refcount 归 0（p88 时代 kill -9 都杀不掉）。
2. 隔离 TUN 测试：EasyTier-mini 无 peer 配置 → `tun0 UP POINTOPOINT RUNNING MTU:1380`、`tun device ready`、dmesg 无异常。
3. 真实 LTE_CAR 配置：tun0 `10.123.123.77` MTU 1360，**7 peer**，RX/TX 双向（20/31），稳定 15 分钟，无硬锁。
4. **裸重启自验**（sysrq 硬重启）：bootfix 自动 insmod tun.ko + 起 EasyTier，tun0 UP、7 peer、双向收发，webd/dropbear/swapp_patched/h264out_v4 全恢复，dmesg 无 panic/mtu less。

---

## 3. 踩过的坑清单（按类别）

### 3.1 ABI / 内核结构体
| 坑 | 现象 | 根因 | 解法 |
|---|---|---|---|
| vermagic 匹配 ≠ ABI 匹配 | insmod 成功但EasyTier 建设备即僵死/panic | MODVERSIONS=n，CRC 不校验字段偏移 | 必须对齐编译 config |
| netdev_ops 偏移差 0xc | `register_netdevice+0x78` NULL deref panic | 模块写 dev+0x12c，vendor 读 dev+0x120 | p120 patch（临时）/ 对齐 config（最终） |
| sk_refcnt 偏移差 0x44 | refcount_t increment on 0 + D 进程杀不掉 | vendor sk+0x88，模块 sk+0x44 | p88 patch（临时）/ 对齐 config（最终） |
| MTU 锁死 0 | SIOCSIFMTU FAIL、write -1、`mtu less than device minimum`、硬锁 | 缺 IPV6 → mtu 字段前移 4 字节 | `CONFIG_IPV6=y` 重编（最终解） |
| "up 模式稳定"假象 | 误判 p88 已修好 | up 模式不发包，MTU=0 不暴露 | 必须测 write 真实发包路径 |
| p1500 单点 patch 无效 | 改 mtu 偏移 MTU 仍 0 | 写错字段（0xac 不是真正 mtu） | 放弃打地鼠，转 config 重建 |

### 3.2 构建环境
| 坑 | 现象 | 解法 |
|---|---|---|
| `.config` 改了不生效 | 打开 IPV6 后偏移不变 | 必须 `make syncconfig` 重生成 `include/generated/autoconf.h`（kbuild 用 autoconf.h，不是 .config） |
| kbuild 脚本要 `python` | ubuntu/服务器只有 python3 | `ln -sf $(which python3) ~/bin/python`，PATH 前置 |
| 直接 `make drivers/net/tun.ko` 触发 dtc 失败 | gcc10 `-fno-common` 多重定义 | 改外部模块方式 `make M=<dir> modules` 绕开 dtc |
| `CROSS_COMPILE` 路径 | gcc 在 /usr/local/bin 是指向 /usr/bin 的软链，ld/objcopy 只在 /usr/bin | 用 `CROSS_COMPILE=/usr/bin/arm-linux-gnueabihf-` |
| ARM 指令集 | /usr/bin gcc 默认 armv5，缺 ARMv7 指令 | `KCFLAGS` 加 `-march=armv7-a` |
| OpenIPC LOCALVERSION 带 `+` | vermagic `4.19.111+ ...` | patch 掉 / 或换 Firefly 树（原生匹配） |

### 3.3 设备侧 / 工具链
| 坑 | 现象 | 解法 |
|---|---|---|
| 无 wget/nc/tftp applet | HTTP 传大文件失败 | 用 `scp -O`（vendor busybox ssh 通道） |
| SSH 免密每次重启丢 | /root bind mount 掉 | webd exec `mount --bind /data/dropbear/root_home /root` 重建 |
| 进程 `kill -9` 杀不掉 | ABI 错位导致内核态死锁 | 只能有管理 reboot 清理 |
| busybox 功能残缺 | 无 nohup/cut/md5sum/dtc | 后台用 `&`+重定向；复合命令避免 awk/cut；`-c` ping 需管理员权限（噪音） |
| 保险 flag | bootfix 的 `.tun_insmod_failed` 会阻止加载 | 部署前删 flag；保留作失败自保护 |

---

## 4. 可复现构建（本地 / GA）

### 4.1 固化输入
- 内核源码：Firefly `git clone --depth 1 -b rv1126_rv1109/linux_release_v2.2.5g https://gitlab.com/firefly-linux/kernel.git`
- 工具链：Firefly prebuilt gcc-linaro 6.3.1（同 vendor）
- config：`board/s4602/tun/vendor-aligned.config`（**本次从成功构建机拉取固化，3600 行，`CONFIG_IPV6=y`**）

### 4.2 构建命令
```sh
cp board/s4602/tun/vendor-aligned.config /opt/fl-kernel/.config
cd /opt/fl-kernel
export ARCH=arm
export CROSS_COMPILE=/usr/bin/arm-linux-gnueabihf-   # GA 用 Firefly prebuilt 路径
export PATH=$HOME/bin:$PATH                          # python 软链
make syncconfig ARCH=arm                             # 或 olddefconfig，重生成 autoconf.h
mkdir -p /tmp/tbuild
cp drivers/net/tun.c /tmp/tbuild/
printf 'obj-m += tun.o\n' > /tmp/tbuild/Makefile
make M=/tmp/tbuild modules ARCH=arm CROSS_COMPILE=$CROSS_COMPILE \
     KCFLAGS="-DCONFIG_TUN_MODULE -march=armv7-a"
strings /tmp/tbuild/tun.ko | grep '^vermagic='        # 期望 4.19.111 SMP preempt mod_unload ARMv7 p2v8
```

### 4.3 布局自检（强烈建议纳入 CI）
把 `layout_probe.c`（`offsetof` 常量放 `.rodata`）一起编译，objdump 读出，断言：
```
netdev_ops == 0x120
mtu        == 0x140
min_mtu    == 0x144
max_mtu    == 0x148
```
任一不符 → 直接 fail，避免再次产出"能装但崩"的模块。

---

## 5. GitHub Actions 可行性评估

**结论：可以，且建议做。** 详见 workflow `build-tun-vendor-aligned.yml`。

- 现有 `build-tun-firefly.yml` 用 `rv1126_defconfig`——正是导致 netdev_ops 错位、需要 p120 patch 的那套 config，**不能直接用**。
- 新 workflow 用固化的 `vendor-aligned.config`，从源码拉起到编译全在 GA 完成，产出 artifact 前做 vermagic + 布局双重自检。
- 唯一注意：GA 上 gcc-linaro 6.3.1 是公开仓可拉（现成 workflow 已验证），无需 NDA。`-march=armv7-a` 用 Firefly prebuilt 工具链默认就带，无需额外处理。

---

## 6. 关键证据文件

| 文件 | 内容 |
|---|---|
| `docs/evidence/vendor-vmlinux.bin` | vendor 裸内核 dump，`VA=file_off+0xb00080f0` |
| `docs/evidence/vendor-kallsyms.txt` | vendor 全量符号（31524 行），含 inet6_/ipv6_ 证明 IPV6=y |
| `docs/evidence/serial_panic_capture_20261118.log` | `register_netdevice+0x78` panic 栈 |
| `docs/evidence/serial_*tun*.log` | 各轮 TUN 硬锁 / 测试串口日志 |
| `.stage_tun/tun-firefly*.ko` | p120 / p88 / p1500 各版二进制补丁（历史） |
| `.stage_tun/tun-vendor-aligned-final.ko` | 最终成功模块 |
| `board/s4602/tun/vendor-aligned.config` | 成功构建用的内核 config（GA 输入） |
| `.workbuddy/memory/2026-11-18.md` | 逐时段实测记录（本报告素材来源） |

---

## 7. 一句话教训

> `vermagic` 匹配 + 符号齐全 + insmod 成功 + 能创建设备 + 能配 IP，**都不等于 ABI 兼容**；真正的试金石是**真实收发包**。而结构体布局由**编译 config**决定，不是工具链版本——差一个 `CONFIG_IPV6` 就是 4 字节偏移、就是 MTU=0、就是整机硬锁。遇到这类问题，与其逐字段二进制 patch（打地鼠），不如**反推 config 用同树从头重编**。
