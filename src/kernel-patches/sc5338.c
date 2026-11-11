// SPDX-License-Identifier: GPL-2.0
/*
 * sc5338 sensor driver (Rockchip V4L2 subdev port)
 *
 * Copyright (C) 2020 Rockchip Electronics Co., Ltd.
 * Copyright (C) 2026 OpenIPC / cmcc-s4602-rv1109 porting effort
 *
 * Ported for the China Mobile S4602 (Rockchip RV1109) camera from the
 * OpenIPC/linux Rockchip V4L2 skeleton (sc500ai.c) and the SC5338 register
 * data in sc5338-regs-rockchip.h. SC5338 (SmartSens, chip id 0xce50) specific
 * register tables and the analogue-gain LUT are derived from the upstream
 * OpenIPC/openingenic SC5338 driver (GPL-2.0) and the S4602 vendor kernel
 * (vendor-mode3 table matches the device's HDR_X2 stream at 0.9551 similarity).
 *
 * V0.0X01.0X00 first version.
 */

#include <linux/clk.h>
#include <linux/device.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/pm_runtime.h>
#include <linux/regulator/consumer.h>
#include <linux/sysfs.h>
#include <linux/slab.h>
#include <linux/version.h>
#include <linux/rk-camera-module.h>
#include <linux/rk-preisp.h>
#include <media/media-entity.h>
#include <media/v4l2-async.h>
#include <media/v4l2-ctrls.h>
#include <media/v4l2-subdev.h>
#include <linux/pinctrl/consumer.h>

#define DRIVER_VERSION			KERNEL_VERSION(0, 0x01, 0x00)

#ifndef V4L2_CID_DIGITAL_GAIN
#define V4L2_CID_DIGITAL_GAIN		V4L2_CID_GAIN
#endif

#define SC5338_LANES			2

/* 2-lane MIPI: ~720 Mbps/lane gives ~140 Mpx/s (2880x1620@30 RAW10). */
#define SC5338_LINK_FREQ_360M		360000000
#define SC5338_LINK_FREQ_720M		720000000

#define SC5338_MAX_PIXEL_RATE		(SC5338_LINK_FREQ_720M / 10 * 2 * SC5338_LANES)

#define SC5338_XVCLK_FREQ		24000000

/* Chip id is split across two bytes on SC5338 (like sc5239). */
#define SC5338_CHIP_ID 		0xce50
#define SC5338_REG_CHIP_ID_H		0x3107
#define SC5338_REG_CHIP_ID_L		0x3108

#define SC5338_REG_CTRL_MODE		0x0100
#define SC5338_MODE_SW_STANDBY		0x0
#define SC5338_MODE_STREAMING		BIT(0)

/* Long-frame exposure (16-bit packed into 3 registers, no doubling). */
#define SC5338_REG_EXPOSURE_H		0x3e00
#define SC5338_REG_EXPOSURE_M		0x3e01
#define SC5338_REG_EXPOSURE_L		0x3e02

#define	SC5338_EXPOSURE_MIN		4
#define	SC5338_EXPOSURE_STEP		1

/* Long-frame analogue gain: 16-bit value across 0x3e07 (low) / 0x3e09 (high). */
#define SC5338_REG_ANA_GAIN		0x3e07
#define SC5338_REG_ANA_GAIN_H		0x3e09

#define SC5338_GAIN_MIN		0
#define SC5338_GAIN_MAX		327680
#define SC5338_GAIN_STEP		1
#define SC5338_GAIN_DEFAULT		0

/* VTS (vertical total size) for vblank control. */
#define SC5338_REG_VTS_H		0x320e
#define SC5338_REG_VTS_L		0x320f
#define SC5338_VTS_MAX			0x7fff

#define SC5338_SOFTWARE_RESET_REG	0x0103

/* Short-frame (HDR) exposure: 0x3e04 (M) / 0x3e05 (L). */
#define SC5338_REG_SHORT_EXPOSURE_M	0x3e04
#define SC5338_REG_SHORT_EXPOSURE_L	0x3e05

/* Short-frame (HDR) analogue gain: 0x3e11 (low) / 0x3e13 (high). */
#define SC5338_REG_SANA_GAIN		0x3e11
#define SC5338_REG_SANA_GAIN_H		0x3e13

#define SC5338_FLIP_MIRROR_REG		0x3221
#define SC5338_FLIP_MASK		0x60
#define SC5338_MIRROR_MASK		0x06

/* Terminator for the regval arrays (matches sc5338-regs-rockchip.h REG_NULL). */
#define REG_NULL			0x0000

#define SC5338_REG_VALUE_08BIT		1
#define SC5338_REG_VALUE_16BIT		2
#define SC5338_REG_VALUE_24BIT		3

#define OF_CAMERA_PINCTRL_STATE_DEFAULT	"rockchip,camera_default"
#define OF_CAMERA_PINCTRL_STATE_SLEEP	"rockchip,camera_sleep"
#define OF_CAMERA_HDR_MODE		"rockchip,camera-hdr-mode"

#define SC5338_NAME			"sc5338"

/* Exposure packing (long frame): identical to the Ingenic SC5338 driver. */
#define SC5338_FETCH_EXP_H(VAL)		(((VAL) >> 12) & 0xF)
#define SC5338_FETCH_EXP_M(VAL)		(((VAL) >> 4) & 0xFF)
#define SC5338_FETCH_EXP_L(VAL)		(((VAL) & 0xF) << 4)
/* Short-frame exposure packing. */
#define SC5338_FETCH_SHORT_EXP_M(VAL)	(((VAL) >> 4) & 0xFF)
#define SC5338_FETCH_SHORT_EXP_L(VAL)	(((VAL) & 0xF) << 4)

static const char * const sc5338_supply_names[] = {
	"avdd",		/* Analog power */
	"dovdd",	/* Digital I/O power */
	"dvdd",		/* Digital core power */
};

#define sc5338_NUM_SUPPLIES ARRAY_SIZE(sc5338_supply_names)

enum sc5338_max_pad {
	PAD0, /* link to isp */
	PAD1, /* link to csi wr0 | hdr x2:L x3:M */
	PAD2, /* link to csi wr1 | hdr      x3:L */
	PAD3, /* link to csi wr2 | hdr x2:M x3:S */
	PAD_MAX,
};

struct regval {
	u16 addr;
	u8 val;
};

struct sc5338_mode {
	u32 bus_fmt;
	u32 width;
	u32 height;
	struct v4l2_fract max_fps;
	u32 hts_def;
	u32 vts_def;
	u32 exp_def;
	u32 mipi_freq_idx;
	u32 bpp;
	const struct regval *reg_list;
	u32 hdr_mode;
	u32 vc[PAD_MAX];
};

struct sc5338 {
	struct i2c_client	*client;
	struct clk		*xvclk;
	struct gpio_desc	*reset_gpio;
	struct gpio_desc	*pwdn_gpio;
	struct regulator_bulk_data supplies[sc5338_NUM_SUPPLIES];

	struct pinctrl		*pinctrl;
	struct pinctrl_state	*pins_default;
	struct pinctrl_state	*pins_sleep;

