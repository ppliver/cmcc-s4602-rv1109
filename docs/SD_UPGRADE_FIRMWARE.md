# S4602 SD 卡升级固件（原厂移动固件增强包）

**版本 20261118.1 · 2026-11-18 · 在原厂移动固件（Linux 4.19.111 #47, 千里眼）上实现**

把散装部署固化为两层结构的正式升级固件：

| 层 | 内容 | 落点 | 开关 |
|---|---|---|---|
| **base 基础增强** | h264out UDP 推流 + webd WEB 后台 + 自愈框架（SSH/shadow/看门狗接管/swapp 换血） | 随首刷自动装入 /data/www | 始终启用 |
| **optional 可选模块** | go2rtc v1.9.15（mp4 panic 增强补丁版）+ easytier-mini v2.7.0 TUN 组网（vendor ABI 对齐 tun.ko） | go2rtc→/data/www；easytier-mini→data_bk | `/data/www/modules.conf`（go2rtc/easytier 各自 =0/1） |

原厂 **boot_b / U-Boot / 内核 / system_a 完全不动**，只刷 system_b(mtd9) 一个分区——与
2026-11-17 的钩子镜像同一条已验证红线（`docs/VENDOR_ROUTE_A_PLAN.md`）。

## 1. 核心设计：两段引导，解决"先有鸡还是先有蛋"

全新原厂机的 /data/www 是空的，原钩子镜像（S99local 只拉 /data/www/bootfix.sh）装上后什么都不会发生。
升级固件分两段解决：

```
U-Boot(fatload SD) 刷 system_b 钩子镜像 v2
        │
        ▼ 首次上电
S99local.sh v2 (squashfs, 只做一件事)
  └─ /data/www/bootfix.sh 不存在 → 从 SD 引导拷贝 bootfix.sh → 后台拉起
        │
        ▼ bootfix v7 阶段 0（此后每次开机都跑）
SD 同步：VERSION 门控 + MANIFEST.sizes 尺寸门控（busybox 无 md5sum）
  ├─ base/*  → /data/www/（临时拷贝+rename 原子替换，bootfix 自身用 mv 保 inode）
  ├─ optional/* → /data/www/（easytier-mini 除外 → data_bk，20.7MB 出厂空分区）
  └─ 校验失败 → ABORT 保留现有安装；成功 → 记 .upgrade_version，bootfix 已更新则 exec 重入
        │
        ▼ 自愈链（顺序为实测红线，不可调换）
fast wins(shadow/dropbear/webd/DNS) → 等 swapp → 换血(亚秒间隙,喂狗接管)
  → 35s IQ 稳态 → 存活复核 → h264out(按 stream.conf) → go2rtc(+守护) 
```

**升级体验**：首刷之后所有升级 = 改 SD 卡（新 VERSION + 新文件）→ 重新上电。不再需要串口。

## 2. SD 卡结构（FAT32，`out/s4602_sd_upgrade/` 即卡根目录）

```
system_b.img                  ← 必须在 FAT 根（U-Boot fatload 用）
s4602_upgrade/
├── VERSION                   ← "20261118.1"，升级门控
├── MANIFEST.sizes            ← 设备端尺寸门控（name size 每行）
├── MANIFEST.md5              ← 主机侧参考
├── README.txt                ← 卡上说明
├── flash/system_b.img        ← 镜像副本（归档）
├── base/                     ← 基础增强（10 文件，~4.0MB）
│   bootfix.sh  webd  index.html  h264out_v4  stream.conf
│   rtlib.tar.gz  swapp_patched.xz  np_resetpasswd.sh  shadow.md5  modules.conf
└── optional/                 ← 可选模块（6 文件，~10.8MB）
    go2rtc.xz(增强补丁版 5.45MB)  go2rtc.yaml  rtp2pipe
    tun.ko(vendor 对齐版)  easytier-mini(4.4MB)  easytier_ltecar.toml
```

全部二进制**逐一从实机验证过的部署件回拉**（scp 自 192.168.6.214），非本地猜测版本：

