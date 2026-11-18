# 有线网卡（eth0 / YT8512B）链路诊断 + 折腾全程记录

> 目标：让 S4602 的 eth0 像原厂固件一样通过 PoE + DHCP 联网出图。
> 时间跨度：**2026-11-12 ~ 2026-11-13**（两天战线）。
> 状态：根因已定位并修复（PHY 驱动 + 原厂寄存器序列），设备侧现场验证受内核回归阻塞，
> 已改用运行时 sysfs 方案 + SD uImage 三参 bootm 启动法收口。

本文自 2026-11-18 起为**唯一** eth0 记录：既含 11-12 静态取证（CRU/GRF、BMSR、时钟链），
也含 11-13 下半场的翻车（内核回归 / bootm 控制 DTB / NAND 坏块）与最终收口。

---

## 0. 一句话结论 + 时间线

| 阶段 | 结论 |
|---|---|
| 现象 | eth0 注册但 **NO-CARRIER**，`udhcpc` 广播 5 次无租约 |
| 排除 | 网络侧（PoE/线/交换机）没问题 → 必为 OpenIPC 内核侧；DTB 与 vendor 黄金**逐字节一致** → DTB 非根因 |
| 根因① | OpenIPC 内核没编 **YT8512B 专用 PHY 驱动**（只有通用 c22），RMII link 起不来 |
| 修复① | 回移植 `motorcomm.c` + 4 处 4.19 适配，PHY attach 成功 |
| 仍 NO-CARRIER | bench **本来就插着线但没物理链路**；BMSR 上一轮**读错**（latch-low） |
| 时钟嫌疑 | 用 `devmem` 直读 CRU/GRF，**RMII 50 MHz 链全对**，排除 SoC 时钟 |
| **根因②** | 原厂 rootfs 有 `S10phyreg_config`：boot 早期经 sysfs `phy_registers` **逐条写 YT8512B 扩展寄存器**；OpenIPC 既没跑脚本、内核驱动也没做这步 |
| 修复② | 把原厂序列写进 `motorcomm.c`（`yt8512_s4602_phy_init`），commit `4d76e24` |
| 翻车 | CI 新内核**有回归**（clk 驱动不认 type 11 → arch timer 拿不到 → 启动即死机）；`bootm`/`fdt addr` 绕不过 U-Boot 控制 DTB bug |
| 收口 | 改**运行时 sysfs 方案**（`diag_apply_factory.txt`）+ SD uImage 三参 bootm 启动法，不依赖坏内核构建 |

---

## 1. 硬件与驱动事实（已实证）

| 项 | 值 | 证据 |
|---|---|---|
| PHY | Motorcomm **YT8512B**，`phy_id = 0x00000128` | `YT8512B Ethernet stmmac-0:00: attached PHY driver` |
| MDIO 地址 | `stmmac-0:00`（i2c1 不用） | vendor DTB `ethernet@ffc40000/mdio/phy@0` |
| 接口模式 | `phy-mode = "rmii"` | vendor DTB |
| 时钟方向 | `clock_in_out = "output"`（SoC 出 50 MHz 给 PHY） | vendor DTB |
| 引脚 | `pinctrl-0 = <rmiim0-miim, rmiim0-bus2, gmac-clk-m0-pins>` | vendor DTB |
| PHY 复位 | `snps,reset-gpio = <0x9d 0x11 0x1>` + `snps,reset-active-low` + `snps,reset-delays-us = <0 50000 10000>` | vendor DTB |
| PHY 驱动 | OpenIPC 4.19 **上游没有** motorcomm（只有 YT8511/8521/8522/8531/8531S/INT861X/YT8821）；Rockchip `develop-4.19` 的 `drivers/net/phy/` 也**没有** motorcomm.c | 已从两个上游仓库实测拉取核对 |
| 我们的实现 | 从 Rockchip BSP 回移植 `motorcomm.c`（含 YT8512B 条目），4.19 适配 4 处 | `src/kernel-patches/motorcomm.c` |

