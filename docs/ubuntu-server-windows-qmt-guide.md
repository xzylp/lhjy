# Ubuntu Server 宿主机 + Windows 虚拟机(QMT) 部署指南

## 目标

本文档面向以下场景：

- 迷你主机安装 `Ubuntu Server 24.04 LTS`
- 宿主机长期运行 Linux 服务
- Windows 作为常驻虚拟机运行
- QMT 安装在 Windows 虚拟机中
- 通过 `ThinkPad T14p` 远程管理宿主机和 Windows 虚拟机

推荐方案：

- 宿主机：`Ubuntu Server 24.04 LTS`
- 虚拟化：`KVM + QEMU + libvirt`
- Web 管理：`Cockpit + cockpit-machines`
- Windows 远程：`RDP` 为主，`RustDesk/ToDesk/AnyDesk` 为辅

不推荐一开始就装完整 Linux 桌面。当前目标是“Windows 虚拟机易管理”，不是“宿主机本地桌面易操作”。

## 架构建议

建议最终结构如下：

1. 迷你主机运行 Ubuntu Server。
2. Ubuntu Server 提供 SSH 和 Cockpit Web 管理。
3. Ubuntu Server 内通过 KVM/libvirt 运行一个 Windows 虚拟机。
4. QMT 安装在 Windows 虚拟机中。
5. T14p 负责：
   - SSH 登录宿主机
   - 浏览器打开 Cockpit 管理虚拟机
   - 使用远程桌面连接 Windows 虚拟机

## 为什么不优先装 Ubuntu Desktop

对你的目标来说，`Ubuntu Server + KVM/libvirt + Cockpit` 更合适，原因如下：

- 宿主机更轻，内存和后台服务占用更低
- 长期稳定性通常优于 Desktop
- Windows 虚拟机才是主要交互面
- Cockpit 足够承担大部分图形化管理需求
- 后期要跑 Docker、监控、脚本、定时任务更自然

如果将来你确定必须在宿主机本地长期使用图形桌面，再考虑补轻量桌面环境，例如 `XFCE`。

## 硬件建议

最低建议：

- 内存：`32GB`
- SSD：`1TB`
- 网线：建议长期接入

更舒适的建议：

- 内存：`64GB`
- SSD：`1TB` 或 `2TB`

对 QMT 这种长期常驻的 Windows 虚拟机场景，`16GB` 会比较吃紧，不建议。

## 宿主机安装阶段建议

安装 Ubuntu Server 时：

1. 直接安装 `Ubuntu Server 24.04 LTS`
2. 勾选 `OpenSSH server`
3. 分区建议先用整盘安装，后续再按目录规划数据
4. 宿主机名建议固定，例如 `mini-server`