| 文件 | 字节 | 来源/验证 |
|---|---|---|
| h264out_v4 | 67444 | 实机 720p25 稳定推流 + --mirror 5602 |
| webd | 464044 | 实机 :8080 /api/status|stream|cloud|log|exec |
| swapp_patched.xz | 969416 | 实机换血+喂狗（bootfix 第4/5步铁律配套） |
| rtlib.tar.gz | 2293151 | glibc armhf 运行库，h264out 依赖 |
| go2rtc.xz | 5454732 | GA run 44258779862 增强补丁版，mp4 3+5 轮压力 0 panic |
| tun.ko | 473144 | vendor ABI 对齐版（CONFIG_IPV6=y，net_device mtu@0x140），7 peers 无硬锁 |
| easytier-mini | 4482312 | v2.7.0 armhf，实机 TUN 模式 |

## 3. 钩子镜像 v2（upgrade/s4602_upgrade/flash/system_b.img）

- **46137344B（0x2C00000 满 mtd9）**，crc32 **0x45bcc5e9**，尾部 0xFF 填充 ✓
- `reserved_pebs=332` 与原厂逐字段一致（vol_size=42156032）——**UBI PEB 预算红线**，
  352 会导致 `not enough PEBs` 内核 panic（2026-11-17 事故教训）
- ubinize `-p 131072 -m 2048 -Q 1734217813`、动态卷 vol_id=0 rootfs、mksquashfs `-comp xz -b 131072`
  全参数复刻；ubireader 校验：Image seq 1734217813 / vol_id=0 / dynamic / alignment=1 ✓
- 内容校验：unsquashfs 回读——S99local v2 (1406B, 0755) 在位，原厂 swapp/librkaiq/resetPasswd 完好（2023 时间戳未动）
- 重建脚本 `upgrade/build_hook_image.sh`（WSL 离线），校验脚本 `upgrade/check_hook_image.sh`

## 4. 安装流程

### 4.1 全新原厂机首装（一次性，串口 + 主机）

1. `python tools/make_sd_upgrade.py` 打包 → `out/s4602_sd_upgrade/`
2. SD 卡主机端格式化 FAT32（**必须新卡或重格**，当前实验卡 FAT 已损坏），拷入全部内容
3. 串口 1500000 8N1，断电上电截停 U-Boot：
   ```
   python tools/sd_upgrade_flash.py          # DRY-RUN：截停 + 3 项只读校验
   python tools/sd_upgrade_flash.py --go     # 写 mtd9 + 回读 CRC 双向比对 + reset
   ```
   （`sd_upgrade_flash.py` = `flash_mtd9_only.py` 同一引擎，镜像指向升级包；默认不写）
4. 首启约 60s 自愈链自动完成；验证：`http://<设备IP>:8080`、UDP 5600 收流、`:1984` go2rtc

### 4.2 存量设备（已刷钩子镜像 v1 crc 0xb0256154）

钩子 v1 只拉 /data/www/bootfix.sh，v2 钩子才带 SD 引导。两种路径：
- **推荐**：重刷 v2 镜像（同 4.1），之后获得"换 SD 即升级"能力
- **过渡**：手工 scp bootfix v7 到 /data/www（设备侧自会做 SD 同步之外的一切）

### 4.3 后续升级（无串口）

改 SD 卡 s4602_upgrade（VERSION 变更）→ 重新上电。阶段 0 自动同步。SD 可拔。

### 4.4 回滚

U-Boot 下用原厂 `system_b.img`（crc 0x38df701f, md5 458bc1e3）fatload +
`mtd write spi-nand0 <addr> 0x03A80000 0x02C00000` → 完全出厂态。
全片 256MiB 备份（backup/snapshot/full-256M.img）为最后保险。

## 5. bootfix v7 相对实机版（v6 系）的差异

| 项 | 说明 |
|---|---|
| +阶段 0 SD 同步 | VERSION 门控 + 尺寸门控 + 原子替换 + bootfix 自更新 exec 重入 |
| +go2rtc 守护 | 20s 探活循环，崩了自动重启，每 boot 最多 10 次（mp4 panic 运维缺口收口） |
| +DNS 修复 | /etc/resolv.conf→/tmp（tmpfs）重启即丢；开机补写 223.5.5.5/114.114.114.114（实机已验证写入） |
| +modules.conf | 可选模块开关（go2rtc=0 则连守护都不装；easytier=0 不 insmod tun.ko） |
| easytier 路径 | /tmp/bk(data_bk) → /data/.swdb_bk → /data/www 依次探测 |
| 其余 | 与 2026-11-18 实机全绿版逐行一致（换血时序/IQ 稳态窗/shadow 守卫） |

