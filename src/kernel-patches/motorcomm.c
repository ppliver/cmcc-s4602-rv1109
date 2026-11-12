// SPDX-License-Identifier: GPL-2.0+
/*
 * Driver for Motorcomm PHYs
 *
 * Author: Peter Geis <pgwipeout@gmail.com>
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/of_device.h>
#include <linux/phy.h>
#include <linux/delay.h>
#include <linux/jiffies.h>
#include <linux/workqueue.h>

/*
 * OpenIPC/linux rockchip-rv11xx is 4.19.111, which predates
 * PHY_ID_MATCH_EXACT() (added in mainline 5.0). Provide the identical
 * expansion for both struct phy_driver and struct mdio_device_id.
 * Both structs carry phy_id / phy_id_mask fields in 4.19.
 */
#ifndef PHY_ID_MATCH_EXACT
#define PHY_ID_MATCH_EXACT(id) .phy_id = (id), .phy_id_mask = 0xffffffff
#endif

#define PHY_ID_YT8011		0x4f51eb01
#define PHY_ID_YT8511		0x0000010a
#define PHY_ID_YT8512		0x00000118
#define PHY_ID_YT8512B		0x00000128
#define PHY_ID_YT8522		0x4f51e928
#define PHY_ID_YT8531S		0x4f51e91a
#define PHY_ID_YT8531		0x4f51e91b

enum {
	YT8011_RGMII_DVDDIO_1V8 = 1,
	YT8011_RGMII_DVDDIO_2V5,
	YT8011_RGMII_DVDDIO_3V3
};

struct yt8011_priv {
	u8 polling_mode;
	u8 chip_mode;
	u8 vddio;
};

#define YT8011_SPEED_MODE		0xc000
#define YT8011_DUPLEX			0x2000
#define YT8011_SPEED_MODE_BIT		14
#define YT8011_DUPLEX_BIT		13
#define YT8011_LINK_STATUS_BIT		10

#define REG_PHY_SPEC_STATUS		0x11
#define REG_DEBUG_ADDR_OFFSET		0x1e
#define REG_DEBUG_DATA			0x1f
#define REG_MII_MMD_CTRL		0x0D  /* MMD access control register */
#define REG_MII_MMD_DATA		0x0E  /* MMD access data register */

#define YT8511_PAGE_SELECT	0x1e
#define YT8511_PAGE		0x1f
#define YT8511_EXT_CLK_GATE	0x0c
#define YT8511_EXT_DELAY_DRIVE	0x0d
#define YT8511_EXT_SLEEP_CTRL	0x27

/* 2b00 25m from pll
 * 2b01 25m from xtl *default*
 * 2b10 62.m from pll
 * 2b11 125m from pll
 */
#define YT8511_CLK_125M		(BIT(2) | BIT(1))
#define YT8511_PLLON_SLP	BIT(14)

/* RX Delay enabled = 1.8ns 1000T, 8ns 10/100T */
#define YT8511_DELAY_RX		BIT(0)

/* TX Gig-E Delay is bits 7:4, default 0x5
 * TX Fast-E Delay is bits 15:12, default 0xf
 * Delay = 150ps * N - 250ps
 * On = 2000ps, off = 50ps
 */
#define YT8511_DELAY_GE_TX_EN	(0xf << 4)
#define YT8511_DELAY_GE_TX_DIS	(0x2 << 4)
#define YT8511_DELAY_FE_TX_EN	(0xf << 12)
#define YT8511_DELAY_FE_TX_DIS	(0x2 << 12)

#define YT8512_EXTREG_AFE_PLL		0x50
#define YT8512_EXTREG_EXTEND_COMBO	0x4000
#define YT8512_EXTREG_LED0		0x40c0
#define YT8512_EXTREG_LED1		0x40c3

#define YT8512_EXTREG_SLEEP_CONTROL1	0x2027

#define YT_SOFTWARE_RESET		0x8000

#define YT8512_CONFIG_PLL_REFCLK_SEL_EN	0x0040
#define YT8512_CONTROL1_RMII_EN		0x0001
#define YT8512_LED0_ACT_BLK_IND		0x1000
#define YT8512_LED0_DIS_LED_AN_TRY	0x0001
#define YT8512_LED0_BT_BLK_EN		0x0002
#define YT8512_LED0_HT_BLK_EN		0x0004
#define YT8512_LED0_COL_BLK_EN		0x0008
#define YT8512_LED0_BT_ON_EN		0x0010
#define YT8512_LED1_BT_ON_EN		0x0010
#define YT8512_LED1_TXACT_BLK_EN	0x0100
#define YT8512_LED1_RXACT_BLK_EN	0x0200
#define YT8512_SPEED_MODE		0xc000
#define YT8512_DUPLEX			0x2000

#define YT8512_SPEED_MODE_BIT		14
#define YT8512_DUPLEX_BIT		13
#define YT8512_EN_SLEEP_SW_BIT		15

#define YT8522_TX_CLK_DELAY             0x4210
#define YT8522_ANAGLOG_IF_CTRL          0x4008
#define YT8522_DAC_CTRL                 0x2057
#define YT8522_INTERPOLATOR_FILTER_1    0x14
#define YT8522_INTERPOLATOR_FILTER_2    0x15
#define YT8522_EXTENDED_COMBO_CTRL_1    0x4000

#define YTXXXX_SPEED_MODE               0xc000
#define YTXXXX_DUPLEX                   0x2000
#define YTXXXX_SPEED_MODE_BIT           14
#define YTXXXX_DUPLEX_BIT               13
#define YTXXXX_AUTO_NEGOTIATION_BIT     12
#define YTXXXX_ASYMMETRIC_PAUSE_BIT     11
#define YTXXXX_PAUSE_BIT                10
#define YTXXXX_LINK_STATUS_BIT          10

/* if system depends on ethernet packet to restore from sleep,
 * please define this macro to 1 otherwise, define it to 0.
 */
#define SYS_WAKEUP_BASED_ON_ETH_PKT	1

/* to enable system WOL feature of phy, please define this macro to 1
 * otherwise, define it to 0.
 */
#define YTPHY_WOL_FEATURE_ENABLE	0

#if (YTPHY_WOL_FEATURE_ENABLE)
#undef SYS_WAKEUP_BASED_ON_ETH_PKT
#define SYS_WAKEUP_BASED_ON_ETH_PKT	1
#endif

struct yt8xxx_priv {
	u8 polling_mode;
	u8 chip_mode;
};

/* for YT8531 package A xtal init config */
#define YTPHY8531A_XTAL_INIT		0


#define YT8521_EXTREG_SLEEP_CONTROL1	0x27
#define YT8521_EN_SLEEP_SW_BIT		15

#define YT8521_SPEED_MODE		0xc000
#define YT8521_DUPLEX			0x2000
#define YT8521_SPEED_MODE_BIT		14
#define YT8521_DUPLEX_BIT		13
#define YT8521_LINK_STATUS_BIT		10

/* YT8521 polling mode */
#define YT8521_PHY_MODE_FIBER		1 /* fiber mode only */
#define YT8521_PHY_MODE_UTP		2 /* utp mode only */
#define YT8521_PHY_MODE_POLL		3 /* fiber and utp, poll mode */

static int yt8521_hw_strap_polling(struct phy_device *phydev);
#define YT8521_PHY_MODE_CURR		yt8521_hw_strap_polling(phydev)

static int yt8511_read_page(struct phy_device *phydev)
{
	return __phy_read(phydev, YT8511_PAGE_SELECT);
};