	struct v4l2_subdev	subdev;
	struct media_pad	pad;
	struct v4l2_ctrl_handler ctrl_handler;
	struct v4l2_ctrl	*exposure;
	struct v4l2_ctrl	*anal_gain;
	struct v4l2_ctrl	*digi_gain;
	struct v4l2_ctrl	*hblank;
	struct v4l2_ctrl	*vblank;
	struct v4l2_ctrl	*pixel_rate;
	struct v4l2_ctrl	*link_freq;
	struct mutex		mutex;
	bool			streaming;
	bool			power_on;
	const struct sc5338_mode *cur_mode;
	u32			module_index;
	const char		*module_facing;
	const char		*module_name;
	const char		*len_name;
	bool			has_init_exp;
	struct preisp_hdrae_exp_s init_hdrae_exp;
};

#define to_sc5338(sd) container_of(sd, struct sc5338, subdev)

/*
 * Register tables live in sc5338-tables.h (build header, generated from
 * sc5338-regs-rockchip.h reference):
 *   sc5338_vendor_mode3_regs           -> HDR_X2 (device-exact, S4602 dmesg hdr:5)
 *   sc5338_public_linear_2880x1620_30fps_2lane_regs -> linear 2880x1620 30fps
 */
#include "sc5338-tables.h"

static const struct sc5338_mode supported_modes[] = {
	{
		.width = 2880,
		.height = 1620,
		.max_fps = {
			.numerator = 10000,
			.denominator = 300000,
		},
		.exp_def = 0x700,
		.hts_def = 0x640,
		.vts_def = 0x708,
		.bus_fmt = MEDIA_BUS_FMT_SBGGR10_1X10,
		.reg_list = sc5338_public_linear_2880x1620_30fps_2lane_regs,
		.mipi_freq_idx = 0,
		.bpp = 10,
		.hdr_mode = NO_HDR,
		.vc[PAD0] = V4L2_MBUS_CSI2_CHANNEL_0,
	},
	{
		.width = 2880,
		.height = 1620,
		.max_fps = {
			.numerator = 10000,
			.denominator = 150000,
		},
		.exp_def = 0xc80,
		.hts_def = 0x640,
		.vts_def = 0x0e10,
		.bus_fmt = MEDIA_BUS_FMT_SBGGR10_1X10,
		.reg_list = sc5338_vendor_mode3_regs,
		.mipi_freq_idx = 1,
		.bpp = 10,
		.hdr_mode = HDR_X2,
		.vc[PAD0] = V4L2_MBUS_CSI2_CHANNEL_1,
		.vc[PAD1] = V4L2_MBUS_CSI2_CHANNEL_0,//L->csi wr0
		.vc[PAD2] = V4L2_MBUS_CSI2_CHANNEL_1,
		.vc[PAD3] = V4L2_MBUS_CSI2_CHANNEL_1,//M->csi wr2
	},
};

static const s64 link_freq_items[] = {
	SC5338_LINK_FREQ_360M,
	SC5338_LINK_FREQ_720M
};

/* Analogue-gain LUT (value, isp_gain) — from the upstream OpenIPC SC5338 driver. */
struct again_lut {
	unsigned int value;
	unsigned int gain;
};

static const struct again_lut sc5338_again_lut[] = {
	{0x80,0},{0x84,2886},{0x88,5776},{0x8c,8494},{0x90,11136},
	{0x94,13706},{0x98,16287},{0x9c,18723},{0xa0,21097},{0xa4,23414},
	{0xa8,25746},{0xac,27953},{0xb0,30109},{0xb4,32217},{0xb8,34345},
	{0xbc,36361},{0xc0,38336},{0xc4,40270},{0xc8,42226},{0xcc,44082},
	{0xd0,45904},{0xd4,47690},{0xd8,49500},{0xdc,51220},{0xe0,52910},
	{0xe4,54571},{0xe8,56254},{0xec,57857},{0xf0,59433},{0xf4,60984},
	{0xf8,62558},{0xfc,64059},{0x880,65536},{0x884,68422},{0x888,71312},
	{0x88c,74030},{0x890,76672},{0x894,79242},{0x898,81823},{0x89c,84259},
	{0x8a0,86633},{0x8a4,88950},{0x8a8,91282},{0x8ac,93489},{0x8b0,95645},
	{0x8b4,97753},{0x8b8,99881},{0x8bc,101897},{0x8c0,103872},{0x8c4,105806},
	{0x8c8,107762},{0x8cc,109618},{0x8d0,111440},{0x8d4,113226},{0x8d8,115036},
	{0x8dc,116756},{0x8e0,118446},{0x8e4,120107},{0x8e8,121790},{0x8ec,123393},
	{0x8f0,124969},{0x8f4,126520},{0x8f8,128094},{0x8fc,129595},{0x980,131072},
	{0x984,133958},{0x988,136848},{0x98c,139566},{0x990,142208},{0x994,144778},
	{0x998,147359},{0x99c,149795},{0x9a0,152169},{0x9a4,154486},{0x9a8,156818},
	{0x9ac,159025},{0x9b0,161181},{0x9b4,163289},{0x9b8,165417},{0x9bc,167433},
	{0x9c0,169408},{0x9c4,171342},{0x9c8,173298},{0x9cc,175154},{0x9d0,176976},
	{0x9d4,178762},{0x9d8,180572},{0x9dc,182292},{0x9e0,183982},{0x9e4,185643},
	{0x9e8,187326},{0x9ec,188929},{0x9f0,190505},{0x9f4,192056},{0x9f8,193630},
	{0x9fc,195131},{0xb80,196608},{0xb84,199494},{0xb88,202384},{0xb8c,205102},
	{0xb90,207744},{0xb94,210314},{0xb98,212895},{0xb9c,215331},{0xba0,217705},
	{0xba4,220022},{0xba8,222354},{0xbac,224561},{0xbb0,226717},{0xbb4,228825},
	{0xbb8,230953},{0xbbc,232969},{0xbc0,234944},{0xbc4,236878},{0xbc8,238834},
	{0xbcc,240690},{0xbd0,242512},{0xbd4,244298},{0xbd8,246108},{0xbdc,247828},
	{0xbe0,249518},{0xbe4,251179},{0xbe8,252862},{0xbec,254465},{0xbf0,256041},
	{0xbf4,257592},{0xbf8,259166},{0xbfc,260667},{0xf80,262144},{0xf84,265030},
	{0xf88,267920},{0xf8c,270638},{0xf90,273280},{0xf94,275850},{0xf98,278431},
	{0xf9c,280867},{0xfa0,283241},{0xfa4,285558},{0xfa8,287890},{0xfac,290097},
	{0xfb0,292253},{0xfb4,294361},{0xfb8,296489},{0xfbc,298505},{0xfc0,300480},
	{0xfc4,302414},{0xfc8,304370},{0xfcc,306226},{0xfd0,308048},{0xfd4,309834},
	{0xfd8,311644},{0xfdc,313364},{0xfe0,315054},{0xfe4,316715},{0xfe8,318398},
	{0xfec,320001},{0xff0,321577},{0xff4,323128},{0xff8,324702},{0xffc,326203},
	{0x1f80,327680},
};

/* Map an ISP gain value to the 16-bit sensor register value. */
static u32 sc5338_calc_again(u32 total_gain)
{
	int i;

	for (i = 0; i < ARRAY_SIZE(sc5338_again_lut); i++) {
		if (total_gain <= sc5338_again_lut[i].gain)
			return sc5338_again_lut[i].value;
	}
	return sc5338_again_lut[ARRAY_SIZE(sc5338_again_lut) - 1].value;
}