已在实机 busybox 验证：`sh -n` PASS；幂等试跑全绿（日志 `=== bootfix v7 done ===`，全进程无扰动）。

## 6. 已知事项 / 待办

- **实验卡 SD FAT 损坏**（mmcblk1 clusters badly computed）：装机前必须主机重格。
- **首装全链实测未做**：需要一张好 SD + 用户执行 `--go`（设备当前是在役开发机，未经批准不动 flash）。
- webd 存在双实例（PID 201/966，历史遗留）；v7 幂等逻辑保证重启后单实例。
- TUN 模式在原硬锁根因（CONFIG_IPV6 布局）解决后已稳定，但 `easytier_ltecar.toml` 的
  `mtu=1380`、UDP peer us01.225284.xyz 不通（TCP peers 正常）仍为已知边界。
- swapp_patched 智能侦测固定开销 ~30% 单核（云端 HEMU 强制下发，改不掉，已评估可接受）。

## 7. 原厂升级通道考古（2026-11-18，对话检索 + 二进制铁证）

**对话记录**：conversation_search 3 次检索（SD 卡升级/TF 刷机/U-Boot bootdelay）0 命中——
历史上从未讨论过"原厂预留 SD 升级"，此前刷机一直是串口 fatload+mtd write 手工路线。

**U-Boot 层：无 SD 升级预留**。`bootcmd=boot_fit;boot_android ${devtype} ${devnum}`，
boot_fit 从 mtd boot 分区读 FIT 且恒成功（boot_android 是 fallback 走不到）；无 saveenv。

**但 swapp 运行时层有完整原厂升级通道**（二进制字符串铁证）：
- SD 卡路径：swapp 扫描 SD 卡目录找 **`security_boot_update.zip`**（`open %s dir err` +
  `scandir` + `not dir need to scan`）；云端 OTA 走 **FTP**（`ftp://user:pass@host/` +
  ftpget）下载到 `/usr/upgrade/%s` → `/tmp/update.zip`
- **签名硬门**：`swsignature` whole-file RSA(e=3) 验签，硬编码公钥指纹
  `37FCE8CBE38705C2783C9085FA4D5A6E54310292667C8E00`——无私钥，自签包必被
  `swsignature not match !!` 拒绝
- 执行链：杀 swapp/swnetservice/swsyscmd → `/system/etc/init.d/recovery.sh`（等 swapp
  退出，8s 强杀）→ `/usr/sbin/recovery --update_package=/tmp/update.zip` → Android
  recovery 式解包，**按分区名**（/proc/mtd 的 boot_b/system_b...）MTD 写入：坏块规避、
  回读校验、UBI 卷支持（`mtd_write_ubi_data`/`ubi_clear_volume`）、`RSA_e_3_verify` 整包
  验签；`upgrade_security_uboot` 串证实**连 U-Boot 都能刷**；状态落 /data/status、/data/progress
- 本机实测：SD 挂载点 = **/tmp/udisk0**（mmcblk1；startup.sh 挂的是 mmcblk0→/tmp/sdcard，
  本机 mmcblk0 不存在所以 /tmp/sdcard 恒空）；`/usr/sbin/recovery`、`recovery.sh`、
  `/usr/upgrade` 均在位；mount 带 `noexec`（SD 上不能直接跑二进制，拷贝不受影响）

**结论**：原厂 SD 升级通道存在且活着，但被 RSA 签名硬门挡住（拿不到原厂私钥即无法自签包）。
对我们的 bootfix 方案而言它只是"可选未来增强"（除非逆向 recovery 验签漏洞），不改变现方案；
写包格式时可参考其"按分区名的 zip"结构。

## 8. recovery 验签逆向（2026-11-18，用户指令"先试试逆向 recovery 验签"）

目标：判定全新原厂机能否免串口走原厂 SD 通道刷自制固件。结论先行：**不可行，验签链闭合**。
分析产物在 `out/re_analysis/`（disasm.txt / verify_file.txt / install_flow.txt / rsa_e3.txt /
swmodules_disasm.txt / swapp_updatesys2.txt 等），反汇编由 WSL `arm-linux-gnueabihf-objdump` 生成。

### 8.1 recovery 侧（usr/sbin/recovery，150KB stripped ARM，crypto 全静态内联）