static int yt8511_write_page(struct phy_device *phydev, int page)
{
	return __phy_write(phydev, YT8511_PAGE_SELECT, page);
};

static int yt8511_config_init(struct phy_device *phydev)
{
	int oldpage, ret = 0;
	unsigned int ge, fe;

	oldpage = phy_select_page(phydev, YT8511_EXT_CLK_GATE);
	if (oldpage < 0)
		goto err_restore_page;

	/* set rgmii delay mode */
	switch (phydev->interface) {
	case PHY_INTERFACE_MODE_RGMII:
		ge = YT8511_DELAY_GE_TX_DIS;
		fe = YT8511_DELAY_FE_TX_DIS;
		break;
	case PHY_INTERFACE_MODE_RGMII_RXID:
		ge = YT8511_DELAY_RX | YT8511_DELAY_GE_TX_DIS;
		fe = YT8511_DELAY_FE_TX_DIS;
		break;
	case PHY_INTERFACE_MODE_RGMII_TXID:
		ge = YT8511_DELAY_GE_TX_EN;
		fe = YT8511_DELAY_FE_TX_EN;
		break;
	case PHY_INTERFACE_MODE_RGMII_ID:
		ge = YT8511_DELAY_RX | YT8511_DELAY_GE_TX_EN;
		fe = YT8511_DELAY_FE_TX_EN;
		break;
	default: /* do not support other modes */
		ret = -EOPNOTSUPP;
		goto err_restore_page;
	}

	ret = __phy_modify(phydev, YT8511_PAGE, (YT8511_DELAY_RX | YT8511_DELAY_GE_TX_EN), ge);
	if (ret < 0)
		goto err_restore_page;

	/* set clock mode to 125mhz */
	ret = __phy_modify(phydev, YT8511_PAGE, 0, YT8511_CLK_125M);
	if (ret < 0)
		goto err_restore_page;

	/* fast ethernet delay is in a separate page */
	ret = __phy_write(phydev, YT8511_PAGE_SELECT, YT8511_EXT_DELAY_DRIVE);
	if (ret < 0)
		goto err_restore_page;

	ret = __phy_modify(phydev, YT8511_PAGE, YT8511_DELAY_FE_TX_EN, fe);
	if (ret < 0)
		goto err_restore_page;

	/* leave pll enabled in sleep */
	ret = __phy_write(phydev, YT8511_PAGE_SELECT, YT8511_EXT_SLEEP_CTRL);
	if (ret < 0)
		goto err_restore_page;

	ret = __phy_modify(phydev, YT8511_PAGE, 0, YT8511_PLLON_SLP);
	if (ret < 0)
		goto err_restore_page;

err_restore_page:
	return phy_restore_page(phydev, oldpage, ret);
}

static u32 ytphy_read_ext(struct phy_device *phydev, u32 regnum)
{
	int ret;

	mutex_lock(&phydev->mdio.bus->mdio_lock);
	ret = __phy_write(phydev, REG_DEBUG_ADDR_OFFSET, regnum);
	if (ret < 0)
		goto err_handle;

	ret = __phy_read(phydev, REG_DEBUG_DATA);
	if (ret < 0)
		goto err_handle;

err_handle:
	mutex_unlock(&phydev->mdio.bus->mdio_lock);
	return ret;
}

static int ytphy_write_ext(struct phy_device *phydev, u32 regnum, u16 val)
{
	int ret;

	mutex_lock(&phydev->mdio.bus->mdio_lock);
	ret = __phy_write(phydev, REG_DEBUG_ADDR_OFFSET, regnum);
	if (ret < 0)
		goto err_handle;

	ret = __phy_write(phydev, REG_DEBUG_DATA, val);
	if (ret < 0)
		goto err_handle;

err_handle:
	mutex_unlock(&phydev->mdio.bus->mdio_lock);
	return ret;
}

/* ======================= S4602 bench diagnostics (2026-11-12) =======================
 * 背景：bench 环境没有网线、也没有对端交换机口，eth0 永远是 carrier=0。
 * 只看 sysfs 读数无法区分两种完全不同的故障：
 *   (a) PHY 没有有效参考时钟 / 我们的驱动把它配坏了  -> 软件问题，可修
 *   (b) 物理层根本没接                                -> 现场问题，只能等 PoE 口
 * 因此在 PHY 起来 8 秒后自动跑一组自检，只用 CL22 标准寄存器 + YT8512
 * 厂商扩展寄存器，不依赖任何外部工具：
 *
 *   T1 内部自环 (BMCR_LOOPBACK)  —— 不需要网线，判定 PHY 核心 + 参考时钟
 *   T2 强制 100M 全双工         —— 判定物理 MDI 链路是否存在
 *   T3 强制 10M 半双工
 *   T4 回滚我们对 AFE_PLL/EXTEND_COMBO 的改写（恢复出厂扩展寄存器）后再测
 *
 * 判据（现场看这一段就够）：
 *   T1 link=1 且 T2/T3 link=0 -> PHY 正常，现场没插线/对端未启用
 *   T1 link=1 且 T2 link=1    -> 链路已通
 *   T1 link=0                 -> PHY 无参考时钟或配置错，继续查时钟链
 *   T2 link=0 但 T4 link=1    -> 是我们的 PHY 时钟初始化把 PHY 配坏了
 *
 * 注：CL22 的 BMSR bit2(Link Status) 是 latch-low 语义，读一次会被清掉，
 *     第二次读才是实时值 —— 所以判定链路必须连续读两次。
 * ========================================================================== */
#define S4602_DIAG_TAG "s4602-phy-diag: "

/* mii.h 没有 ANADV(0x07) 的宏，这里自己定义 */
#define S4602_MII_ANADV		0x07

struct s4602_phy_diag {
	struct delayed_work work;
	struct phy_device *phydev;
	/* yt8512_clk_init() 改写之前的厂商扩展寄存器原值，用于 T4 回滚 */
	int orig_pll;
	int orig_combo;
	int have_orig;
};

static struct s4602_phy_diag s4602_diag;

static int s4602_mdio_read(struct phy_device *phydev, u32 regnum)
{
	int ret;

	mutex_lock(&phydev->mdio.bus->mdio_lock);
	ret = __phy_read(phydev, regnum);
	mutex_unlock(&phydev->mdio.bus->mdio_lock);

	return ret;
}

static int s4602_mdio_write(struct phy_device *phydev, u32 regnum, u16 val)
{
	int ret;

	mutex_lock(&phydev->mdio.bus->mdio_lock);
	ret = __phy_write(phydev, regnum, val);
	mutex_unlock(&phydev->mdio.bus->mdio_lock);

	return ret;
}

/* 连续读两次 BMSR，第二次才是实时链路状态 */
static int s4602_bmsr_live(struct phy_device *phydev)
{
	int first, second;

	first = s4602_mdio_read(phydev, MII_BMSR);
	second = s4602_mdio_read(phydev, MII_BMSR);
	if (first < 0 || second < 0)
		return -EIO;

	return second;
}