/* Write registers up to 4 at a time */
static int sc5338_write_reg(struct i2c_client *client, u16 reg,
                            u32 len, u32 val)
{
	u32 buf_i, val_i;
	u8 buf[6];
	u8 *val_p;
	__be32 val_be;

	if (len > 4)
		return -EINVAL;

	buf[0] = reg >> 8;
	buf[1] = reg & 0xff;

	val_be = cpu_to_be32(val);
	val_p = (u8 *)&val_be;
	buf_i = 2;
	val_i = 4 - len;

	while (val_i < 4)
		buf[buf_i++] = val_p[val_i++];

	if (i2c_master_send(client, buf, len + 2) != len + 2)
		return -EIO;

	return 0;
}

static int sc5338_write_array(struct i2c_client *client,
                              const struct regval *regs)
{
	u32 i;
	int ret = 0;

	for (i = 0; ret == 0 && regs[i].addr != REG_NULL; i++)
		ret = sc5338_write_reg(client, regs[i].addr,
		                      SC5338_REG_VALUE_08BIT, regs[i].val);

	return ret;
}

/* Read registers up to 4 at a time */
static int sc5338_read_reg(struct i2c_client *client, u16 reg, unsigned int len,
                           u32 *val)
{
	struct i2c_msg msgs[2];
	u8 *data_be_p;
	__be32 data_be = 0;
	__be16 reg_addr_be = cpu_to_be16(reg);
	int ret;

	if (len > 4 || !len)
		return -EINVAL;

	data_be_p = (u8 *)&data_be;
	/* Write register address */
	msgs[0].addr = client->addr;
	msgs[0].flags = 0;
	msgs[0].len = 2;
	msgs[0].buf = (u8 *)&reg_addr_be;

	/* Read data from register */
	msgs[1].addr = client->addr;
	msgs[1].flags = I2C_M_RD;
	msgs[1].len = len;
	msgs[1].buf = &data_be_p[4 - len];

	ret = i2c_transfer(client->adapter, msgs, ARRAY_SIZE(msgs));
	if (ret != ARRAY_SIZE(msgs))
		return -EIO;

	*val = be32_to_cpu(data_be);

	return 0;
}

static int sc5338_get_reso_dist(const struct sc5338_mode *mode,
                                struct v4l2_mbus_framefmt *framefmt)
{
	return abs(mode->width - framefmt->width) +
	       abs(mode->height - framefmt->height);
}

static const struct sc5338_mode *
sc5338_find_best_fit(struct v4l2_subdev_format *fmt)
{
	struct v4l2_mbus_framefmt *framefmt = &fmt->format;
	int dist;
	int cur_best_fit = 0;
	int cur_best_fit_dist = -1;
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(supported_modes); i++) {
		dist = sc5338_get_reso_dist(&supported_modes[i], framefmt);
		if (cur_best_fit_dist == -1 || dist < cur_best_fit_dist) {
			cur_best_fit_dist = dist;
			cur_best_fit = i;
		}
	}

	return &supported_modes[cur_best_fit];
}

static int sc5338_set_fmt(struct v4l2_subdev *sd,
                          struct v4l2_subdev_pad_config *cfg,
                          struct v4l2_subdev_format *fmt)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	const struct sc5338_mode *mode;
	s64 h_blank, vblank_def;
	u64 pixel_rate = 0;

	mutex_lock(&sc5338->mutex);

	mode = sc5338_find_best_fit(fmt);
	fmt->format.code = mode->bus_fmt;
	fmt->format.width = mode->width;
	fmt->format.height = mode->height;
	fmt->format.field = V4L2_FIELD_NONE;
	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
		*v4l2_subdev_get_try_format(sd, cfg, fmt->pad) = fmt->format;
#else
		mutex_unlock(&sc5338->mutex);
		return -ENOTTY;
#endif
	} else {
		sc5338->cur_mode = mode;

		h_blank = mode->hts_def - mode->width;
		__v4l2_ctrl_modify_range(sc5338->hblank, h_blank,
		                         h_blank, 1, h_blank);
		vblank_def = mode->vts_def - mode->height;
		__v4l2_ctrl_modify_range(sc5338->vblank, vblank_def,
		                         SC5338_VTS_MAX - mode->height,
		                         1, vblank_def);

		__v4l2_ctrl_s_ctrl(sc5338->link_freq, mode->mipi_freq_idx);
		pixel_rate = (u32)link_freq_items[mode->mipi_freq_idx] / mode->bpp * 2 * SC5338_LANES;
		__v4l2_ctrl_s_ctrl_int64(sc5338->pixel_rate, pixel_rate);
	}

	mutex_unlock(&sc5338->mutex);

	return 0;
}

static int sc5338_get_fmt(struct v4l2_subdev *sd,
                          struct v4l2_subdev_pad_config *cfg,
                          struct v4l2_subdev_format *fmt)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	const struct sc5338_mode *mode = sc5338->cur_mode;

	mutex_lock(&sc5338->mutex);
	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
		fmt->format = *v4l2_subdev_get_try_format(sd, cfg, fmt->pad);
#else
		mutex_unlock(&sc5338->mutex);
		return -ENOTTY;
#endif
	} else {
		fmt->format.width = mode->width;
		fmt->format.height = mode->height;
		fmt->format.code = mode->bus_fmt;
		fmt->format.field = V4L2_FIELD_NONE;
		/* format info: width/height/data type/virctual channel */
		if (fmt->pad < PAD_MAX && mode->hdr_mode != NO_HDR)
			fmt->reserved[0] = mode->vc[fmt->pad];
		else
			fmt->reserved[0] = mode->vc[PAD0];
	}
	mutex_unlock(&sc5338->mutex);

	return 0;
}

static int sc5338_enum_mbus_code(struct v4l2_subdev *sd,
                                 struct v4l2_subdev_pad_config *cfg,
                                 struct v4l2_subdev_mbus_code_enum *code)
{
	struct sc5338 *sc5338 = to_sc5338(sd);

	if (code->index != 0)
		return -EINVAL;
	code->code = sc5338->cur_mode->bus_fmt;

	return 0;
}

static int sc5338_enum_frame_sizes(struct v4l2_subdev *sd,
                                   struct v4l2_subdev_pad_config *cfg,
                                   struct v4l2_subdev_frame_size_enum *fse)
{
	if (fse->index >= ARRAY_SIZE(supported_modes))
		return -EINVAL;

	if (fse->code != supported_modes[0].bus_fmt)
		return -EINVAL;

	fse->min_width  = supported_modes[fse->index].width;
	fse->max_width  = supported_modes[fse->index].width;
	fse->max_height = supported_modes[fse->index].height;
	fse->min_height = supported_modes[fse->index].height;

	return 0;
}

static int sc5338_g_frame_interval(struct v4l2_subdev *sd,
                                   struct v4l2_subdev_frame_interval *fi)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	const struct sc5338_mode *mode = sc5338->cur_mode;

	mutex_lock(&sc5338->mutex);
	fi->interval = mode->max_fps;
	mutex_unlock(&sc5338->mutex);

	return 0;
}

