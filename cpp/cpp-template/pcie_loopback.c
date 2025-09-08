#include <linux/module.h>
#include <linux/init.h>
#include <linux/pci.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/cdev.h>

#define VENDOR_ID 0x10ee  // Xilinx
#define DEVICE_ID 0x7028  // Replace with your actual PCIe core ID

#define BAR 0
#define DEV_NAME "pcie_loopback"

static struct pci_dev *pdev;
static void __iomem *bar0_addr;
static int major;
static struct cdev pcie_cdev;

static ssize_t pcie_read(struct file *f, char __user *buf, size_t len, loff_t *off) {
    uint32_t val;
    val = ioread32(bar0_addr);
    return copy_to_user(buf, &val, sizeof(val)) ? -EFAULT : sizeof(val);
}

static ssize_t pcie_write(struct file *f, const char __user *buf, size_t len, loff_t *off) {
    uint32_t val;
    if (copy_from_user(&val, buf, sizeof(val)))
        return -EFAULT;
    iowrite32(val, bar0_addr);
    return sizeof(val);
}

static struct file_operations fops = {
    .owner = THIS_MODULE,
    .read = pcie_read,
    .write = pcie_write
};

static int __init pcie_init(void) {
    struct pci_dev *dev = NULL;
    while ((dev = pci_get_device(VENDOR_ID, DEVICE_ID, dev))) {
        if (pci_enable_device(dev)) continue;
        bar0_addr = pci_iomap(dev, BAR, 0);
        if (!bar0_addr) return -ENOMEM;
        pdev = dev;
        break;
    }

    major = register_chrdev(0, DEV_NAME, &fops);
    if (major < 0) return major;

    cdev_init(&pcie_cdev, &fops);
    cdev_add(&pcie_cdev, MKDEV(major, 0), 1);

    printk(KERN_INFO "pcie_loopback loaded.\n");
    return 0;
}

static void __exit pcie_exit(void) {
    if (bar0_addr) pci_iounmap(pdev, bar0_addr);
    if (pdev) pci_disable_device(pdev);
    cdev_del(&pcie_cdev);
    unregister_chrdev(major, DEV_NAME);
    printk(KERN_INFO "pcie_loopback unloaded.\n");
}

module_init(pcie_init);
module_exit(pcie_exit);
MODULE_LICENSE("GPL");