static void s4602_dump(struct phy_device *phydev, const char *tag)
{
	int bmcr, bmsr, anar, anlp, aner, anadv, spec;
	int pll, combo, led0, led1, slp;

	bmcr  = s4602_mdio_read(phydev, MII_BMCR);
	bmsr  = s4602_bmsr_live(phydev);
	anar  = s4602_mdio_read(phydev, MII_ADVERTISE);
	anlp  = s4602_mdio_read(phydev, MII_LPA);
	aner  = s4602_mdio_read(phydev, MII_EXPANSION);
	anadv = s4602_mdio_read(phydev, S4602_MII_ANADV);
	spec  = s4602_mdio_read(phydev, REG_PHY_SPEC_STATUS);

	if (bmsr < 0)
		bmsr = 0;
	if (bmcr < 0)
		bmcr = 0;
	if (anar < 0)
		anar = 0;
	if (anlp < 0)
		anlp = 0;
	if (aner < 0)
		aner = 0;
	if (anadv < 0)
		anadv = 0;
	if (spec < 0)
		spec = 0;

	pr_info(S4602_DIAG_TAG "%-9s BMCR=0x%04x BMSR=0x%04x(link=%d aneg_done=%d)\n",
		tag, (u16)bmcr, (u16)bmsr,
		!!(bmsr & BMSR_LSTATUS), !!(bmsr & BMSR_ANEGCOMPLETE));
	pr_info(S4602_DIAG_TAG "%-9s ANAR=0x%04x ANLP=0x%04x ANER=0x%04x ANADV=0x%04x SPEC=0x%04x\n",
		tag, (u16)anar, (u16)anlp, (u16)aner, (u16)anadv, (u16)spec);

	pll   = (int)ytphy_read_ext(phydev, YT8512_EXTREG_AFE_PLL) & 0xffff;
	combo = (int)ytphy_read_ext(phydev, YT8512_EXTREG_EXTEND_COMBO) & 0xffff;
	led0  = (int)ytphy_read_ext(phydev, YT8512_EXTREG_LED0) & 0xffff;
	led1  = (int)ytphy_read_ext(phydev, YT8512_EXTREG_LED1) & 0xffff;
	slp   = (int)ytphy_read_ext(phydev, YT8512_EXTREG_SLEEP_CONTROL1) & 0xffff;

	pr_info(S4602_DIAG_TAG "%-9s ext AFE_PLL(0x50)=0x%04x EXTEND_COMBO(0x4000)=0x%04x\n",
		tag, pll, combo);
	pr_info(S4602_DIAG_TAG "%-9s ext LED0(0x40c0)=0x%04x LED1(0x40c3)=0x%04x SLEEP(0x2027)=0x%04x\n",
		tag, led0, led1, slp);
}

/*
 * 强制速率下测链路：关自协商、锁速率/双工，再连续读 BMSR。
 * 返回 1=有链路 0=无链路 -1=读写失败。
 * 把读回的 BMCR 一起打出来：若与我们要写的不一致，说明 phylib 轮询线程
 * 在测试窗口里把 BMCR 改回去了，此时 link 结论不可信（会标 POLLER-CLOBBERED）。
 */
static int s4602_force_link(struct phy_device *phydev, u16 bmcr, const char *name)
{
	int link = 0, bmsr = 0, i, bmcr_back, spec;

	s4602_mdio_write(phydev, MII_BMCR, bmcr);
	msleep(200);

	for (i = 0; i < 5; i++) {
		bmsr = s4602_bmsr_live(phydev);
		if (bmsr < 0)
			return -1;
		if (bmsr & BMSR_LSTATUS) {
			link = 1;
			break;
		}
		msleep(50);
	}

	bmcr_back = s4602_mdio_read(phydev, MII_BMCR);
	spec = s4602_mdio_read(phydev, REG_PHY_SPEC_STATUS);

	if (bmcr_back < 0)
		bmcr_back = 0;
	if (spec < 0)
		spec = 0;

	pr_info(S4602_DIAG_TAG "T %-15s want=0x%04x got=0x%04x BMSR=0x%04x link=%d SPEC=0x%04x%s\n",
		name, bmcr, (u16)bmcr_back, (u16)bmsr, link, (u16)spec,
		((u16)bmcr_back != bmcr) ? " [POLLER-CLOBBERED]" : "");

	return link;
}

static void s4602_phy_diag_work(struct work_struct *work)
{
	struct s4602_phy_diag *diag =
		container_of(to_delayed_work(work), struct s4602_phy_diag, work);
	struct phy_device *phydev = diag->phydev;
	int link_loop, link_100, link_10, link_rb;

	if (!phydev)
		return;

	pr_info(S4602_DIAG_TAG "===================== BEGIN =====================\n");

	s4602_dump(phydev, "idle");

	/* T1 内部自环：不需要网线，判定 PHY 核心与参考时钟是否正常 */
	link_loop = s4602_force_link(phydev,
			BMCR_LOOPBACK | BMCR_SPEED100 | BMCR_FULLDPLX,
			"loopback-100fd");
	pr_info(S4602_DIAG_TAG "T1 loopback link=%d => %s\n", link_loop,
		link_loop ? "PHY 核心/参考时钟 OK" : "PHY 无参考时钟或配置错误");

	/* T2/T3 强制速率：判定物理 MDI 链路是否存在 */
	link_100 = s4602_force_link(phydev, BMCR_SPEED100 | BMCR_FULLDPLX,
			"force-100fd");
	pr_info(S4602_DIAG_TAG "T2 force-100fd link=%d\n", link_100);

	link_10 = s4602_force_link(phydev, BMCR_SPEED10, "force-10hd");
	pr_info(S4602_DIAG_TAG "T3 force-10hd link=%d\n", link_10);

	/*
	 * T4 回滚厂商扩展寄存器：去掉 PLL_REFCLK_SEL_EN / RMII_EN，
	 * 恢复成 attach 时读到的原值，再测一次 —— A/B 我们的时钟初始化。
	 */
	link_rb = -1;
	if (diag->have_orig) {
		ytphy_write_ext(phydev, YT8512_EXTREG_AFE_PLL,
				(u16)diag->orig_pll);
		ytphy_write_ext(phydev, YT8512_EXTREG_EXTEND_COMBO,
				(u16)diag->orig_combo);
		msleep(500);
		s4602_dump(phydev, "rollback");
		link_rb = s4602_force_link(phydev, BMCR_SPEED100 | BMCR_FULLDPLX,
				"rollback-100fd");
		pr_info(S4602_DIAG_TAG "T4 rollback link=%d (T2 was %d)\n",
			link_rb, link_100);

		if (link_rb == 1 && link_100 == 0) {
			/* 回滚反而通了：保留出厂扩展寄存器配置 */
			pr_info(S4602_DIAG_TAG "AUTO-FIX: 保留回滚后的扩展寄存器配置\n");
		} else {
			/* 恢复我们自己的配置（在原值基础上重新置位） */
			ytphy_write_ext(phydev, YT8512_EXTREG_AFE_PLL,
					(u16)(diag->orig_pll |
					      YT8512_CONFIG_PLL_REFCLK_SEL_EN));
			ytphy_write_ext(phydev, YT8512_EXTREG_EXTEND_COMBO,
					(u16)(diag->orig_combo |
					      YT8512_CONTROL1_RMII_EN));
			msleep(200);
		}
	}

	/* 恢复自协商 */
	s4602_mdio_write(phydev, MII_BMCR, BMCR_ANENABLE | BMCR_ANRESTART);
	msleep(300);
	s4602_dump(phydev, "restore");

	pr_info(S4602_DIAG_TAG "====================== END ======================\n");
	pr_info(S4602_DIAG_TAG "SUMMARY: T1loop=%d T2force100=%d T3force10=%d T4rollback=%d\n",
		link_loop, link_100, link_10, link_rb);
}