> **重要**：Rockchip 公开 4.19 树里没有 motorcomm 驱动 ⇒ 原厂 CMCC 固件的 4.19 内核
> 极可能**用的是 generic PHY 驱动**（`genphy` 有 catch-all 的 `PHY_ANY_ID`）。
> 也就是说：我们回移植的 motorcomm 驱动在**现场是否比 genphy 更好，从未被验证过**，
> 它反而引入了"改写 PHY 厂商扩展寄存器"这个新的风险面（见 §4.2 与 §5 的 T4）。

### 1.1 motorcomm.c 的 4.19 适配（4 处，缺一不可编译/可用）

1. `phy_lock_mdio_bus/phy_unlock_mdio_bus`（5.x 才有的封装）→ 改回 `mutex_lock(&phydev->mdio.bus->mdio_lock)`
2. 补 `#define PHY_ID_MATCH_EXACT 0`（4.19 没有这个宏）
3. **补 `.features = PHY_BASIC_FEATURES`** —— 关键坑：4.19 `phy_probe` 直接 `supported = phydrv->features`，没有 5.x 的 `get_features` 回退；不补的话 `supported=0` → **PHY 协商不出任何速率**。
4. 修 BSP 笔误：`yt8512_led_init` 把位掩码 `0x0010` 当成寄存器地址写（正确应为 `0x40c3`）。

修法配套：`board/s4602/s4602_kernel_fragment.config` 加 `CONFIG_MOTORCOMM_PHY=y`（连带 REALTEK/MICREL），`build.yml` 加构建后硬自检（符号须 =y 否则 CI 失败）。

### 1.2 坑：Rockchip FIQ debugger 劫持串口（串口传文件会断）

Rockchip 内核编译进 FIQ debugger：串口输入流里出现**连续 3 字节 `fiq`** 即接管 tty（实测 `echo fiq` 触发，`fi`/`iq`/`debug` 不触发）。XMODEM 传 3.38 MB 时恰在 block 3224 断开——镜像内第一个 `fiq` 就在 offset 3,301,364（= block 3224 起点），分毫不差。

解法：`tools/make_safe_b64.py` 用**无小写字母的 64 字符自定义 base64 字母表**编码（原理上不可能出现 `fiq`），设备端 `tr '<自定义>' '<标准>' | base64 -d` 还原。另有坑：shell 被二进制垃圾吃掉会停在 ash 的 PS2 `>`，要 **Ctrl-C** 复位。

---

## 2. bench 上没有物理链路（双向证实）

| 证据 | 内容 |
|---|---|
| 原厂 `_a` 槽启动日志 `docs/evidence/factory-boot.log` | `udhcpc: broadcasting discover` ×5 → `udhcpc: no lease, forking to background` |
| 我们的 OpenIPC 启动日志 `docs/evidence/live-kernel-flash-20261112-201304.log` | 同样 `udhcpc: no lease` |
| PHY 寄存器实测（4 次采样，1 s 间隔，值完全稳定） | `ANLP(reg5) = 0x0000` —— 对端自协商通告**全零**，说明**根本没收到对端的 FLP** |

结论：两套固件在 bench 上同样失败 ⇒ bench 无网线/无对端端口/交换机未放行。
**"eth0 NO-CARRIER" 在 bench 上不是软件 bug，而是环境事实。**（现场有 PoE 交换机口，验证要等现场。）

---

## 3. 重要纠错：上一轮的 BMSR 解码是错的

上一轮把 `BMSR = 0x7949` 读成 "bit2 link=1、bit5 autoneg_done=1"，**结论相反**。

权威位定义（`include/uapi/linux/mii.h`，4.19）：

```
#define BMCR_RESET        0x8000   /* bit15 */
#define BMCR_LOOPBACK     0x4000   /* bit14 */
#define BMCR_SPEED100     0x2000   /* bit13 */
#define BMCR_ANENABLE     0x1000   /* bit12 */
#define BMCR_FULLDPLX     0x0100   /* bit8  */
#define BMCR_ANRESTART    0x0200   /* bit9  */
#define BMCR_SPEED1000    0x0040   /* bit6  */
#define BMSR_LSTATUS      0x0004   /* bit2  —— latch-low，读一次会被清掉 */
#define BMSR_ANEGCAPABLE  0x0008   /* bit3  */
#define BMSR_ANEGCOMPLETE 0x0020   /* bit5  */
```