static int sc5338_g_mbus_config(struct v4l2_subdev *sd,
                                struct v4l2_mbus_config *config)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	const struct sc5338_mode *mode = sc5338->cur_mode;
	u32 val = 1 << (SC5338_LANES - 1) |
	          V4L2_MBUS_CSI2_CHANNEL_0 |
	          V4L2_MBUS_CSI2_CONTINUOUS_CLOCK;

	if (mode->hdr_mode != NO_HDR)
		val |= V4L2_MBUS_CSI2_CHANNEL_1;
	if (mode->hdr_mode == HDR_X3)
		val |= V4L2_MBUS_CSI2_CHANNEL_2;

	config->type = V4L2_MBUS_CSI2;
	config->flags = val;

	return 0;
}

static void sc5338_get_module_inf(struct sc5338 *sc5338,
                                  struct rkmodule_inf *inf)
{
	memset(inf, 0, sizeof(*inf));
	strlcpy(inf->base.sensor, SC5338_NAME, sizeof(inf->base.sensor));
	strlcpy(inf->base.module, sc5338->module_name,
	        sizeof(inf->base.module));
	strlcpy(inf->base.lens, sc5338->len_name, sizeof(inf->base.lens));
}

static int sc5338_set_hdrae(struct sc5338 *sc5338,
			     struct preisp_hdrae_exp_s *ae)
{
	int ret = 0;
	u32 l_exp_time, s_exp_time;
	u32 l_t_gain, s_t_gain;
	u32 l_again = 0, s_again = 0;

	if (!sc5338->has_init_exp && !sc5338->streaming) {
		sc5338->init_hdrae_exp = *ae;
		sc5338->has_init_exp = true;
		dev_dbg(&sc5338->client->dev, "sc5338 don't stream, record exp for hdr!\n");
		return ret;
	}
	l_exp_time = ae->long_exp_reg;
	s_exp_time = ae->short_exp_reg;
	l_t_gain = ae->long_gain_reg;
	s_t_gain = ae->short_gain_reg;

	dev_dbg(&sc5338->client->dev,
		"rev exp req: L_exp: 0x%x, S_exp: 0x%x, L_tgain: 0x%x, S_tgain: 0x%x\n",
		l_exp_time, s_exp_time,
		l_t_gain, s_t_gain);

	// set long-frame exposure
	ret |= sc5338_write_reg(sc5338->client,
				 SC5338_REG_EXPOSURE_H,
				 SC5338_REG_VALUE_08BIT,
				 SC5338_FETCH_EXP_H(l_exp_time));
	ret |= sc5338_write_reg(sc5338->client,
	                         SC5338_REG_EXPOSURE_M,
	                         SC5338_REG_VALUE_08BIT,
	                         SC5338_FETCH_EXP_M(l_exp_time));
	ret |= sc5338_write_reg(sc5338->client,
	                         SC5338_REG_EXPOSURE_L,
	                         SC5338_REG_VALUE_08BIT,
	                         SC5338_FETCH_EXP_L(l_exp_time));
	// set short-frame exposure
	ret |= sc5338_write_reg(sc5338->client,
	                         SC5338_REG_SHORT_EXPOSURE_M,
	                         SC5338_REG_VALUE_08BIT,
	                         SC5338_FETCH_SHORT_EXP_M(s_exp_time));
	ret |= sc5338_write_reg(sc5338->client,
	                         SC5338_REG_SHORT_EXPOSURE_L,
	                         SC5338_REG_VALUE_08BIT,
	                         SC5338_FETCH_SHORT_EXP_L(s_exp_time));

	// set long-frame analogue gain (16-bit: 0x3e07 low / 0x3e09 high)
	l_again = sc5338_calc_again(l_t_gain);
	ret |= sc5338_write_reg(sc5338->client,
				 SC5338_REG_ANA_GAIN,
				 SC5338_REG_VALUE_08BIT,
				 l_again & 0xff);
	ret |= sc5338_write_reg(sc5338->client,
	                         SC5338_REG_ANA_GAIN_H,
	                         SC5338_REG_VALUE_08BIT,
	                         (l_again >> 8) & 0xff);
	// set short-frame analogue gain (0x3e11 low / 0x3e13 high)
	s_again = sc5338_calc_again(s_t_gain);
	ret |= sc5338_write_reg(sc5338->client,
				 SC5338_REG_SANA_GAIN,
				 SC5338_REG_VALUE_08BIT,
				 s_again & 0xff);
	ret |= sc5338_write_reg(sc5338->client,
	                         SC5338_REG_SANA_GAIN_H,
	                         SC5338_REG_VALUE_08BIT,
	                         (s_again >> 8) & 0xff);

	return ret;
}

static long sc5338_ioctl(struct v4l2_subdev *sd, unsigned int cmd, void *arg)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	struct rkmodule_hdr_cfg *hdr;
	const struct sc5338_mode *mode;

	long ret = 0;
	u32 i, h, w;
	u32 stream = 0;
	u64 pixel_rate = 0;

	switch (cmd) {
	case RKMODULE_GET_MODULE_INFO:
		sc5338_get_module_inf(sc5338, (struct rkmodule_inf *)arg);
		break;
	case RKMODULE_GET_HDR_CFG:
		hdr = (struct rkmodule_hdr_cfg *)arg;
		hdr->esp.mode = HDR_NORMAL_VC;
		hdr->hdr_mode = sc5338->cur_mode->hdr_mode;
		break;
	case RKMODULE_SET_HDR_CFG:
		hdr = (struct rkmodule_hdr_cfg *)arg;
		w = sc5338->cur_mode->width;
		h = sc5338->cur_mode->height;
		for (i = 0; i < ARRAY_SIZE(supported_modes); i++) {
			if (w == supported_modes[i].width &&
			    h == supported_modes[i].height &&
			    supported_modes[i].hdr_mode == hdr->hdr_mode) {
				sc5338->cur_mode = &supported_modes[i];
				break;
			}
		}
		if (i == ARRAY_SIZE(supported_modes)) {
			dev_err(&sc5338->client->dev,
				"not find hdr mode:%d %dx%d config\n",
				hdr->hdr_mode, w, h);
			ret = -EINVAL;
		} else {
			mode = sc5338->cur_mode;
			w = sc5338->cur_mode->hts_def - sc5338->cur_mode->width;
			h = sc5338->cur_mode->vts_def - sc5338->cur_mode->height;
			__v4l2_ctrl_modify_range(sc5338->hblank, w, w, 1, w);
			__v4l2_ctrl_modify_range(sc5338->vblank, h,
						 SC5338_VTS_MAX - sc5338->cur_mode->height, 1, h);

			__v4l2_ctrl_s_ctrl(sc5338->link_freq, mode->mipi_freq_idx);
			pixel_rate = (u32)link_freq_items[mode->mipi_freq_idx] / mode->bpp * 2 * SC5338_LANES;
			__v4l2_ctrl_s_ctrl_int64(sc5338->pixel_rate, pixel_rate);

			dev_info(&sc5338->client->dev, "sensor mode: %d\n", sc5338->cur_mode->hdr_mode);
		}
		break;
	case PREISP_CMD_SET_HDRAE_EXP:
		if (sc5338->cur_mode->hdr_mode == HDR_X2)
			ret = sc5338_set_hdrae(sc5338, arg);
		break;
	case RKMODULE_SET_QUICK_STREAM:
		stream = *((u32 *)arg);
		if (stream)
			ret = sc5338_write_reg(sc5338->client, SC5338_REG_CTRL_MODE,
			                        SC5338_REG_VALUE_08BIT, SC5338_MODE_STREAMING);
		else
			ret = sc5338_write_reg(sc5338->client, SC5338_REG_CTRL_MODE,
			                        SC5338_REG_VALUE_08BIT, SC5338_MODE_SW_STANDBY);
		break;
	default:
		ret = -ENOIOCTLCMD;
		break;
	}

	return ret;
}

