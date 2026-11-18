
## 9. 文件来源与 GitHub Actions 复现（2026-11-18）

### 9.1 每个文件的来源（provenance）

| 文件 | 来源 | 状态 |
|---|---|---|
| `factory/system_b.img` | **原厂 mtd9 备份**（crc32 0x38df701f，46137344B），本次入库为唯一镜像源头 | git 已入库 |
| `flash/system_b.img`（钩子 v2） | `upgrade/build_hook_image.sh` 从 factory 全链重建（解包→注入 S99local v2→mksquashfs→ubinize→0xFF 填充） | ubireader 全绿：reserved_pebs=332 / seq 1734217813 / 尾部 0xFF；S99local 与 hook-src diff 逐字节一致；362=361 原厂+1 文件 |
| `base/bootfix.sh` | 本地编写 v7 | 真机 busybox `sh -n` + 幂等试跑全绿 |
| `base/webd` `index.html` `h264out_v4` `rtlib.tar.gz` `swapp_patched.xz` `np_resetpasswd.sh` `shadow.md5` | **实机回拉**（scp /data/www@192.168.6.214，源码 webd.c/h264out.c 在 tools/） | 实机在役验证 |
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