据此重新解码实测值：

| 寄存器 | 值 | 正确解读 |
|---|---|---|
| BMCR (0) | `0x1140` | ANENABLE(bit12) + FULLDPLX(bit8) + SPEED1000(bit6，默认值)；ANRESTART(bit9)=0 → 自协商已跑完一轮 |
| BMSR (1) | `0x7949` | **bit2 LSTATUS = 0 → 链路 DOWN**；**bit5 ANEGCOMPLETE = 0 → 自协商未完成**；bit3 ANEGCAPABLE=1 |
| ANAR (4) | `0x11e1` | 通告 10/100 全/半双工 + 1000T(bit8) |
| ANLP (5) | `0x0000` | **对端通告全零 → 没收到任何对端信号** |
| ANER (6) | `0x0004` | bit2 = 自协商重启/超时错误 |
| ANADV (7) | `0x2001` | bit13 = 自协商能力（link partner 用） |

**两个关键点**

1. **BMSR bit2 是 latch-low 语义**：读一次会把状态清掉，第二次读才是实时值。
   所以"判定链路"必须**连续读两次**——原来的单读结论方法上就不成立。
2. `0x7949` 里 bit2/bit5 都是 0：**PHY 侧既没有链路、也没完成自协商**，
   与 `ANLP = 0x0000` 完全一致 ⇒ 指向"物理层未接通"，而不是"软件没配好"。

---

## 4. 仍未排除的两个软件风险点

### 4.1 `clk_mac_refout` 取不到 → GMAC 的 50 MHz 引脚时钟可能从未使能

> **本节已在 2026-11-12 用 `devmem` 直接读 CRU/GRF 核对完毕，结论见 §4.1.1：
> SoC 侧的 RMII 50 MHz 链完全正确且已使能，此嫌疑排除。**

vendor DTB 里 gmac 节点的 `clock-names` 是：

```
"stmmaceth", "mac_clk_rx", "mac_clk_tx", "clk_mac_ref",
"aclk_mac", "pclk_mac", "clk_mac_speed", "ptp_ref"
```

**没有 `clk_mac_refout`**。而 `dwmac-rk.c` 在 RMII + `clock_in_out=output` 时会：

```c
bsp_priv->clk_mac_refout = devm_clk_get(dev, "clk_mac_refout");
if (IS_ERR(bsp_priv->clk_mac_refout))
        dev_err(dev, "cannot get clock %s\n", "clk_mac_refout");
```

→ 必然失败。**原厂内核日志里也有这一条**：

```
[    0.190124] rk_gmac-dwmac ffc40000.ethernet: cannot get clock clk_mac_refout
```

OpenIPC 的 `dwmac-rk.c` 同样报这条。但"原厂也有"只说明**原厂固件也拿不到这个时钟**，并不能证明 PHY 拿到了 50 MHz。

设备实测时钟树（`/sys/kernel/debug/clk/clk_summary`）：

```
clk_gmac_ethernet_out2io   enable=0 prepare=0  rate=62,500,000   <- 未使能
  clk_gmac_div              enable=1 prepare=1  rate=50,000,000   <- 已使能，50 MHz 正确
    clk_gmac_src_m0         enable=1            rate=50,000,000
      clk_gmac_src          enable=3            rate=50,000,000
```

RV1126 时钟树（`drivers/clk/rockchip/clk-rv1126.c`）里：

```c
COMPOSITE(CLK_GMAC_DIV, "clk_gmac_div", mux_cpll_gpll_p, 0,
        RV1126_CLKSEL_CON(63), 7, 1, MFLAGS, 0, 5, DFLAGS,
        RV1126_CLKGATE_CON(20), 6, GFLAGS),
COMPOSITE(CLK_GMAC_ETHERNET_OUT, "clk_gmac_ethernet_out2io", mux_cpll_gpll_p, 0,
        RV1126_CLKSEL_CON(61), 15, 1, MFLAGS, 8, 5, DFLAGS,
        RV1126_CLKGATE_CON(20), 11, GFLAGS),
```

