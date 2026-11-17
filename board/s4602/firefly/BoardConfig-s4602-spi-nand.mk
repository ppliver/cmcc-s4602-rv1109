#!/bin/bash
# S4602 (CMCC 千里眼, RV1109 / SPI-NAND 256MiB) board config for Firefly
# rv1126_rv1109 BSP (manifest rv1126_rv1109_linux_release, tag linux_release_v2.2.5g).
#
# Base: a copy of device/rockchip/rv1126_rv1109/BoardConfig-spi-nand.mk with the
# S4602 deltas below. Place this at device/rockchip/rv1126_rv1109/ in the SDK tree,
# then:  ./build.sh BoardConfig-s4602-spi-nand.mk   (or select via build.sh launch)
#
# S4602 deltas vs the stock SPI-NAND board config:
#   RK_KERNEL_DEFCONFIG_FRAGMENT -> our fragment (SC5338 + YT8512B + vendor mtdparts/cmdline)
#   RK_KERNEL_DTS                -> Stage 1 uses the stock RV1109 SPI-NAND dts to prove the
#                                    kernel FIT builds; golden DTS is docs/evidence/vendor-kernel.dtb
#   RK_PARAMETER                 -> MUST be the vendor 14-partition GPT (board/s4602), NOT
#                                    Firefly's 7-partition parameter-fit-nand-256M.txt
#   RK_CFG_BUILDROOT             -> Stage 2 (Buildroot rootfs + rkmedia RTSP demo)

# Target chip / arch
export RK_CHIP=RV1126
export RK_ARCH=arm
export RK_TARGET_PRODUCT=rv1126_rv1109

# U-Boot (Stage 1 we do NOT build U-Boot; keep stock settings, build only kernel+rootfs)
export RK_UBOOT_DEFCONFIG=rv1126
export RK_UBOOT_DEFCONFIG_FRAGMENT=rk-sfc.config
export RK_LOADER_UPDATE_SPL=true
export RK_UBOOT_FORMAT_TYPE=fit

# Kernel
export RK_KERNEL_DEFCONFIG=rv1126
export RK_KERNEL_DEFCONFIG_FRAGMENT=s4602_fragment.firefly.config
export RK_KERNEL_DTS=rv1109-38-v10-spi-nand
export RK_BOOT_IMG=zboot.img
export RK_KERNEL_IMG=kernel/arch/arm/boot/zImage
export RK_KERNEL_FIT_ITS=boot.its

# GPT / parameter -- vendor layout, 14 partitions (see board/s4602/board.yaml)
# The actual GPT bytes come from tools/generate_parameter from board.yaml; RK_PARAMETER
# points at the Firefly-named file that we overwrite with the vendor GPT in the workflow.
export RK_PARAMETER=parameter-fit-nand-256M.txt

# Rootfs (Stage 1: reuse existing UBI; Stage 2: Buildroot UBI)
export RK_ROOTFS_TYPE=ubi
export RK_ROOTFS_IMG=rockdev/rootfs.${RK_ROOTFS_TYPE}
export RK_CFG_BUILDROOT=rockchip_rv1126_rv1109_spi_nand

# OEM IPC tree (Stage 2: where rkmedia demo + iqfiles land)
export RK_OEM_DIR=oem_ipc
export RK_OEM_BUILDIN_BUILDROOT=YES
export RK_OEM_FS_TYPE=ubi

# Build
export RK_JOBS=8
export RK_TARGET_BOARD_ARRAY=