static int __maybe_unused ytphy_write_mmd(struct phy_device *phydev, u16 device, u16 reg, u16 value)
{
	int ret = 0;

	mutex_lock(&phydev->mdio.bus->mdio_lock);

	__phy_write(phydev, REG_MII_MMD_CTRL, device);
	__phy_write(phydev, REG_MII_MMD_DATA, reg);
	__phy_write(phydev, REG_MII_MMD_CTRL, device | 0x4000);
	__phy_write(phydev, REG_MII_MMD_DATA, value);

	mutex_unlock(&phydev->mdio.bus->mdio_lock);

	return ret;
}

static int yt8011_probe(struct phy_device *phydev)
{
	struct device *dev = &phydev->mdio.dev;
	struct yt8011_priv *priv;
	int chip_config;

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	phydev->priv = priv;

	/* ext reg 0x9030 bit0
	 * 0 = chip works in RGMII mode; 1 = chip works in SGMII mode
	 */
	chip_config = ytphy_read_ext(phydev, 0x9030);
	priv->chip_mode = chip_config & 0x1;

	return 0;
}

static int yt8011_config_aneg(struct phy_device *phydev)
{
	phydev->speed = SPEED_1000;

	return 0;
}

static int yt8011_config_vddio(struct phy_device *phydev)
{
	struct yt8011_priv *priv = phydev->priv;

	if (!(priv->chip_mode)) { /* rgmii config */
		switch (priv->vddio) {
		case YT8011_RGMII_DVDDIO_2V5:
			dev_info(&phydev->mdio.dev, "config PHY vddio 2v5\n");
			ytphy_write_ext(phydev, 0x9000, 0x8000);
			ytphy_write_ext(phydev, 0x0062, 0x0000);
			ytphy_write_ext(phydev, 0x9000, 0x0000);
			ytphy_write_ext(phydev, 0x9031, 0xb200);
			ytphy_write_ext(phydev, 0x9111, 0x5);
			ytphy_write_ext(phydev, 0x9114, 0x3939);
			ytphy_write_ext(phydev, 0x9112, 0xf);
			ytphy_write_ext(phydev, 0x9110, 0x0);
			ytphy_write_ext(phydev, 0x9113, 0x10);
			ytphy_write_ext(phydev, 0x903d, 0x2);
			break;
		case YT8011_RGMII_DVDDIO_1V8:
			dev_info(&phydev->mdio.dev, "config PHY for 1v8\n");
			ytphy_write_ext(phydev, 0x9000, 0x8000);
			ytphy_write_ext(phydev, 0x0062, 0x0000);
			ytphy_write_ext(phydev, 0x9000, 0x0000);
			ytphy_write_ext(phydev, 0x9031, 0xb200);
			ytphy_write_ext(phydev, 0x9116, 0x6);
			ytphy_write_ext(phydev, 0x9119, 0x3939);
			ytphy_write_ext(phydev, 0x9117, 0xf);
			ytphy_write_ext(phydev, 0x9115, 0x0);
			ytphy_write_ext(phydev, 0x9118, 0x20);
			ytphy_write_ext(phydev, 0x903d, 0x3);
			break;
		case YT8011_RGMII_DVDDIO_3V3:
		default:
			dev_info(&phydev->mdio.dev, "config PHY for 3v3\n");
			ytphy_write_ext(phydev, 0x9000, 0x8000);
			ytphy_write_ext(phydev, 0x0062, 0x0000);
			ytphy_write_ext(phydev, 0x9000, 0x0000);
			ytphy_write_ext(phydev, 0x9031, 0xb200);
			ytphy_write_ext(phydev, 0x903b, 0x0040);
			ytphy_write_ext(phydev, 0x903e, 0x3b3b);
			ytphy_write_ext(phydev, 0x903c, 0xf);
			ytphy_write_ext(phydev, 0x903d, 0x1000);
			ytphy_write_ext(phydev, 0x9038, 0x0000);
			break;
		}
	}
	return 0;
}
static int yt8011_aneg_done(struct phy_device *phydev)
{
	int link_utp = 0;

	/* UTP */
	ytphy_write_ext(phydev, 0x9000, 0);
	link_utp = !!(phy_read(phydev, REG_PHY_SPEC_STATUS) & (BIT(YT8011_LINK_STATUS_BIT)));

	return !!(link_utp);
}

static int yt8011_automotive_adjust_status(struct phy_device *phydev, int val)
{
	int speed_mode;
	int speed = SPEED_UNKNOWN;

	speed_mode = (val & YT8011_SPEED_MODE) >> YT8011_SPEED_MODE_BIT;
	switch (speed_mode) {
	case 1:
		speed = SPEED_100;
		break;
	case 2:
		speed = SPEED_1000;
		break;
	default:
		speed = SPEED_UNKNOWN;
		break;
	}

	phydev->speed = speed;
	phydev->duplex = DUPLEX_FULL;

	return 0;
}

static int yt8011_read_status(struct phy_device *phydev)
{
	int ret;
	int val;
	int link;
	int link_utp = 0;

	/* UTP */
	ret = ytphy_write_ext(phydev, 0x9000, 0x0);
	if (ret < 0)
		return ret;

	val = phy_read(phydev, REG_PHY_SPEC_STATUS);
	if (val < 0)
		return val;

	link = val & (BIT(YT8011_LINK_STATUS_BIT));
	if (link) {
		link_utp = 1;
		yt8011_automotive_adjust_status(phydev, val);
	} else {
		link_utp = 0;
	}

	if (link_utp) {
		if (phydev->link == 0)
			phydev->link = 1;
	} else {
		if (phydev->link == 1)
			phydev->link = 0;
	}

	if (link_utp)
		ytphy_write_ext(phydev, 0x9000, 0x0);

	return 0;
}

static int ytphy_soft_reset(struct phy_device *phydev)
{
	int ret = 0, val = 0;

	val = phy_read(phydev, MII_BMCR);
	if (val < 0)
		return val;

	ret = phy_write(phydev, MII_BMCR, val | BMCR_RESET);
	if (ret < 0)
		return ret;

	return ret;
}

static int yt8011_soft_reset(struct phy_device *phydev)
{
	struct yt8011_priv *priv = phydev->priv;

	/* utp */
	ytphy_write_ext(phydev, 0x9000, 0x0);
	ytphy_soft_reset(phydev);

	if (priv->chip_mode) { /* sgmm */
		ytphy_write_ext(phydev, 0x9000, 0x8000);
		ytphy_soft_reset(phydev);

		/* restore utp space */
		ytphy_write_ext(phydev, 0x9000, 0x0);
	}

	return 0;
}

static int yt8512_clk_init(struct phy_device *phydev)
{
	struct device_node *node = phydev->mdio.dev.of_node;
	const char *strings = NULL;
	int ret;
	int val;

	if (node && node->parent && node->parent->parent) {
		ret = of_property_read_string(node->parent->parent,
					      "clock_in_out", &strings);
		if (!ret) {
			if (!strcmp(strings, "input"))
				return 0;
		}
	}

	val = ytphy_read_ext(phydev, YT8512_EXTREG_AFE_PLL);
	if (val < 0)
		return val;

	/* 保存原值：S4602 诊断的 T4 要用它做 A/B 回滚 */
	s4602_diag.orig_pll = val & 0xffff;
	s4602_diag.have_orig = 1;

	val |= YT8512_CONFIG_PLL_REFCLK_SEL_EN;

	ret = ytphy_write_ext(phydev, YT8512_EXTREG_AFE_PLL, val);
	if (ret < 0)
		return ret;

	val = ytphy_read_ext(phydev, YT8512_EXTREG_EXTEND_COMBO);
	if (val < 0)
		return val;

	s4602_diag.orig_combo = val & 0xffff;

	val |= YT8512_CONTROL1_RMII_EN;

	ret = ytphy_write_ext(phydev, YT8512_EXTREG_EXTEND_COMBO, val);
	if (ret < 0)
		return ret;

	val = phy_read(phydev, MII_BMCR);
	if (val < 0)
		return val;

	val |= YT_SOFTWARE_RESET;
	ret = phy_write(phydev, MII_BMCR, val);

	return ret;
}