- `clk_gmac_div` = 50 MHz，**已使能** ⇒ MAC 内部 RMII 时钟正常。
- `clk_gmac_ethernet_out2io` = 62.5 MHz（RGMII 用），**未使能**。
- 注意：`enable_count` 只是 **Linux 的软件计数器**，**不等于硬件门控位**——
  U-Boot 若曾置位该位，Linux 仍会显示 0。所以这里**不能下结论**，
  必须读 CRU 寄存器或用功能测试来判定。

#### 4.1.1 直接读 CRU/GRF 后的结论（2026-11-12，已排除该嫌疑）

设备上 `/dev/mem` 存在但 `dd` 被 devmem 限制（EIO），**BusyBox `devmem` applet 可用**
⇒ 无需重建内核即可直接读硬件寄存器。

地址推导（源码核对）：

```
CRU  = 0xff490000     GRF = 0xfe000000
RV1126_CLKSEL_CON(x)  = x*4 + 0x100     (drivers/clk/rockchip/clk.h:111)
RV1126_CLKGATE_CON(x) = x*4 + 0x280     (clk.h:112)
RV1126_GMAC_CON       = 0x460           (clk-rv1126.c:14)
RV1126_GRF_IOFUNC_CON1= 0x10264         (clk-rv1126.c:15)
```

实测：

| 寄存器 | 值 |
|---|---|
| `0xff4902d0` CLKGATE_CON(20) | `0x00003A00`（置位 bit9/11/12/13） |
| `0xff4901f4` CLKSEL_CON(61) | `0x00000709` |
| `0xff4901fc` CLKSEL_CON(63) | `0x00000109` |
| `0xff490460` GMAC_CON | `0x00000010` |
| `0xfe010264` GRF_IOFUNC_CON1 | `0x00000000` |

**门控位极性**：用 CLKGATE_CON(20) 的 8 个位与 Linux `clk_summary` 的 `enable_count`
逐一对照，**8/8 全部一致于"1 = 关断（低有效使能）"**。若极性相反，则
`clk_gmac_div`（Linux 明确 `enable_count=1`）应当是关断的，矛盾。
⇒ **debugfs 的 `enable_count` 只是软件计数器，不能当硬件状态用。**

**RMII 50 MHz 链逐级核对（全部通过）**：

```
clk_gmac_div  : CLKSEL_CON(63)=0x109 -> mux=cpll(500MHz), div=10 -> 50 MHz
                CLKGATE_CON(20) bit6 = 0 -> 使能           ✓
clk_gmac_src  : GRF_IOFUNC_CON1 bit12 = 0 -> 选 src_m0 = clk_gmac_div   ✓
clk_gmac_rx_src: CLKGATE_CON(20) bit8 = 0 -> 使能           ✓
rmii_mode_clk : GMAC_CON bit1 = 0 -> mux_rmii_clk_p[0] = clk_gmac_rx_div20 (2.5MHz) ✓
clk_gmac_tx_rx: GMAC_CON bit4 = 1 -> rmii_mode_clk          ✓
```

⇒ **SoC 侧给 PHY 提供 50 MHz 参考时钟的链路完整且已使能。**

`clk_gmac_ethernet_out2io`（CLKGATE_CON(20) bit11 = 1 → 关断，CLKSEL_CON(61)=0x709
→ cpll/8 = 62.5 MHz）是 **RGMII 场景**的引脚源（gpll 1 GHz /8 = 125 MHz），
与本板 RMII 配置无关。

**⇒ 4.1 的嫌疑排除。剩下的候选只有 PHY 侧与物理层，由 §5 的 T1/T2/T3/T4 判定。**

### 4.2 我们回移植的 motorcomm 驱动会改写 PHY 厂商扩展寄存器

`yt8512_clk_init()` 会写：

- `AFE_PLL (0x50)` |= `PLL_REFCLK_SEL_EN (0x0040)` —— 选择 PLL 作为参考时钟源
- `EXTEND_COMBO (0x4000)` |= `RMII_EN (0x0001)` —— 使能 RMII 模式