#ifdef CONFIG_COMPAT
static long sc5338_compat_ioctl32(struct v4l2_subdev *sd,
                                  unsigned int cmd, unsigned long arg)
{
	void __user *up = compat_ptr(arg);
	struct rkmodule_inf *inf;
	struct rkmodule_awb_cfg *cfg;
	struct rkmodule_hdr_cfg *hdr;
	struct preisp_hdrae_exp_s *hdrae;
	long ret = 0;
	u32 stream = 0;

	switch (cmd) {
	case RKMODULE_GET_MODULE_INFO:
		inf = kzalloc(sizeof(*inf), GFP_KERNEL);
		if (!inf) {
			ret = -ENOMEM;
			return ret;
		}

		ret = sc5338_ioctl(sd, cmd, inf);
		if (!ret)
			ret = copy_to_user(up, inf, sizeof(*inf));
		kfree(inf);
		break;
	case RKMODULE_AWB_CFG:
		cfg = kzalloc(sizeof(*cfg), GFP_KERNEL);
		if (!cfg) {
			ret = -ENOMEM;
			return ret;
		}

		ret = copy_from_user(cfg, up, sizeof(*cfg));
		if (!ret)
			ret = sc5338_ioctl(sd, cmd, cfg);
		kfree(cfg);
		break;
	case RKMODULE_GET_HDR_CFG:
		hdr = kzalloc(sizeof(*hdr), GFP_KERNEL);
		if (!hdr) {
			ret = -ENOMEM;
			return ret;
		}

		ret = sc5338_ioctl(sd, cmd, hdr);
		if (!ret)
			ret = copy_to_user(up, hdr, sizeof(*hdr));
		kfree(hdr);
		break;
	case RKMODULE_SET_HDR_CFG:
		hdr = kzalloc(sizeof(*hdr), GFP_KERNEL);
		if (!hdr) {
			ret = -ENOMEM;
			return ret;
		}

		ret = copy_from_user(hdr, up, sizeof(*hdr));
		if (!ret)
			ret = sc5338_ioctl(sd, cmd, hdr);
		kfree(hdr);
		break;
	case PREISP_CMD_SET_HDRAE_EXP:
		hdrae = kzalloc(sizeof(*hdrae), GFP_KERNEL);
		if (!hdrae) {
			ret = -ENOMEM;
			return ret;
		}

		ret = copy_from_user(hdrae, up, sizeof(*hdrae));
		if (!ret)
			ret = sc5338_ioctl(sd, cmd, hdrae);
		kfree(hdrae);
		break;
	case RKMODULE_SET_QUICK_STREAM:
		ret = copy_from_user(&stream, up, sizeof(u32));
		if (!ret)
			ret = sc5338_ioctl(sd, cmd, &stream);
		break;
	default:
		ret = -ENOIOCTLCMD;
		break;
	}

	return ret;
}
#endif

static int __sc5338_start_stream(struct sc5338 *sc5338)
{
	int ret;

	ret = sc5338_write_array(sc5338->client, sc5338->cur_mode->reg_list);
	if (ret)
		return ret;

	/* In case these controls are set before streaming */
	ret = __v4l2_ctrl_handler_setup(&sc5338->ctrl_handler);
	if (ret)
		return ret;
	if (sc5338->has_init_exp && sc5338->cur_mode->hdr_mode != NO_HDR) {
		ret = sc5338_ioctl(&sc5338->subdev, PREISP_CMD_SET_HDRAE_EXP,
		                    &sc5338->init_hdrae_exp);
		if (ret) {
			dev_err(&sc5338->client->dev,
			        "init exp fail in hdr mode\n");
			return ret;
		}
	}

	return sc5338_write_reg(sc5338->client, SC5338_REG_CTRL_MODE,
	                         SC5338_REG_VALUE_08BIT, SC5338_MODE_STREAMING);
}

static int __sc5338_stop_stream(struct sc5338 *sc5338)
{
	sc5338->has_init_exp = false;
	return sc5338_write_reg(sc5338->client, SC5338_REG_CTRL_MODE,
	                         SC5338_REG_VALUE_08BIT, SC5338_MODE_SW_STANDBY);
}

static int sc5338_s_stream(struct v4l2_subdev *sd, int on)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	struct i2c_client *client = sc5338->client;
	int ret = 0;

	mutex_lock(&sc5338->mutex);
	on = !!on;
	if (on == sc5338->streaming)
		goto unlock_and_return;

	if (on) {
		ret = pm_runtime_get_sync(&client->dev);
		if (ret < 0) {
			pm_runtime_put_noidle(&client->dev);
			goto unlock_and_return;
		}

		ret = __sc5338_start_stream(sc5338);
		if (ret) {
			v4l2_err(sd, "start stream failed while write regs\n");
			pm_runtime_put(&client->dev);
			goto unlock_and_return;
		}
	} else {
		__sc5338_stop_stream(sc5338);
		pm_runtime_put(&client->dev);
	}

	sc5338->streaming = on;

unlock_and_return:
	mutex_unlock(&sc5338->mutex);

	return ret;
}

static int sc5338_s_power(struct v4l2_subdev *sd, int on)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	struct i2c_client *client = sc5338->client;
	int ret = 0;

	mutex_lock(&sc5338->mutex);

	/* If the power state is not modified - no work to do. */
	if (sc5338->power_on == !!on)
		goto unlock_and_return;

	if (on) {
		ret = pm_runtime_get_sync(&client->dev);
		if (ret < 0) {
			pm_runtime_put_noidle(&client->dev);
			goto unlock_and_return;
		}

		ret |= sc5338_write_reg(sc5338->client,
		                         SC5338_SOFTWARE_RESET_REG,
		                         SC5338_REG_VALUE_08BIT,
		                         0x01);
		usleep_range(100, 200);

		sc5338->power_on = true;
	} else {
		pm_runtime_put(&client->dev);
		sc5338->power_on = false;
	}

unlock_and_return:
	mutex_unlock(&sc5338->mutex);

	return ret;
}

static int __sc5338_power_on(struct sc5338 *sc5338)
{
	int ret;
	struct device *dev = &sc5338->client->dev;

	if (!IS_ERR_OR_NULL(sc5338->pins_default)) {
		ret = pinctrl_select_state(sc5338->pinctrl,
		                           sc5338->pins_default);
		if (ret < 0)
			dev_err(dev, "could not set pins\n");
	}
	ret = clk_set_rate(sc5338->xvclk, SC5338_XVCLK_FREQ);
	if (ret < 0)
		dev_warn(dev, "Failed to set xvclk rate (24MHz)\n");
	if (clk_get_rate(sc5338->xvclk) != SC5338_XVCLK_FREQ)
		dev_warn(dev, "xvclk mismatched, modes are based on 24MHz\n");
	ret = clk_prepare_enable(sc5338->xvclk);
	if (ret < 0) {
		dev_err(dev, "Failed to enable xvclk\n");
		return ret;
	}
	if (!IS_ERR(sc5338->reset_gpio))
		gpiod_set_value_cansleep(sc5338->reset_gpio, 0);

	ret = regulator_bulk_enable(sc5338_NUM_SUPPLIES, sc5338->supplies);
	if (ret < 0) {
		dev_err(dev, "Failed to enable regulators\n");
		goto disable_clk;
	}

	if (!IS_ERR(sc5338->reset_gpio))
		gpiod_set_value_cansleep(sc5338->reset_gpio, 1);

	usleep_range(500, 1000);
	if (!IS_ERR(sc5338->pwdn_gpio))
		gpiod_set_value_cansleep(sc5338->pwdn_gpio, 1);

	usleep_range(4000, 5000);
	return 0;

disable_clk:
	clk_disable_unprepare(sc5338->xvclk);

	return ret;
}