static int yt8512_led_init(struct phy_device *phydev)
{
	int ret;
	int val;
	int mask;

	val = ytphy_read_ext(phydev, YT8512_EXTREG_LED0);
	if (val < 0)
		return val;

	val |= YT8512_LED0_ACT_BLK_IND;

	mask = YT8512_LED0_DIS_LED_AN_TRY | YT8512_LED0_BT_BLK_EN |
		YT8512_LED0_HT_BLK_EN | YT8512_LED0_COL_BLK_EN |
		YT8512_LED0_BT_ON_EN;
	val &= ~mask;

	ret = ytphy_write_ext(phydev, YT8512_EXTREG_LED0, val);
	if (ret < 0)
		return ret;

	val = ytphy_read_ext(phydev, YT8512_EXTREG_LED1);
	if (val < 0)
		return val;

	val |= YT8512_LED1_BT_ON_EN;

	mask = YT8512_LED1_TXACT_BLK_EN | YT8512_LED1_RXACT_BLK_EN;
	val &= ~mask;

	/* Rockchip BSP typo: used the bitmask YT8512_LED1_BT_ON_EN (0x0010)
	 * as the extended-register address. Must be YT8512_EXTREG_LED1
	 * (0x40c3) or we corrupt an unrelated register. */
	ret = ytphy_write_ext(phydev, YT8512_EXTREG_LED1, val);

	return ret;
}

static int yt8011_config_init(struct phy_device *phydev)
{
	struct yt8011_priv *priv = phydev->priv;
	struct device_node *np = phydev->mdio.dev.of_node;
	const char *vddio_conf;

	phydev->autoneg = AUTONEG_DISABLE;

	if (!np) {
		dev_err(&phydev->mdio.dev, "Device Tree node is missing\n");
		priv->vddio = YT8011_RGMII_DVDDIO_3V3;
	} else {
		if (of_property_read_string(np, "motorcomm,vddio", &vddio_conf)) {
			dev_err(&phydev->mdio.dev, "Missing 'motorcomm,vddio' property in DTS, using 3v3 default\n");
			priv->vddio = YT8011_RGMII_DVDDIO_3V3;
		} else {
			if (!strcasecmp(vddio_conf, "1v8")) {
				priv->vddio = YT8011_RGMII_DVDDIO_1V8;
			} else if (!strcasecmp(vddio_conf, "2v5")) {
				priv->vddio = YT8011_RGMII_DVDDIO_2V5;
			} else if (!strcasecmp(vddio_conf, "3v3")) {
				priv->vddio = YT8011_RGMII_DVDDIO_3V3;
			} else {
				dev_err(&phydev->mdio.dev, "Invalid 'motorcomm,vddio' value, using 3v3 default\n");
				priv->vddio = YT8011_RGMII_DVDDIO_3V3;
			}
		}
	}
	/* UTP */
	ytphy_write_ext(phydev, 0x9000, 0x0);

	ytphy_write_ext(phydev, 0x1008, 0x2119);
	ytphy_write_ext(phydev, 0x1092, 0x712);
	ytphy_write_ext(phydev, 0x90bc, 0x6661);
	ytphy_write_ext(phydev, 0x90b9, 0x620b);
	ytphy_write_ext(phydev, 0x2001, 0x6418);
	ytphy_write_ext(phydev, 0x1019, 0x3712);
	ytphy_write_ext(phydev, 0x101a, 0x3713);
	ytphy_write_ext(phydev, 0x2015, 0x1012);
	ytphy_write_ext(phydev, 0x2005, 0x810);
	ytphy_write_ext(phydev, 0x2013, 0xff06);
	ytphy_write_ext(phydev, 0x1053, 0xf);
	ytphy_write_ext(phydev, 0x105e, 0xa46c);
	ytphy_write_ext(phydev, 0x1088, 0x002b);
	ytphy_write_ext(phydev, 0x1088, 0x002b);
	ytphy_write_ext(phydev, 0x1088, 0xb);
	ytphy_write_ext(phydev, 0x3008, 0x143);
	ytphy_write_ext(phydev, 0x3009, 0x1918);
	ytphy_write_ext(phydev, 0x9095, 0x1a1a);
	ytphy_write_ext(phydev, 0x9096, 0x1a10);
	ytphy_write_ext(phydev, 0x9097, 0x101a);
	ytphy_write_ext(phydev, 0x9098, 0x01ff);
	yt8011_config_vddio(phydev);

	ytphy_soft_reset(phydev);

	netdev_info(phydev->attached_dev, "%s done, phy addr: %d\n", __func__, phydev->mdio.addr);

	return 0;
}

/*
 * CMCC S4602 定制板 YT8512B PHY 初始化。
 * 序列逐字复制自原厂固件实际启动日志（eth0 成功 "Link is Up" 的那一次）：
 *   reg30=0x2027 reg31=0x2026      (ext 0x2027 <- 0x2026)
 *   reg16=0x62                    (CL22 reg 0x10 <- 0x62)
 *   reg0 =0xa100                  (BMCR 软复位 + 100M/全双工预设)
 *   reg30=0x2057 reg31=0x2a4f
 *   reg30=0x1   reg31=0x1240
 *   reg30=0x11  reg31=0x6
 *   reg30=0x12  reg31=0xa
 *   reg30=0x13  reg31=0xf
 *   reg30=0x14  reg31=0x11
 *   reg30=0x15  reg31=0x1fe
 * 原厂固件靠这套写寄存器（etc/init.d/S10phyreg_config 用户态脚本）让 PHY 在板载
 * RMII 走线下出链路；OpenIPC 之前既没跑这个脚本、内核驱动也没做这套写寄存器，
 * 所以 eth0 一直 NO-CARRIER。必须放在 clk_init 之后，作为最终生效的配置。
 *
 * 注：同型号备份 rootfs 里的脚本该行是 "0x10 0x2"（reg16=0x2），与启动日志的 0x62
 * 不一致。这里以实测 Link is Up 的启动日志 0x62 为准；若个别批次实测用 0x2，把
 * 下面 0x62 改成 0x2 即可。
 */
static int yt8512_s4602_phy_init(struct phy_device *phydev)
{
	int ret;

	ret = ytphy_write_ext(phydev, 0x2027, 0x2026);
	if (ret < 0)
		return ret;
	ret = phy_write(phydev, 0x10, 0x62);
	if (ret < 0)
		return ret;
	ret = phy_write(phydev, MII_BMCR, 0xa100);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x2057, 0x2a4f);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x0001, 0x1240);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x0011, 0x0006);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x0012, 0x000a);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x0013, 0x000f);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x0014, 0x0011);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0x0015, 0x01fe);
	if (ret < 0)
		return ret;

	return 0;
}