如果原厂固件用的是 genphy（**不碰这些寄存器**），那么我们这两位写入
**有可能把 PHY 从"能工作"改成"不能工作"**，而 bench 上两者都不 link，
所以这个风险**从来没被暴露过**（§5 的 T4 专门回滚这两位再测，就是覆盖这个风险）。

---

## 5. 内核自检块 `s4602-phy-diag`

文件：`src/kernel-patches/motorcomm.c`（commit `69e803f`）。
PHY attach 后 **8 秒**自动执行，只用 CL22 标准寄存器 + YT8512 扩展寄存器。

| 测试 | 做什么 | 判定 |
|---|---|---|
| **T1** | `BMCR_LOOPBACK`（0x4000）内部自环，不关自协商以外的任何外部依赖 | link=1 ⇒ **PHY 核心与参考时钟正常**；link=0 ⇒ PHY 无参考时钟/配置错 |
| **T2** | 强制 100M 全双工（`BMCR_SPEED100|BMCR_FULLDPLX`，关自协商） | link=1 ⇒ 物理 MDI 链路存在 |
| **T3** | 强制 10M 半双工（`BMCR_SPEED10`） | 同上，另一速率交叉验证 |
| **T4** | 把 `AFE_PLL`/`EXTEND_COMBO` **回滚到 attach 时读到的原值**再测一次 | T4=1 而 T2=0 ⇒ **是我们的 PHY 时钟初始化把 PHY 配坏了**（此时内核自动保留回滚配置，打印 `AUTO-FIX`） |

工程细节：

- BMSR **连续读两次**才取实时链路位（latch-low）。
- 所有寄存器访问走 `__phy_read/__phy_write` + `mdio_lock`，不与 phylib 轮询线程抢总线。
- 强制速率测试会把**读回的 BMCR** 一起打印；若被轮询线程改回，标注
  `[POLLER-CLOBBERED]`，避免采信无效结论。
- 结束后恢复 `BMCR = ANENABLE|ANRESTART` 自协商。

日志长这样（现场只要看 `s4602-phy-diag:` 开头的行）：

```
s4602-phy-diag: ===================== BEGIN =====================
s4602-phy-diag: idle      BMCR=0x1140 BMSR=0x7949(link=0 aneg_done=0)
s4602-phy-diag: idle      ANAR=0x11e1 ANLP=0x0000 ANER=0x0004 ANADV=0x2001 SPEC=0x0000
s4602-phy-diag: idle      ext AFE_PLL(0x50)=0x0040 EXTEND_COMBO(0x4000)=0x0001
s4602-phy-diag: T loopback-100fd    want=0x6100 got=0x6100 BMSR=0x.... link=? SPEC=0x....
s4602-phy-diag: T1 loopback link=? => ...
s4602-phy-diag: T force-100fd      want=0x2100 got=0x2100 BMSR=0x.... link=? SPEC=0x....
s4602-phy-diag: T2 force-100fd link=?
s4602-phy-diag: T force-10hd       want=0x0000 got=0x0000 BMSR=0x.... link=? SPEC=0x....
s4602-phy-diag: T3 force-10hd link=?
s4602-phy-diag: T4 rollback link=? (T2 was ?)
s4602-phy-diag: ====================== END ======================
s4602-phy-diag: SUMMARY: T1loop=? T2force100=? T3force10=? T4rollback=?
```

### 读表规则

| 观察 | 结论 | 下一步 |
|---|---|---|
| T1=1，T2=T3=0 | PHY 完全正常，bench 没插线/对端未启用 | 等现场插 PoE 口即可 |
| T1=1，T2=1 | 链路已通 | 直接跑 DHCP |
| T1=0 | PHY 没有有效参考时钟，或我们的扩展寄存器配置把它配坏了 | 看 T4；查 gmac_clk 引脚时钟与 CRU 门控位 |
| T2=0 但 T4=1 | 我们的 motorcomm 时钟初始化是元凶 | 内核已自动保留回滚配置（AUTO-FIX） |

---

## 6. 根因②（决定性）：原厂 PHY 寄存器初始化序列缺失

