// 编译期 net_device 布局探测：把 offsetof 常量放进 .rodata，离线 objdump 读出。
// 用途：在 CI 里断言 tun.ko 的编译 config 与 vendor #47 的 struct net_device 布局一致。
// 目标值（vendor 4.19.111 #47，从 vendor-vmlinux 反汇编得到）：
//   netdev_ops=0x120  mtu=0x140  min_mtu=0x144  max_mtu=0x148
// 差一个 CONFIG_IPV6 就会让 mtu 簇整体偏移 4 字节（缺 ndisc_ops 指针）。
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/stddef.h>

const unsigned int lay_size       = sizeof(struct net_device);
const unsigned int lay_netdev_ops = offsetof(struct net_device, netdev_ops);
const unsigned int lay_mtu        = offsetof(struct net_device, mtu);
const unsigned int lay_min_mtu    = offsetof(struct net_device, min_mtu);
const unsigned int lay_max_mtu    = offsetof(struct net_device, max_mtu);
const unsigned int lay_flags      = offsetof(struct net_device, flags);
const unsigned int lay_txqlen     = offsetof(struct net_device, tx_queue_len);

static int __init lay_init(void) { return 0; }
static void __exit lay_exit(void) {}
module_init(lay_init);
module_exit(lay_exit);
MODULE_LICENSE("GPL");