安装完成后的最小初始化：

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y curl wget git vim htop tmux jq net-tools ca-certificates gnupg lsb-release
sudo timedatectl set-timezone Asia/Shanghai
sudo ufw allow 22/tcp
sudo ufw enable
sudo systemctl enable ssh
```

## 安装 KVM/libvirt

Ubuntu Server 官方文档推荐使用 `libvirt` 管理 KVM 虚拟机。

安装：

```bash
sudo apt update
sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst bridge-utils ovmf
```

检查 KVM 和 libvirt：

```bash
sudo systemctl enable --now libvirtd
sudo systemctl status libvirtd --no-pager
kvm-ok
virsh list --all
```

把当前用户加入相关组：

```bash
sudo usermod -aG libvirt,kvm $USER
newgrp libvirt
newgrp kvm
```

再次验证：

```bash
virsh uri
virsh list --all
```

## 安装 Cockpit Web 管理

Cockpit 可以在浏览器中管理服务器，`cockpit-machines` 可以管理 libvirt/KVM 虚拟机。

安装：

```bash
sudo apt install -y cockpit cockpit-machines
sudo systemctl enable --now cockpit.socket
sudo systemctl status cockpit.socket --no-pager
sudo ufw allow 9090/tcp
```

浏览器访问：

```text
https://宿主机IP:9090
```

使用宿主机 Linux 用户名和密码登录。

## Windows 虚拟机安装前准备

你需要准备两个 ISO：

1. Windows 安装镜像
2. VirtIO 驱动镜像

VirtIO 驱动很重要，否则 Windows 虚拟机的磁盘和网卡性能会明显受限。

建议准备：

- Windows 11 或 Windows 10 官方 ISO
- VirtIO 驱动 ISO

如果你还没准备好，可以先创建虚拟机，后续再挂载驱动 ISO。

## Windows 虚拟机资源建议

如果宿主机是 `32GB` 内存，建议先这样分：

- vCPU：`6` 到 `8`
- 内存：`12GB` 到 `16GB`
- 系统盘：`150GB` 到 `200GB`

如果宿主机是 `64GB` 内存：

- vCPU：`8` 到 `12`
- 内存：`16GB` 到 `24GB`
- 系统盘：`200GB+`

QMT 更关注稳定性和持续运行，不需要把 CPU 和内存一次给满。

## 网络模式怎么选

初期建议：

- 先用 `NAT`

理由：

- 更稳
- 最省事
- 不容易把宿主机网络搞断
- 足够完成 Windows 安装、远程桌面和 QMT 初步验证

后期如果你明确需要：

- Windows 虚拟机直接占用局域网独立 IP
- 局域网里其他设备直接访问 Windows VM

再切换为 `Bridge`。

## libvirt 网桥与桥接建议

### 推荐顺序

1. 先用 NAT 完成 Windows 安装和 QMT验证
2. 确认系统稳定后再决定是否改桥接

### NAT 的优点

- 配置最简单
- 对宿主机影响最小
- 适合作为第一阶段的稳定方案

### Bridge 的优点

- Windows VM 拿到独立局域网 IP
- 局域网远程桌面更自然
- 某些依赖广播或局域网发现的场景更方便

### Bridge 的缺点

- 网络配置更容易出错
- 宿主机可能因桥接配置不当断网

## 如果后续需要桥接

Ubuntu Server 使用 Netplan 管理网络。

桥接示例，假设物理网卡是 `enp3s0`：

```yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    enp3s0:
      dhcp4: no
  bridges:
    br0:
      dhcp4: yes
      interfaces:
        - enp3s0