PHY attach 成功但 link=0 的**真正原因**：

用 WSL `ubi_reader` 解出原厂 rootfs 备份 `system_b.img`（UBI→squashfs），找到
**`etc/init.d/S10phyreg_config`** —— 原厂在 boot 早期通过 sysfs
`/sys/devices/platform/ffc40000.ethernet/mdio_bus/stmmac-0/stmmac-0:00/phy_registers`
**逐条写 YT8512B 扩展寄存器 + BMCR 软复位**。**OpenIPC 既没跑这个脚本、内核驱动也没做这套写操作。**

**原厂实测序列**（启动日志 Link is Up 那次，reg30/reg31 间接寻址）：

```
0x2027←0x2026;  0x10←0x62;  BMCR←0xa100(软复位+100M/full);
0x2057←0x2a4f;  0x1←0x1240;  0x11←0x6;  0x12←0xa;  0x13←0xf;  0x14←0x11;  0x15←0x1fe
```

（备份 rootfs 脚本里 reg16 是 `0x2`，但实测 Link is Up 那次是 `0x62`，
**以 0x62 为准**，代码注释标了备选 `0x2`。）

**修复**：`src/kernel-patches/motorcomm.c` 新增 `yt8512_s4602_phy_init()`，把上面序列一字不差
写进 `yt8512_config_init`（放在 `yt8512_clk_init` 之后作最终生效配置）；诊断块 T1–T4 保留未删。
commit `4d76e24` "fix(net): 固化原厂 YT8512B PHY 寄存器初始化序列修复 eth0 NO-CARRIER"，已 push。

**关键事实修正**：原厂 boot_a/b/rootfs DTB 与本工程 DTB **md5 一致** → pinmux/时钟配置
非差异，**差异 100% 在 PHY 寄存器初始化（软件）**，非硬件/网线。

> 为什么之前在 bench 回放序列"没生效"：bench 本就没物理链路，回放相同序列 link 当然仍 0，
> 是环境结论而非验证失败。

---

## 7. 下半场翻车（2026-11-13）：内核回归 + 启动命令坑

### 7.1 CI 新内核镜像有回归，启动即死机

CI run `43297605801` 构建成功，但用户手工重启后**卡死在 `sched_clock` 之后**，串口彻底静默
（连 SysRq BREAK 无响应）= 真死机。对照 21:07 能启动的 `f484aec1` 日志定位：

| 镜像 | 时钟行为 | 结果 |
|---|---|---|
| `f484aec1`（好） | `arch_timer: cp15 timer running at 24.00MHz` → `sched_clock: 56 bits at 24MHz` | 启动到 `Welcome to OpenIPC` |
| `43297605801`（坏） | `rockchip_clk_register_branches: unknown clock type 11` → `timer_probe: no matching timers` → 伪 `sched_clock: 32 bits at 300 Hz` | **死机** |

**所有本地 FIT 的 DTB 完全相同**（md5 一致，都是正确的 38x38 V10 板级 DTB，含 arch timer 节点）
→ **DTB 不是原因**。结论：新 CI 镜像用的**内核源码 / rockchip clk 驱动相对 `f484aec1` 回退了**
（RV1109 的 clk 驱动不认 clock type 11 → 时钟树注册失败 → arch timer 拿不到时钟）。
**这不是 PHY 修复 commit 的锅，是内核树被换成了有回归的版本。**

教训：**CI 新内核镜像不可直接信任启动**，必须与已知好版本做内核级 diff（arch timer / clk），
不能只看 DTB / CRC。后续若要从源码修，应基于 `f484aec1` 内核树 + 仅叠加 PHY 修复，禁用有回归的内核树。

### 7.2 `bootm` 从 SD RAM 启动仍卡死 + 坏块让 CRC 变假象

- 从 SD `fatload` FIT 进 RAM 用 `bootm` 启动，内核仍报 `Machine model: Rockchip RV1126 Evaluation Board`
  + 无 arch timer → 卡死。bootm 日志 `Loading fdt from 0x1ddf93a0`（**U-Boot 自身控制 DTB**），不是 FIT 的 `fdt@1`。