static void __sc5338_power_off(struct sc5338 *sc5338)
{
	int ret;
	struct device *dev = &sc5338->client->dev;

	if (!IS_ERR(sc5338->pwdn_gpio))
		gpiod_set_value_cansleep(sc5338->pwdn_gpio, 0);
	clk_disable_unprepare(sc5338->xvclk);
	if (!IS_ERR(sc5338->reset_gpio))
		gpiod_set_value_cansleep(sc5338->reset_gpio, 0);
	if (!IS_ERR_OR_NULL(sc5338->pins_sleep)) {
		ret = pinctrl_select_state(sc5338->pinctrl,
		                           sc5338->pins_sleep);
		if (ret < 0)
			dev_dbg(dev, "could not set pins\n");
	}
	regulator_bulk_disable(sc5338_NUM_SUPPLIES, sc5338->supplies);
}

static int sc5338_runtime_resume(struct device *dev)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct sc5338 *sc5338 = to_sc5338(sd);

	return __sc5338_power_on(sc5338);
}

static int sc5338_runtime_suspend(struct device *dev)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct sc5338 *sc5338 = to_sc5338(sd);

	__sc5338_power_off(sc5338);

	return 0;
}

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
static int sc5338_open(struct v4l2_subdev *sd, struct v4l2_subdev_fh *fh)
{
	struct sc5338 *sc5338 = to_sc5338(sd);
	struct v4l2_mbus_framefmt *try_fmt =
	        v4l2_subdev_get_try_format(sd, fh->pad, 0);
	const struct sc5338_mode *def_mode = &supported_modes[0];

	mutex_lock(&sc5338->mutex);
	/* Initialize try_fmt */
	try_fmt->width = def_mode->width;
	try_fmt->height = def_mode->height;
	try_fmt->code = def_mode->bus_fmt;
	try_fmt->field = V4L2_FIELD_NONE;

	mutex_unlock(&sc5338->mutex);
	/* No crop or compose */

	return 0;
}
#endif

#define DST_WIDTH 2880
#define DST_HEIGHT 1616

/*
 * The resolution of the driver configuration needs to be exactly
 * the same as the current output resolution of the sensor,
 * the input width of the isp needs to be 16 aligned,
 * the input height of the isp needs to be 8 aligned.
 * Can be cropped to standard resolution by this function,
 * otherwise it will crop out strange resolution according
 * to the alignment rules.
 */
static int sc5338_get_selection(struct v4l2_subdev *sd,
				struct v4l2_subdev_pad_config *cfg,
				struct v4l2_subdev_selection *sel)
{
	if (sel->target == V4L2_SEL_TGT_CROP_BOUNDS) {
		sel->r.left = 0;
		sel->r.width = DST_WIDTH;
		sel->r.top = 2;
		sel->r.height = DST_HEIGHT;
		return 0;
	}
	return -EINVAL;
}

static int sc5338_enum_frame_interval(struct v4l2_subdev *sd,
                                      struct v4l2_subdev_pad_config *cfg,
                                      struct v4l2_subdev_frame_interval_enum *fie)
{
	if (fie->index >= ARRAY_SIZE(supported_modes))
		return -EINVAL;

	fie->code = supported_modes[fie->index].bus_fmt;
	fie->width = supported_modes[fie->index].width;
	fie->height = supported_modes[fie->index].height;
	fie->interval = supported_modes[fie->index].max_fps;
	fie->reserved[0] = supported_modes[fie->index].hdr_mode;
	return 0;
}

static const struct dev_pm_ops sc5338_pm_ops = {
	SET_RUNTIME_PM_OPS(sc5338_runtime_suspend,
	sc5338_runtime_resume, NULL)
};

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
static const struct v4l2_subdev_internal_ops sc5338_internal_ops = {
	.open = sc5338_open,
};
#endif

static const struct v4l2_subdev_core_ops sc5338_core_ops = {
	.s_power = sc5338_s_power,
	.ioctl = sc5338_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl32 = sc5338_compat_ioctl32,
#endif
};

static const struct v4l2_subdev_video_ops sc5338_video_ops = {
	.s_stream = sc5338_s_stream,
	.g_frame_interval = sc5338_g_frame_interval,
	.g_mbus_config = sc5338_g_mbus_config,
};

static const struct v4l2_subdev_pad_ops sc5338_pad_ops = {
	.enum_mbus_code = sc5338_enum_mbus_code,
	.enum_frame_size = sc5338_enum_frame_sizes,
	.enum_frame_interval = sc5338_enum_frame_interval,
	.get_fmt = sc5338_get_fmt,
	.set_fmt = sc5338_set_fmt,
	.get_selection = sc5338_get_selection,
};

static const struct v4l2_subdev_ops sc5338_subdev_ops = {
	.core	= &sc5338_core_ops,
	.video	= &sc5338_video_ops,
	.pad	= &sc5338_pad_ops,
};