static int yt8512_config_init(struct phy_device *phydev)
{
	int ret;
	int val;

	ret = yt8512_clk_init(phydev);
	if (ret < 0)
		return ret;

	ret = yt8512_led_init(phydev);
	if (ret < 0)
		return ret;

	/* CMCC S4602 定制板 PHY 初始化：逐字复制原厂固件启动日志里的写寄存器序列
	 * （原厂靠 etc/init.d/S10phyreg_config 用户态脚本做这件事，OpenIPC 之前缺了
	 * 它，所以 eth0 一直 NO-CARRIER）。必须放在 clk_init 之后，作为最终生效配置。 */
	ret = yt8512_s4602_phy_init(phydev);
	if (ret < 0)
		return ret;

	/* disable auto sleep */
	val = ytphy_read_ext(phydev, YT8512_EXTREG_SLEEP_CONTROL1);
	if (val < 0)
		return val;

	val &= (~BIT(YT8512_EN_SLEEP_SW_BIT));

	ret = ytphy_write_ext(phydev, YT8512_EXTREG_SLEEP_CONTROL1, val);
	if (ret < 0)
		return ret;

	/*
	 * S4602 bench 诊断：8 秒后自动跑一遍 PHY 自检
	 * （内部自环 / 强制速率 / 扩展寄存器回滚 A-B）。
	 * 现场只要看这一段日志就能判定"是没插线"还是"PHY 坏了"。
	 */
	s4602_diag.phydev = phydev;
	INIT_DELAYED_WORK(&s4602_diag.work, s4602_phy_diag_work);
	schedule_delayed_work(&s4602_diag.work, msecs_to_jiffies(8000));

	return ret;
}

static int yt8512_read_status(struct phy_device *phydev)
{
	int ret;
	int val;
	int speed, speed_mode, duplex;

	ret = genphy_update_link(phydev);
	if (ret)
		return ret;

	val = phy_read(phydev, REG_PHY_SPEC_STATUS);
	if (val < 0)
		return val;

	duplex = (val & YT8512_DUPLEX) >> YT8512_DUPLEX_BIT;
	speed_mode = (val & YT8512_SPEED_MODE) >> YT8512_SPEED_MODE_BIT;
	switch (speed_mode) {
	case 0:
		speed = SPEED_10;
		break;
	case 1:
		speed = SPEED_100;
		break;
	case 2:
	case 3:
	default:
		speed = SPEED_UNKNOWN;
		break;
	}

	phydev->speed = speed;
	phydev->duplex = duplex;

	return 0;
}

static int yt8521_soft_reset(struct phy_device *phydev)
{
	int ret = 0, val;

	if (YT8521_PHY_MODE_CURR == YT8521_PHY_MODE_UTP) {
		ytphy_write_ext(phydev, 0xa000, 0);
		ret = ytphy_soft_reset(phydev);
		if (ret < 0)
			return ret;
	}

	if (YT8521_PHY_MODE_CURR == YT8521_PHY_MODE_FIBER) {
		ytphy_write_ext(phydev, 0xa000, 2);
		ret = ytphy_soft_reset(phydev);
		if (ret < 0)
			return ret;

		ytphy_write_ext(phydev, 0xa000, 0);
	}

	if (YT8521_PHY_MODE_CURR == YT8521_PHY_MODE_POLL) {
		val = ytphy_read_ext(phydev, 0xa001);
		ytphy_write_ext(phydev, 0xa001, (val & ~0x8000));

		ytphy_write_ext(phydev, 0xa000, 0);
		ret = ytphy_soft_reset(phydev);
		if (ret < 0)
			return ret;
	}

	return 0;
}

static int yt8521_hw_strap_polling(struct phy_device *phydev)
{
	int val = 0;

	val = ytphy_read_ext(phydev, 0xa001) & 0x7;
	switch (val) {
	case 1:
	case 4:
	case 5:
		return YT8521_PHY_MODE_FIBER;
	case 2:
	case 6:
	case 7:
		return YT8521_PHY_MODE_POLL;
	case 3:
	case 0:
	default:
		return YT8521_PHY_MODE_UTP;
	}
}

static int yt8521_config_init(struct phy_device *phydev)
{
	int ret, hw_strap_mode;
	int val;

#if (YTPHY_WOL_FEATURE_ENABLE)
	struct ethtool_wolinfo wol;

	/* set phy wol enable */
	memset(&wol, 0x0, sizeof(struct ethtool_wolinfo));
	wol.wolopts |= WAKE_MAGIC;
	ytphy_wol_feature_set(phydev, &wol);
#endif

	phydev->irq = PHY_POLL;
	/* NOTE: this function should not be called more than one for each chip. */
	hw_strap_mode = ytphy_read_ext(phydev, 0xa001) & 0x7;

	ytphy_write_ext(phydev, 0xa000, 0);

	/* disable auto sleep */
	val = ytphy_read_ext(phydev, YT8521_EXTREG_SLEEP_CONTROL1);
	if (val < 0)
		return val;

	val &= (~BIT(YT8521_EN_SLEEP_SW_BIT));
	ret = ytphy_write_ext(phydev, YT8521_EXTREG_SLEEP_CONTROL1, val);
	if (ret < 0)
		return ret;

	/* enable RXC clock when no wire plug */
	val = ytphy_read_ext(phydev, 0xc);
	if (val < 0)
		return val;
	val &= ~(1 << 12);
	ret = ytphy_write_ext(phydev, 0xc, val);
	if (ret < 0)
		return ret;

	netdev_info(phydev->attached_dev, "%s done, phy addr: %d, strap mode = %d, polling mode = %d\n",
		    __func__, phydev->mdio.addr, hw_strap_mode, yt8521_hw_strap_polling(phydev));

	return ret;
}

/* for fiber mode, there is no 10M speed mode and
 * this function is for this purpose.
 */
static int yt8521_adjust_status(struct phy_device *phydev, int val, int is_utp)
{
	int speed = SPEED_UNKNOWN;
	int speed_mode, duplex;

	if (is_utp)
		duplex = (val & YT8521_DUPLEX) >> YT8521_DUPLEX_BIT;
	else
		duplex = 1;
	speed_mode = (val & YT8521_SPEED_MODE) >> YT8521_SPEED_MODE_BIT;
	switch (speed_mode) {
	case 0:
		if (is_utp)
			speed = SPEED_10;
		break;
	case 1:
		speed = SPEED_100;
		break;
	case 2:
		speed = SPEED_1000;
		break;
	case 3:
		break;
	default:
		speed = SPEED_UNKNOWN;
		break;
	}

	phydev->speed = speed;
	phydev->duplex = duplex;

	return 0;
}

/* for fiber mode, when speed is 100M, there is no definition for
 * autonegotiation, and this function handles this case and return
 * 1 per linux kernel's polling.
 */
static int yt8521_aneg_done(struct phy_device *phydev)
{
	int link_fiber = 0, link_utp = 0;

	/* reading Fiber */
	ytphy_write_ext(phydev, 0xa000, 2);
	link_fiber = !!(phy_read(phydev, REG_PHY_SPEC_STATUS) & (BIT(YT8521_LINK_STATUS_BIT)));

	/* reading UTP */
	ytphy_write_ext(phydev, 0xa000, 0);
	if (!link_fiber)
		link_utp = !!(phy_read(phydev, REG_PHY_SPEC_STATUS) & (BIT(YT8521_LINK_STATUS_BIT)));

	netdev_info(phydev->attached_dev, "%s, phy addr: %d, link_fiber: %d, link_utp: %d\n",
		    __func__, phydev->mdio.addr, link_fiber, link_utp);
	return !!(link_fiber | link_utp);
}