`main(0x14e88) --update_package→ install_package(0x16624) → really_install_package(0x15d44..)
→ load_keys("/etc/keys") + verify_file(0x17df8)`：

- **load_keys 失败 → return 2 中止；verify_file 非零 → return 2 中止**。无任何跳过路径。
- `/etc/keys` 是 squashfs 内的**文本格式** mincrypt RSAPublicKey（`v2 {64,0x137d4afb,{n[64]},{rr[64]}}`）。
  已解析并数学验证：**RSA-2048，e=3，单钥**（rr = R² mod n ✓，n0inv = -n^-1 mod 2^32 ✓）。
- `RSA_e_3_verify(0x255cc)`：签名 256B，先 `key->len==64`、`e==3` 硬校验，模幂后
  **前 236 字节与 rodata 0x303b4 常量表逐字节比对**（即 `00 01 FF×215 00` 完整 PKCS#1 v1.5
  填充），后 20 字节与 SHA-1 摘要比对。**填充检查逐字节严格 → Bleichenbacher e=3 伪造不可行**。
- `verify_file`（老 AOSP whole-file 方案）：
  - 末尾 6B footer `[B_lo,B_hi][FF FF][A_lo,A_hi]`：A=comment_size（决定 EOCD 位置与签名区）、
    B=签名长度（仅做 ≥262 检查）；
  - SHA-1 覆盖 `[0, eocd_start+20)`——**EOCD 前 20 字节在内，包含 central directory offset/size**
    → 注释区（哈希区外）无论怎么改都重定向不了解包器，签名复用攻击无效；
  - 签名 = comment 末尾 256B；逐 key 尝试；
  - 防注释区藏第二个 EOCD 的扫描（i=4..eocd_size-3 找 PK\x05\x06）。
- 交叉验证：recovery 里**没有** uboot 直刷/独立验签旁路（无 upgrade_security_uboot 字符串）。

### 8.2 swapp 侧（sw_upgrade_file_verify → libswmodules.so swsignature）

- swapp 对 SD 包（security_boot_update.zip→/tmp/update.zip）的验签 = libswmodules
  `sw_swsignature_fp`：**HMAC-SHA256**（`sw_signature_create/update/final` 三个 72B 包装
  直透 `sw_hmac256_*`，mbedtls `sha2_hmac_starts/finish`），密钥=**内嵌字符串**
  （`sha2_hmac_starts(ctx, key=*(指针), strlen(key), 0)`，指针来自 pc-rel 混淆表）。
- `sw_hmac256_signature_final`（0xc558）有**参数门控**：
  `if (sw_parameter_get_int(混淆名)==0) return 1;`——参数为 0 时验签直接放行。
  理论可提取 HMAC 密钥或操纵参数绕过此门，但：
- **swapp 全程无 mtd 写调用**（mtd_scan_partitions/mtd_find_partition_by_name/
  mtd_read_partition/mtd_read_close 仅读）→ U-Boot 刷写也必须经 recovery → 即使绕过
  swsignature，仍死在 recovery 的 RSA 门上。
- libswkey.so 与升级无关（设备授权证书 X.509 链校验：PEM_read_X509/有效期/keylen）。
- recovery.sh 实锤固定执行 `/usr/sbin/recovery --update_package=/tmp/update.zip`，
  之后读 /data/status 等 "success"（swapp 侧 strncmp 铁证 @0xbd6c→0x85f64）。

### 8.3 最终判定

| 环节 | 实现 | 可绕过性 |
|---|---|---|
| swapp 包验签 | HMAC-SHA256，内嵌密钥 + 参数门控 | 理论可（提取密钥/改参数），但无意义 |
| recovery 包验签 | mincrypt RSA-2048 e=3，逐字节填充，强制 | **不可**（无私钥；填充严格；哈希含 cd_offset） |
| 旁路刷写 | 无（swapp 不写 mtd；recovery 无跳过路径） | 无 |

**免拆机免串口刷自制固件到全新原厂机 = 不可行**。串口首刷（一次性写入钩子镜像 v2）仍是
唯一路径；装上钩子后 SSH/webd 常驻，此后升级走 bootfix v7 换 SD 重启，永久免串口。

分析中间产物保留在 `out/re_analysis/`（含 libswkey/libswmodules 反汇编与公钥解析脚本输出）。