```

应用配置：

```bash
sudo netplan apply
```

改桥接前务必先：

- 本地接显示器
- 备份 `/etc/netplan/*.yaml`
- 准备好回滚方案

## 创建 Windows 虚拟机的建议

优先在 Cockpit 的 `Virtual Machines` 页面创建。

推荐参数：

- 固件：`UEFI/OVMF`
- 芯片组：默认即可
- 磁盘总线：优先 `VirtIO`
- 网卡模型：优先 `VirtIO`
- 显示：默认图形设备即可

如果安装时看不到磁盘：

- 这是正常现象
- 说明还没有加载 VirtIO 存储驱动
- 在安装界面选择 `Load driver`
- 从 VirtIO ISO 中加载对应的存储驱动

## Windows 安装完成后必须做的事

1. 安装 VirtIO 驱动
2. 安装 QEMU Guest Agent
3. 安装远程桌面或远程控制工具
4. 调整电源策略
5. 配置自动登录
6. 配置虚拟机开机自启动

## Windows 虚拟机内建议配置

### 1. 远程连接

建议优先：

- `Windows 远程桌面 RDP`

可选：

- `RustDesk`
- `ToDesk`
- `AnyDesk`

RDP 优点：

- 局域网内最好用
- 开销低
- 稳定性通常最好

### 2. 电源设置

在 Windows 虚拟机内：

- 关闭自动睡眠
- 关闭自动休眠
- 关闭 USB 省电
- 关闭显示器自动关闭不重要，但可以一起关掉

目标是：

- QMT 长期开机不掉线

### 3. 自动登录

如果这台 Windows VM 是专门跑 QMT 的，可以配置自动登录，避免宿主机重启后 Windows 停在登录界面。

### 4. 时间同步

QMT 对时间敏感，建议：

- 保持宿主机时区正确
- Windows 虚拟机使用自动同步时间
- 安装 QEMU Guest Agent 后观察时间是否稳定

## 设置 Windows 虚拟机开机自启动

安装完成并验证稳定后，在宿主机执行：

```bash
virsh list --all
virsh autostart <你的虚拟机名称>
virsh dominfo <你的虚拟机名称>
```

确认输出里有：

- `Autostart: enable`

如果宿主机重启后还希望自动运行，可以额外验证：

```bash
sudo systemctl status libvirtd --no-pager
virsh list --all
```

## QMT 放在虚拟机里时要特别注意的点

QMT 能否稳定长期运行，关键不只是“系统装上”，还包括以下几类约束：

1. 是否依赖 USB 设备
2. 是否依赖券商驱动或本地组件
3. 是否要求固定网络环境
4. 是否对 Windows 自动登录敏感
5. 是否对睡眠、重启、更新时间敏感

因此建议你按下面顺序推进：

1. 先把 Windows VM 装好
2. 先验证远程桌面稳定
3. 再装 QMT
4. 再做一轮开机自启和断线恢复测试

## QMT 场景下推荐的稳定性策略

### 第一阶段

- 宿主机：Ubuntu Server
- Windows VM：NAT 网络
- 远程方式：RDP
- 目标：先确保 QMT 能正常安装、运行、登录

### 第二阶段

- 需要独立 IP 时，再切到桥接
- 需要 USB 设备时，再做 USB 直通
- 需要更强恢复能力时，再补监控脚本和自动重启

## 宿主机建议安装的额外工具

```bash
sudo apt install -y qemu-guest-agent
sudo apt install -y btop nvme-cli smartmontools
```

说明：

- `btop`：看资源占用
- `nvme-cli`：看 NVMe 状态
- `smartmontools`：看磁盘健康

## 推荐的运维检查命令

查看虚拟机：

```bash
virsh list --all
virsh dominfo <vm-name>
virsh net-list --all
```

查看 KVM 相关服务：

```bash
systemctl status libvirtd --no-pager
systemctl status cockpit.socket --no-pager
```

查看资源：

```bash
free -h
df -h
htop
```

## 最小落地步骤

如果你想最快进入可用状态，推荐按下面顺序：

1. 安装 Ubuntu Server
2. 装 `KVM/libvirt`
3. 装 `Cockpit + cockpit-machines`
4. 用 NAT 创建 Windows VM
5. 安装 Windows
6. 安装 VirtIO 驱动
7. 安装远程桌面
8. 装 QMT
9. 设置 `virsh autostart`
10. 做一次宿主机重启验证

## 最终建议

对你当前的目标，最优先的是：

- 不装宿主机桌面
- 先跑通 `Ubuntu Server + Windows VM + QMT`
- 先求稳，再求复杂网络和高级直通

这条路线比“直接装 Ubuntu Desktop 再折腾 Windows VM”更适合长期常驻。

## 参考资料

- Ubuntu Server 虚拟化总览：<https://documentation.ubuntu.com/server/how-to/virtualisation/>
- Ubuntu Server libvirt：<https://documentation.ubuntu.com/server/how-to/virtualisation/libvirt/>
- Ubuntu Server 基础安装：<https://ubuntu.com/server/docs/tutorial/basic-installation>
- Ubuntu Server 网络与 Netplan：<https://documentation.ubuntu.com/server/explanation/networking/configuring-networks/>
- Cockpit 手册：<https://cockpit-project.org/guide/latest/cockpit-manual>
- Cockpit Virtual Machines：<https://cockpit-project.org/guide/195/feature-virtualmachines.html>
- libvirt VirtIO 说明：<https://libvirt.gitlab.io/libvirt-wiki/Virtio.html>