static int yt8521_read_status(struct phy_device *phydev)
{
	int link_utp = 0, link_fiber = 0;
	int yt8521_fiber_latch_val;
	int yt8521_fiber_curr_val;
	int link, ret;
	int val;

	if (YT8521_PHY_MODE_CURR != YT8521_PHY_MODE_FIBER) {
		/* reading UTP */
		ret = ytphy_write_ext(phydev, 0xa000, 0);
		if (ret < 0)
			return ret;

		val = phy_read(phydev, REG_PHY_SPEC_STATUS);
		if (val < 0)
			return val;

		link = val & (BIT(YT8521_LINK_STATUS_BIT));
		if (link) {
			link_utp = 1;
			yt8521_adjust_status(phydev, val, 1);
		} else {
			link_utp = 0;
		}
	}

	if (YT8521_PHY_MODE_CURR != YT8521_PHY_MODE_UTP) {
		/* reading Fiber */
		ret = ytphy_write_ext(phydev, 0xa000, 2);
		if (ret < 0)
			return ret;

		val = phy_read(phydev, REG_PHY_SPEC_STATUS);
		if (val < 0)
			return val;

		/* note: below debug information is used to check multiple PHy ports. */

		/* for fiber, from 1000m to 100m, there is not link down from 0x11,
		 * and check reg 1 to identify such case this is important for Linux
		 * kernel for that, missing linkdown event will cause problem.
		 */
		yt8521_fiber_latch_val = phy_read(phydev, MII_BMSR);
		yt8521_fiber_curr_val = phy_read(phydev, MII_BMSR);
		link = val & (BIT(YT8521_LINK_STATUS_BIT));
		if (link && yt8521_fiber_latch_val != yt8521_fiber_curr_val) {
			link = 0;
			netdev_info(phydev->attached_dev, "%s, phy addr: %d, fiber link down detect, latch = %04x, curr = %04x\n",
				    __func__, phydev->mdio.addr, yt8521_fiber_latch_val,
				    yt8521_fiber_curr_val);
		}

		if (link) {
			link_fiber = 1;
			yt8521_adjust_status(phydev, val, 0);
		} else {
			link_fiber = 0;
		}
	}

	if (link_utp || link_fiber) {
		if (phydev->link == 0)
			netdev_info(phydev->attached_dev, "%s, phy addr: %d, link up, media: %s, mii reg 0x11 = 0x%x\n",
				    __func__, phydev->mdio.addr,
				    (link_utp && link_fiber) ? "UNKNOWN MEDIA" : (link_utp ? "UTP" : "Fiber"),
				    (unsigned int)val);
		phydev->link = 1;
	} else {
		if (phydev->link == 1)
			netdev_info(phydev->attached_dev, "%s, phy addr: %d, link down\n",
				    __func__, phydev->mdio.addr);
		phydev->link = 0;
	}

	/* utp or combo */
	if (YT8521_PHY_MODE_CURR != YT8521_PHY_MODE_FIBER) {
		if (link_fiber)
			ytphy_write_ext(phydev, 0xa000, 2);
		if (link_utp)
			ytphy_write_ext(phydev, 0xa000, 0);
	}

	return 0;
}

static int yt8521_suspend(struct phy_device *phydev)
{
#if !(SYS_WAKEUP_BASED_ON_ETH_PKT)
	int value;

	ytphy_write_ext(phydev, 0xa000, 0);
	value = phy_read(phydev, MII_BMCR);
	phy_write(phydev, MII_BMCR, value | BMCR_PDOWN);

	ytphy_write_ext(phydev, 0xa000, 2);
	value = phy_read(phydev, MII_BMCR);
	phy_write(phydev, MII_BMCR, value | BMCR_PDOWN);

	ytphy_write_ext(phydev, 0xa000, 0);
#endif /*!(SYS_WAKEUP_BASED_ON_ETH_PKT)*/

	return 0;
}

static int yt8521_resume(struct phy_device *phydev)
{
	int value, ret;

	/* disable auto sleep */
	value = ytphy_read_ext(phydev, YT8521_EXTREG_SLEEP_CONTROL1);
	if (value < 0)
		return value;

	value &= (~BIT(YT8521_EN_SLEEP_SW_BIT));

	ret = ytphy_write_ext(phydev, YT8521_EXTREG_SLEEP_CONTROL1, value);
	if (ret < 0)
		return ret;

#if !(SYS_WAKEUP_BASED_ON_ETH_PKT)
	if (YT8521_PHY_MODE_CURR != YT8521_PHY_MODE_FIBER) {
		ytphy_write_ext(phydev, 0xa000, 0);
		value = phy_read(phydev, MII_BMCR);
		phy_write(phydev, MII_BMCR, value & ~BMCR_PDOWN);
	}

	if (YT8521_PHY_MODE_CURR != YT8521_PHY_MODE_UTP) {
		ytphy_write_ext(phydev, 0xa000, 2);
		value = phy_read(phydev, MII_BMCR);
		phy_write(phydev, MII_BMCR, value & ~BMCR_PDOWN);

		ytphy_write_ext(phydev, 0xa000, 0);
	}
#endif /*!(SYS_WAKEUP_BASED_ON_ETH_PKT)*/

	return 0;
}

static int yt8522_read_status(struct phy_device *phydev)
{
	int speed, speed_mode, duplex, val;

	genphy_read_status(phydev);
	val = phy_read(phydev, REG_PHY_SPEC_STATUS);
	if (val < 0)
		return val;

	/* link up */
	if ((val & BIT(10)) >> YTXXXX_LINK_STATUS_BIT) {
		duplex = (val & BIT(13)) >> YTXXXX_DUPLEX_BIT;
		speed_mode = (val & (BIT(15) | BIT(14))) >> YTXXXX_SPEED_MODE_BIT;
		switch (speed_mode) {
		case 0:
			speed = SPEED_10;
			break;
		case 1:
			speed = SPEED_100;
			break;
		case 2:
		case 3:
		default:
			speed = SPEED_UNKNOWN;
			break;
		}

		phydev->link = 1;
		phydev->speed = speed;
		phydev->duplex = duplex;

		return 0;
	}

	phydev->link = 0;

	return 0;
}

static int yt8522_probe(struct phy_device *phydev)
{
	struct device *dev = &phydev->mdio.dev;
	struct yt8xxx_priv *priv;
	int chip_config;

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	phydev->priv = priv;

	chip_config = ytphy_read_ext(phydev, YT8522_EXTENDED_COMBO_CTRL_1);

	priv->chip_mode = ((chip_config & BIT(3)) >> 3);

	return 0;
}

static int yt8522_config_init(struct phy_device *phydev)
{
	struct yt8xxx_priv *priv = phydev->priv;
	int ret;
	int val;

	/* UTP */
	if (!priv->chip_mode) {
		val = ytphy_write_ext(phydev, YT8522_TX_CLK_DELAY, 0);
		if (val < 0)
			return val;

		val = ytphy_write_ext(phydev, YT8522_ANAGLOG_IF_CTRL, 0xbf2a);
		if (val < 0)
			return val;

		val = ytphy_write_ext(phydev, YT8522_DAC_CTRL, 0x297f);
		if (val < 0)
			return val;

		val = ytphy_write_ext(phydev, YT8522_INTERPOLATOR_FILTER_1, 0x1FE);
		if (val < 0)
			return val;

		val = ytphy_write_ext(phydev, YT8522_INTERPOLATOR_FILTER_2, 0x1FE);
		if (val < 0)
			return val;

		/* disable auto sleep */
		val = ytphy_read_ext(phydev, YT8512_EXTREG_SLEEP_CONTROL1);
		if (val < 0)
			return val;

		val &= (~BIT(YT8512_EN_SLEEP_SW_BIT));

		ret = ytphy_write_ext(phydev, YT8512_EXTREG_SLEEP_CONTROL1, val);
		if (ret < 0)
			return ret;

		ytphy_soft_reset(phydev);
	}

	return 0;
}