- 试 `fdt addr 0x13248d0`（FIT 内正确 fdt@1）重定向 → **无效**，bootm 还是 `0x1ddf93a0`。
  本 U-Boot（2017.09）的 `bootm` **无视 `fdt addr`、强制用控制 DTB**（Eval Board，无 arch timer）。这是该 Rockchip U-Boot 的固有问题。
- **坏块让 CRC 校验变假象**：`mtd read/write` 跳过坏块不留空洞 → 写与回读跳过同一坏块 →
  源/回读 CRC 都 `f484aec1` 误判"刷写成功"；但 U-Boot `boot_fit` 另一条读路径读到的 fdt 被坏块破坏。
  ⇒ "刷写成功 + CRC 一致"在此场景是**假象**。

### 7.3 最终可靠启动法（绕开坏 boot_b + 绕开 bootm 控制 DTB bug）

1. 从 `f484aec1` FIT 拆出 `kernel@1`（3,293,392B raw ARM Image）与 `fdt@1`（85,937B，正确 38x38 V10 DTB）
   → `firmware/s4602_kernel.bin` / `firmware/s4602.dtb`
2. raw Image 的 `bootz` 不支持，`bootm` 又抢控制 DTB → **用 Python 生成 legacy uImage 包裹 raw kernel**
   → `firmware/s4602_uImage.bin`（3,293,456B，`ih_load/ep=0x02008000`）
3. SD 卡根放 `s4602_uImage.bin` + `s4602.dtb`；上电截停 U-Boot：
   ```
   setenv bootargs 'ubi.mtd=9 root=ubi0:rootfs rootfstype=ubifs <完整 mtdparts>'
   fatload mmc 1 0x02008000 s4602_uImage.bin
   fatload mmc 1 0x08300000 s4602.dtb
   bootm 0x02008000 - 0x08300000      # 第 3 参数 = 显式 FDT 地址，不走 FIT fdt 解析、不被控制 DTB 覆盖
   ```
   ⇒ 内核拿到正确 38x38 V10 DTB → 到登录符。脚本 `tools/sd_bootz_openipc.py`（`--go` 自动启动 + 灌 PHY 序列）。

### 7.4 PHY 修复的收口方式

因为好内核（`f484aec1`）没编进 `yt8512_s4602_phy_init`，且 CI 新内核树有回归不可信，
**PHY 修复改用运行时 sysfs 方案**：`tools/diag_apply_factory.txt` 在运行中的系统上写 `phy_registers`
复现原厂序列（已验证 `phy_registers` 可写）。先跑它确认 `eth0/carrier=1`，再考虑固化到 rootfs 启动脚本。

---

## 8. 坑速查清单

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | 缺 YT8512B 专用 PHY 驱动 | eth0 NO-CARRIER | fragment 加 `CONFIG_MOTORCOMM_PHY`，回移植 motorcomm.c |
| 2 | motorcomm 5.x API 不兼容 4.19 | 编译失败 / 协商不出速率 | 4 处适配，**必须补 `.features`**（§1.1） |
| 3 | Rockchip FIQ debugger | XMODEM 传 3.38MB 恰好断 | 无小写字母 base64 字母表 + `tr` 还原（§1.2） |
| 4 | BMSR latch-low | 单读误判链路 up/down | **连读两次**再取 bit2（§3） |
| 5 | bench 无物理链路 | udhcpc no lease（原厂也这样） | 认环境事实，等现场 PoE 口（§2） |
| 6 | debugfs `enable_count` 是软件计数 | 误判时钟门控 | `devmem` 直读 CRU/GRF（§4.1.1） |
| 7 | 原厂 PHY 寄存器初始化缺失 | PHY attach 但 link=0 | `yt8512_s4602_phy_init` 写原厂序列（§6） |
| 8 | CI 新内核 clk 驱动回归 | 启动即死机（无 arch timer） | 用已知好版本 `f484aec1`，内核级 diff 而非看 DTB/CRC（§7.1） |
| 9 | `bootm` 强制控制 DTB | SD RAM 启动仍 Eval Board | uImage + **三参 bootm 显式 FDT**（§7.3） |
| 10 | NAND 坏块让 mtd CRC 变假象 | 源/回读 CRC 一致但 fdt 读坏 | 不依赖 boot_b，走 SD 启动（§7.2） |