static int sc5338_set_ctrl(struct v4l2_ctrl *ctrl)
{
	struct sc5338 *sc5338 = container_of(ctrl->handler,
	                                       struct sc5338, ctrl_handler);
	struct i2c_client *client = sc5338->client;
	s64 max;
	u32 again = 0, again_h = 0;
	int ret = 0;
	u32 val = 0, vts = 0;

	/* Propagate change of current control to all related controls */
	switch (ctrl->id) {
	case V4L2_CID_VBLANK:
		/* Update max exposure while meeting expected vblanking */
		max = sc5338->cur_mode->height + ctrl->val - 5;
		__v4l2_ctrl_modify_range(sc5338->exposure,
		                         sc5338->exposure->minimum, max,
		                         sc5338->exposure->step,
		                         sc5338->exposure->default_value);
		break;
	}

	if (!pm_runtime_get_if_in_use(&client->dev))
		return 0;

	switch (ctrl->id) {
	case V4L2_CID_EXPOSURE:
		if (sc5338->cur_mode->hdr_mode != NO_HDR)
			return ret;
		val = ctrl->val;
		ret = sc5338_write_reg(sc5338->client,
		                        SC5338_REG_EXPOSURE_H,
		                        SC5338_REG_VALUE_08BIT,
		                        SC5338_FETCH_EXP_H(val));
		ret |= sc5338_write_reg(sc5338->client,
		                         SC5338_REG_EXPOSURE_M,
		                         SC5338_REG_VALUE_08BIT,
		                         SC5338_FETCH_EXP_M(val));
		ret |= sc5338_write_reg(sc5338->client,
		                         SC5338_REG_EXPOSURE_L,
		                         SC5338_REG_VALUE_08BIT,
		                         SC5338_FETCH_EXP_L(val));

		dev_dbg(&client->dev, "set exposure 0x%x\n", val);
		break;
	case V4L2_CID_ANALOGUE_GAIN:
		if (sc5338->cur_mode->hdr_mode != NO_HDR)
			return ret;

		again = sc5338_calc_again(ctrl->val);
		again_h = (again >> 8) & 0xff;
		ret = sc5338_write_reg(sc5338->client,
		                        SC5338_REG_ANA_GAIN,
		                        SC5338_REG_VALUE_08BIT,
		                        again & 0xff);
		ret |= sc5338_write_reg(sc5338->client,
		                         SC5338_REG_ANA_GAIN_H,
		                         SC5338_REG_VALUE_08BIT,
		                         again_h);
		dev_dbg(&sc5338->client->dev,
		        "total_gain:%d again 0x%x (0x%02x:0x%02x)\n",
		        ctrl->val, again, again & 0xff, again_h);
		break;
	case V4L2_CID_VBLANK:
		vts = ctrl->val + sc5338->cur_mode->height;
		ret = sc5338_write_reg(sc5338->client,
					SC5338_REG_VTS_H,
					SC5338_REG_VALUE_08BIT,
					(vts >> 8) & 0x7f);
		ret |= sc5338_write_reg(sc5338->client,
					 SC5338_REG_VTS_L,
					 SC5338_REG_VALUE_08BIT,
					 vts & 0xff);
		break;
	case V4L2_CID_HFLIP:
		ret = sc5338_read_reg(sc5338->client, SC5338_FLIP_MIRROR_REG,
		                       SC5338_REG_VALUE_08BIT, &val);
		if (ret)
			break;

		if (ctrl->val)
			val |= SC5338_MIRROR_MASK;
		else
			val &= ~SC5338_MIRROR_MASK;
		ret |= sc5338_write_reg(sc5338->client, SC5338_FLIP_MIRROR_REG,
		                         SC5338_REG_VALUE_08BIT, val);
		break;
	case V4L2_CID_VFLIP:
		ret = sc5338_read_reg(sc5338->client,
		                       SC5338_FLIP_MIRROR_REG,
		                       SC5338_REG_VALUE_08BIT, &val);
		if (ret)
			break;

		if (ctrl->val)
			val |= SC5338_FLIP_MASK;
		else
			val &= ~SC5338_FLIP_MASK;
		ret |= sc5338_write_reg(sc5338->client,
		                         SC5338_FLIP_MIRROR_REG,
		                         SC5338_REG_VALUE_08BIT,
		                         val);
		break;
	default:
		dev_warn(&client->dev, "%s Unhandled id:0x%x, val:0x%x\n",
		         __func__, ctrl->id, ctrl->val);
		break;
	}

	pm_runtime_put(&client->dev);

	return ret;
}

static const struct v4l2_ctrl_ops sc5338_ctrl_ops = {
	.s_ctrl = sc5338_set_ctrl,
};

static int sc5338_initialize_controls(struct sc5338 *sc5338)
{
	const struct sc5338_mode *mode;
	struct v4l2_ctrl_handler *handler;
	s64 exposure_max, vblank_def;
	u64 pixel_rate;
	u32 h_blank;
	int ret;

	handler = &sc5338->ctrl_handler;
	mode = sc5338->cur_mode;
	ret = v4l2_ctrl_handler_init(handler, 8);
	if (ret)
		return ret;
	handler->lock = &sc5338->mutex;

	sc5338->link_freq = v4l2_ctrl_new_int_menu(handler, NULL,
				V4L2_CID_LINK_FREQ,
				ARRAY_SIZE(link_freq_items) - 1, 0,
				link_freq_items);
	__v4l2_ctrl_s_ctrl(sc5338->link_freq, mode->mipi_freq_idx);

	/* pixel rate = link frequency * 2 * lanes / BITS_PER_SAMPLE */
	pixel_rate = (u32)link_freq_items[mode->mipi_freq_idx] / mode->bpp * 2 * SC5338_LANES ;
	sc5338->pixel_rate = v4l2_ctrl_new_std(handler, NULL,
			V4L2_CID_PIXEL_RATE, 0, SC5338_MAX_PIXEL_RATE,
			1, pixel_rate);

	h_blank = mode->hts_def - mode->width;
	sc5338->hblank = v4l2_ctrl_new_std(handler, NULL, V4L2_CID_HBLANK,
	                                    h_blank, h_blank, 1, h_blank);
	if (sc5338->hblank)
		sc5338->hblank->flags |= V4L2_CTRL_FLAG_READ_ONLY;

	vblank_def = mode->vts_def - mode->height;
	sc5338->vblank = v4l2_ctrl_new_std(handler, &sc5338_ctrl_ops,
	                                    V4L2_CID_VBLANK, vblank_def,
	                                    SC5338_VTS_MAX - mode->height,
	                                    1, vblank_def);
	exposure_max = mode->vts_def - 5;
	sc5338->exposure = v4l2_ctrl_new_std(handler, &sc5338_ctrl_ops,
	                                      V4L2_CID_EXPOSURE, SC5338_EXPOSURE_MIN,
	                                      exposure_max, SC5338_EXPOSURE_STEP,
	                                      mode->exp_def);

	sc5338->anal_gain = v4l2_ctrl_new_std(handler, &sc5338_ctrl_ops,
	                                       V4L2_CID_ANALOGUE_GAIN, SC5338_GAIN_MIN,
	                                       SC5338_GAIN_MAX, SC5338_GAIN_STEP,
	                                       SC5338_GAIN_DEFAULT);

	v4l2_ctrl_new_std(handler, &sc5338_ctrl_ops,
				V4L2_CID_HFLIP, 0, 1, 1, 0);

	v4l2_ctrl_new_std(handler, &sc5338_ctrl_ops,
				V4L2_CID_VFLIP, 0, 1, 1, 0);

	if (handler->error) {
		ret = handler->error;
		dev_err(&sc5338->client->dev,
		        "Failed to init controls(%d)\n", ret);
		goto err_free_handler;
	}
	sc5338->subdev.ctrl_handler = handler;
	sc5338->has_init_exp = false;

	return 0;

err_free_handler:
	v4l2_ctrl_handler_free(handler);
	return ret;
}

static int sc5338_check_sensor_id(struct sc5338 *sc5338,
                                   struct i2c_client *client)
{
	struct device *dev = &sc5338->client->dev;
	u32 id_h = 0, id_l = 0;
	u32 id;
	int ret;

	ret = sc5338_read_reg(client, SC5338_REG_CHIP_ID_H,
	                       SC5338_REG_VALUE_08BIT, &id_h);
	ret |= sc5338_read_reg(client, SC5338_REG_CHIP_ID_L,
	                        SC5338_REG_VALUE_08BIT, &id_l);
	id = (id_h << 8) | id_l;
	if (id != SC5338_CHIP_ID) {
		dev_err(dev, "Unexpected sensor id(%04x), ret(%d)\n", id, ret);
		return -ENODEV;
	}

	dev_info(dev, "Detected SC%04x sensor\n", SC5338_CHIP_ID);

	return 0;
}

static int sc5338_configure_regulators(struct sc5338 *sc5338)
{
	unsigned int i;

	for (i = 0; i < sc5338_NUM_SUPPLIES; i++)
		sc5338->supplies[i].supply = sc5338_supply_names[i];

	return devm_regulator_bulk_get(&sc5338->client->dev,
	                               sc5338_NUM_SUPPLIES,
	                               sc5338->supplies);
}