static int yt8531_rxclk_duty_init(struct phy_device *phydev)
{
	unsigned int value = 0x9696;
	int ret = 0;

	ret = ytphy_write_ext(phydev, 0xa040, 0xffff);
	if (ret < 0)
		return ret;

	ret = ytphy_write_ext(phydev, 0xa041, 0xff);
	if (ret < 0)
		return ret;

	ret = ytphy_write_ext(phydev, 0xa039, 0xbf00);
	if (ret < 0)
		return ret;

	/* nodelay duty = 0x9696 (default)
	 * fixed delay duty = 0x4040
	 * step delay 0xf duty = 0x4041
	 */
	if (phydev->interface == PHY_INTERFACE_MODE_RGMII_ID ||
	    phydev->interface == PHY_INTERFACE_MODE_RGMII_RXID)
		value = 0x4040;

	ret = ytphy_write_ext(phydev, 0xa03a, value);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0xa03b, value);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0xa03c, value);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0xa03d, value);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0xa03e, value);
	if (ret < 0)
		return ret;
	ret = ytphy_write_ext(phydev, 0xa03f, value);
	if (ret < 0)
		return ret;

	return ret;
}

static int yt8531S_config_init(struct phy_device *phydev)
{
#if (YTPHY8531A_XTAL_INIT)
	int ret = 0;

	ret = yt8531a_xtal_init(phydev);
	if (ret < 0)
		return ret;
#endif

	return yt8521_config_init(phydev);
}

static int yt8531_config_init(struct phy_device *phydev)
{
	int ret = 0, val;

#if (YTPHY8531A_XTAL_INIT)
	ret = yt8531a_xtal_init(phydev);
	if (ret < 0)
		return ret;
#endif

	/* PHY_CLK_OUT 125M enabled (default) */
	ret = ytphy_write_ext(phydev, 0xa012, 0xd0);
	if (ret < 0)
		return ret;

	ret = yt8531_rxclk_duty_init(phydev);
	if (ret < 0)
		return ret;

	/* RXC, PHY_CLK_OUT and RXData Drive strength:
	 * Drive strength of RXC = 6, PHY_CLK_OUT = 3, RXD0 = 4 (default 1.8v)
	 * If the io voltage is 3.3v, PHY_CLK_OUT = 2, set 0xa010 = 0xdacf
	 */
	ret = ytphy_write_ext(phydev, 0xa010, 0xdbcf);
	if (ret < 0)
		return ret;

	/* Change 100M default BGS voltage from 0x294c to 0x274c */
	val = ytphy_read_ext(phydev, 0x57);
	val = (val & ~(0xf << 8)) | (7 << 8);
	ret = ytphy_write_ext(phydev, 0x57, val);
	if (ret < 0)
		return ret;

	return ret;
}

static struct phy_driver motorcomm_phy_drvs[] = {
	{
		PHY_ID_MATCH_EXACT(PHY_ID_YT8011),
		.name		= "YT8011 Automotive Gigabit Ethernet",
		.features	= PHY_GBIT_FEATURES,
		.flags		= PHY_POLL,
		.probe		= yt8011_probe,
		.config_aneg	= yt8011_config_aneg,
		.aneg_done	= yt8011_aneg_done,
		.config_init	= yt8011_config_init,
		.read_status	= yt8011_read_status,
		.soft_reset	= yt8011_soft_reset,
	}, 	{
		PHY_ID_MATCH_EXACT(PHY_ID_YT8511),
		.name		= "YT8511 Gigabit Ethernet",
		/* 4.19 phylib does NOT auto-probe abilities (no get_features
		 * callback); phy_probe() copies drv->features verbatim into
		 * phydev->supported. Without an explicit mask the PHY would
		 * advertise nothing and never link. */
		.features	= PHY_GBIT_FEATURES,
		.config_init	= yt8511_config_init,
		.suspend	= genphy_suspend,
		.resume		= genphy_resume,
		.read_page	= yt8511_read_page,
		.write_page	= yt8511_write_page,
	}, {
		PHY_ID_MATCH_EXACT(PHY_ID_YT8512),
		.name		= "YT8512 Ethernet",
		.features	= PHY_BASIC_FEATURES,
		.config_init	= yt8512_config_init,
		.read_status	= yt8512_read_status,
		.suspend	= genphy_suspend,
		.resume		= genphy_resume,
	}, {
		PHY_ID_MATCH_EXACT(PHY_ID_YT8512B),
		.name		= "YT8512B Ethernet",
		/* S4602's PHY: 10/100 Mbit RMII. 4.19 needs this explicitly. */
		.features	= PHY_BASIC_FEATURES,
		.config_init	= yt8512_config_init,
		.read_status	= yt8512_read_status,
		.suspend	= genphy_suspend,
		.resume		= genphy_resume,
	}, {
		PHY_ID_MATCH_EXACT(PHY_ID_YT8522),
		.name           = "YT8522 100M Ethernet",
		.features       = PHY_BASIC_FEATURES,
		.probe          = yt8522_probe,
		.soft_reset     = ytphy_soft_reset,
		.config_aneg    = genphy_config_aneg,
		.config_init    = yt8522_config_init,
		.read_status    = yt8522_read_status,
		.suspend        = genphy_suspend,
		.resume         = genphy_resume,
	},  {
		/* same as 8521 */
		PHY_ID_MATCH_EXACT(PHY_ID_YT8531S),
		.name          = "YT8531S Gigabit Ethernet",
		.features      = PHY_GBIT_FEATURES,
		.soft_reset    = yt8521_soft_reset,
		.aneg_done     = yt8521_aneg_done,
		.config_init   = yt8531S_config_init,
		.read_status   = yt8521_read_status,
		.suspend       = yt8521_suspend,
		.resume        = yt8521_resume,
#if (YTPHY_WOL_FEATURE_ENABLE)
		.get_wol       = &ytphy_wol_feature_get,
		.set_wol       = &ytphy_wol_feature_set,
#endif
	}, {
		/* same as 8511 */
		PHY_ID_MATCH_EXACT(PHY_ID_YT8531),
		.name          = "YT8531 Gigabit Ethernet",
		.features      = PHY_GBIT_FEATURES,
		.config_init   = yt8531_config_init,
		.suspend       = genphy_suspend,
		.resume        = genphy_resume,
#if (YTPHY_WOL_FEATURE_ENABLE)
		.get_wol       = &ytphy_wol_feature_get,
		.set_wol       = &ytphy_wol_feature_set,
#endif
	},
};

module_phy_driver(motorcomm_phy_drvs);

MODULE_DESCRIPTION("Motorcomm PHY driver");
MODULE_AUTHOR("Peter Geis");
MODULE_LICENSE("GPL");

static const struct mdio_device_id __maybe_unused motorcomm_tbl[] = {
	{ PHY_ID_MATCH_EXACT(PHY_ID_YT8511) },
	{ PHY_ID_MATCH_EXACT(PHY_ID_YT8512) },
	{ PHY_ID_MATCH_EXACT(PHY_ID_YT8512B) },
	{ PHY_ID_MATCH_EXACT(PHY_ID_YT8522) },
	{ PHY_ID_MATCH_EXACT(PHY_ID_YT8531S) },
	{ PHY_ID_MATCH_EXACT(PHY_ID_YT8531) },
	{ /* sentinal */ }
};

MODULE_DEVICE_TABLE(mdio, motorcomm_tbl);