---

## 9. 现场验证清单（设备接 PoE 交换机口后）

1. 串口开机，抓 `s4602-phy-diag:` 段 → 按 §5 读表规则判定。
2. `cat /sys/class/net/eth0/carrier` 应为 `1`，`operstate` = `up`。
3. `udhcpc -i eth0` 应拿到 IP；`ping` 网关 / OpenIPC 平台。
4. 出图验证：`majestic` / `ffmpeg` 拉流。
   > **状态更新 2026-11-13**：「仍缺 rkaiq IQ/3A tuning XML」已解决 —— 原厂 `system_a.img`
   > 已解包，`COMMON_sc5338.xml` + 全套 RKMedia/rkaiq 库均已找到且 ABI 兼容；
   > 标准 V4L2 **MPLANE** 抓帧已成功出图。详见 [`FIRST_LIGHT.md`](../openipc/FIRST_LIGHT.md)。
   > 当前剩余项是把 IQ 参数注入 rkisp 以得到硬件 NV12 流。

## 10. 若 T1=0（PHY 无参考时钟）的后续排查方向

1. 读 RV1126 CRU（`0xff490000`）`CLKGATE_CON(20)` 的 bit6/bit11 与 `CLKSEL_CON(61/63)`，
   确认 `clk_gmac_div`（50 MHz）与 `clk_gmac_ethernet_out2io` 的硬件门控位真实状态
   —— debugfs 的 `enable_count` 不足以判定（已在 §4.1.1 做完）。
2. 确认 `gmac-clk-m0-pins`（bank3 pin16 mux2 cfg0xb3）的引脚复用与板级实际走线
   是否一致：若 PHY 其实用晶振、50 MHz 由 PHY 输出给 SoC，则 DTB 的
   `clock_in_out="output"` 是错的，应改 `"input"`。
3. 尝试在 motorcomm 驱动里显式 `clk_prepare_enable()` 对应的 50 MHz 引脚时钟。

---

## 11. 相关文件

| 文件 | 说明 |
|---|---|
| `src/kernel-patches/motorcomm.c` | 回移植的 YT8512B 驱动 + `yt8512_s4602_phy_init`（原厂序列）+ `s4602-phy-diag` 自检块 |
| `tools/apply_kernel_patch.py` | CI 注入器（拷贝 motorcomm.c、Makefile、Kconfig） |
| `board/s4602/s4602_kernel_fragment.config` | `CONFIG_MOTORCOMM_PHY=y` 等唯一事实源 |
| `.github/workflows/build.yml` | 内核构建 + PHY 相关符号硬自检 + 诊断块存在性自检 |
| `tools/diag_apply_factory.txt` | 运行时 sysfs 复现原厂 PHY 序列（免重编一键验证） |
| `tools/make_safe_b64.py` | FIQ 安全的 base64 字母表 |
| `tools/sd_bootz_openipc.py` / `sd_boot_openipc.py` | SD uImage 三参 bootm 启动 + 自动灌 PHY 序列 |
| `tools/auto_flash_boot_b.py` | 监听上电 → 截停 → 刷 boot_b |
| `firmware/s4602_uImage.bin` / `s4602.dtb` | uImage 包裹 raw kernel + 正确 38x38 V10 DTB |
| `docs/evidence/factory-boot.log` | 原厂 `_a` 槽启动日志（含 `cannot get clock clk_mac_refout`、`udhcpc no lease`） |
| `docs/evidence/serial-shell-phy4.log` | 4 次 CL22 寄存器采样（原始值来源） |
| `docs/evidence/serial-shell-debugfs.log` | 时钟树 `clk_summary` 快照 |
| `docs/evidence/live-cru-grf-20261112.txt` | CRU/GRF 寄存器实测 |
| `docs/evidence/live-kernel-flash-20261112-201304.log` | 我们内核的启动日志 |