static int sc5338_probe(struct i2c_client *client,
                        const struct i2c_device_id *id)
{
	struct device *dev = &client->dev;
	struct device_node *node = dev->of_node;
	struct sc5338 *sc5338;
	struct v4l2_subdev *sd;
	char facing[2];
	int ret;
	u32 i, hdr_mode = 0;

	dev_info(dev, "driver version: %02x.%02x.%02x",
	         DRIVER_VERSION >> 16,
	         (DRIVER_VERSION & 0xff00) >> 8,
	         DRIVER_VERSION & 0x00ff);

	sc5338 = devm_kzalloc(dev, sizeof(*sc5338), GFP_KERNEL);
	if (!sc5338)
		return -ENOMEM;

	of_property_read_u32(node, OF_CAMERA_HDR_MODE, &hdr_mode);
	ret = of_property_read_u32(node, RKMODULE_CAMERA_MODULE_INDEX,
	                           &sc5338->module_index);
	ret |= of_property_read_string(node, RKMODULE_CAMERA_MODULE_FACING,
	                               &sc5338->module_facing);
	ret |= of_property_read_string(node, RKMODULE_CAMERA_MODULE_NAME,
	                               &sc5338->module_name);
	ret |= of_property_read_string(node, RKMODULE_CAMERA_LENS_NAME,
	                               &sc5338->len_name);
	if (ret) {
		dev_err(dev, "could not get module information!\n");
		return -EINVAL;
	}

	sc5338->client = client;
	for (i = 0; i < ARRAY_SIZE(supported_modes); i++) {
		if (hdr_mode == supported_modes[i].hdr_mode) {
			sc5338->cur_mode = &supported_modes[i];
			break;
		}
	}
	if (i == ARRAY_SIZE(supported_modes))
		sc5338->cur_mode = &supported_modes[0];

	sc5338->xvclk = devm_clk_get(dev, "xvclk");
	if (IS_ERR(sc5338->xvclk)) {
		dev_err(dev, "Failed to get xvclk\n");
		return -EINVAL;
	}

	sc5338->reset_gpio = devm_gpiod_get(dev, "reset", GPIOD_OUT_LOW);
	if (IS_ERR(sc5338->reset_gpio))
		dev_warn(dev, "Failed to get reset-gpios\n");

	sc5338->pwdn_gpio = devm_gpiod_get(dev, "pwdn", GPIOD_OUT_LOW);
	if (IS_ERR(sc5338->pwdn_gpio))
		dev_warn(dev, "Failed to get pwdn-gpios\n");

	sc5338->pinctrl = devm_pinctrl_get(dev);
	if (!IS_ERR(sc5338->pinctrl)) {
		sc5338->pins_default =
		        pinctrl_lookup_state(sc5338->pinctrl,
		                             OF_CAMERA_PINCTRL_STATE_DEFAULT);
		if (IS_ERR(sc5338->pins_default))
			dev_err(dev, "could not get default pinstate\n");

		sc5338->pins_sleep =
		        pinctrl_lookup_state(sc5338->pinctrl,
		                             OF_CAMERA_PINCTRL_STATE_SLEEP);
		if (IS_ERR(sc5338->pins_sleep))
			dev_err(dev, "could not get sleep pinstate\n");
	} else {
		dev_err(dev, "no pinctrl\n");
	}

	ret = sc5338_configure_regulators(sc5338);
	if (ret) {
		dev_err(dev, "Failed to get power regulators\n");
		return ret;
	}

	mutex_init(&sc5338->mutex);

	sd = &sc5338->subdev;
	v4l2_i2c_subdev_init(sd, client, &sc5338_subdev_ops);
	ret = sc5338_initialize_controls(sc5338);
	if (ret)
		goto err_destroy_mutex;

	ret = __sc5338_power_on(sc5338);
	if (ret)
		goto err_free_handler;

	ret = sc5338_check_sensor_id(sc5338, client);
	if (ret)
		goto err_power_off;

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
	sd->internal_ops = &sc5338_internal_ops;
	sd->flags |= V4L2_SUBDEV_FL_HAS_DEVNODE |
	             V4L2_SUBDEV_FL_HAS_EVENTS;
#endif
#if defined(CONFIG_MEDIA_CONTROLLER)
	sc5338->pad.flags = MEDIA_PAD_FL_SOURCE;
	sd->entity.function = MEDIA_ENT_F_CAM_SENSOR;
	ret = media_entity_pads_init(&sd->entity, 1, &sc5338->pad);
	if (ret < 0)
		goto err_power_off;
#endif

	memset(facing, 0, sizeof(facing));
	if (strcmp(sc5338->module_facing, "back") == 0)
		facing[0] = 'b';
	else
		facing[0] = 'f';

	snprintf(sd->name, sizeof(sd->name), "m%02d_%s_%s %s",
	         sc5338->module_index, facing,
	         SC5338_NAME, dev_name(sd->dev));
	ret = v4l2_async_register_subdev_sensor_common(sd);
	if (ret) {
		dev_err(dev, "v4l2 async register subdev failed\n");
		goto err_clean_entity;
	}

	pm_runtime_set_active(dev);
	pm_runtime_enable(dev);
	pm_runtime_idle(dev);

	return 0;

err_clean_entity:
#if defined(CONFIG_MEDIA_CONTROLLER)
	media_entity_cleanup(&sd->entity);
#endif
err_power_off:
	__sc5338_power_off(sc5338);
err_free_handler:
	v4l2_ctrl_handler_free(&sc5338->ctrl_handler);
err_destroy_mutex:
	mutex_destroy(&sc5338->mutex);

	return ret;
}

static int sc5338_remove(struct i2c_client *client)
{
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct sc5338 *sc5338 = to_sc5338(sd);

	v4l2_async_unregister_subdev(sd);
#if defined(CONFIG_MEDIA_CONTROLLER)
	media_entity_cleanup(&sd->entity);
#endif
	v4l2_ctrl_handler_free(&sc5338->ctrl_handler);
	mutex_destroy(&sc5338->mutex);

	pm_runtime_disable(&client->dev);
	if (!pm_runtime_status_suspended(&client->dev))
		__sc5338_power_off(sc5338);
	pm_runtime_set_suspended(&client->dev);

	return 0;
}

#if IS_ENABLED(CONFIG_OF)
static const struct of_device_id sc5338_of_match[] = {
	{ .compatible = "smartsens,sc5338" },
	{},
};
MODULE_DEVICE_TABLE(of, sc5338_of_match);
#endif

static const struct i2c_device_id sc5338_match_id[] = {
	{ "smartsens,sc5338", 0 },
	{ },
};

static struct i2c_driver sc5338_i2c_driver = {
	.driver = {
		.name = SC5338_NAME,
		.pm = &sc5338_pm_ops,
		.of_match_table = of_match_ptr(sc5338_of_match),
	},
	.probe		= &sc5338_probe,
	.remove		= &sc5338_remove,
	.id_table	= sc5338_match_id,
};

static int __init sensor_mod_init(void)
{
	return i2c_add_driver(&sc5338_i2c_driver);
}

static void __exit sensor_mod_exit(void)
{
	i2c_del_driver(&sc5338_i2c_driver);
}

device_initcall_sync(sensor_mod_init);
module_exit(sensor_mod_exit);

MODULE_DESCRIPTION("smartsens sc5338 sensor driver");
MODULE_LICENSE("GPL v2");
