
# Ubuntu Server + Windows(QMT) 当前网络与联调报告

## 审核结论

这份报告以 [`ubuntu-server-windows-qmt-guide.md`](/mnt/d/Coding/lhjy/ashare-system-v2/docs/ubuntu-server-windows-qmt-guide.md) 的既有部署框架为依据，只收口当前已经落地并可联调的网络事实。

当前可确认结论如下：

- 联调主线已经明确为：
  - Linux 宿主机负责 `control plane`
  - Windows 虚拟机负责 `QMT + Windows Execution Gateway`
- Linux 端程序**不直接持有 QMT 会话**
- Linux 与 Windows/QMT 之间应通过 `Windows Execution Gateway / 桥接层` 通讯
- 宿主机、虚拟机、共享目录、管理入口和调试投递路径都已经具备稳定口径
- 当前已验证的关键网络事实足以支持项目联调，不需要再把 `127.0.0.1` 当成跨机通信地址

## 目标

本文档只回答当前联调最关心的三件事：

1. Linux 端程序怎么和 Windows 虚拟机里的 `QMT` 通讯
2. 当前宿主机、虚拟机、共享目录和管理入口分别是什么地址
3. 联调时应该按什么边界和检查项来判断链路是否正确

## 核心结论

当前建议直接按下面这条链路理解系统：

```text
外部访问方
├── 局域网客户端
└── Tailscale 远程客户端
        │
        ▼
Ubuntu Server 宿主机
├── 局域网地址：192.168.99.16
├── Tailscale 地址：100.75.91.75
├── libvirt NAT 网关：192.168.122.1
├── ashare-system-v2 / OpenClaw / 只读控制面
└── vmshare 共享目录
        │
        ▼
Windows VM: win11-qmt
├── NAT 网段地址：192.168.122.66
├── QMT
├── Windows Execution Gateway
└── 仅承担执行面与 Windows 侧通讯能力
```

职责边界：

- Linux：主业务逻辑、控制面 API、只读报告、调度、OpenClaw、共享目录
- Windows VM：`QMT`、执行桥、必要的 Windows 侧数据通讯
- 外部调用方不要把 QMT 当成直接访问对象，应通过 Linux control plane 或约定好的 Windows Gateway 接口访问

## 为什么采用这个口径

原因只有三条：

1. `QMT` 稳定运行环境仍然是 Windows
2. Linux 更适合长期承载 `ashare-system-v2`、`OpenClaw`、共享、运维和只读控制面
3. 把 Windows 收敛成执行面后，联调边界最清晰，后续迁移和故障定位也最简单

## 当前网络拓扑

### 1. 宿主机地址

- 主机名：`yxzmini`
- 局域网地址：`192.168.99.16`
- Tailscale 地址：`100.75.91.75`
- libvirt NAT 网关地址：`192.168.122.1`

### 2. Windows 虚拟机地址

- 虚拟机名称：`win11-qmt`
- 当前约定 NAT 地址：`192.168.122.66`

说明：

- 这个 `192.168.122.66` 是 Windows VM 位于 libvirt NAT 网段内的地址
- Linux 宿主机通过 `192.168.122.0/24` 网段与该虚拟机通信
- Windows VM 访问宿主机时，不应使用 `127.0.0.1`，而应使用宿主机的可达地址

### 3. 当前已知管理入口

- Cockpit：`https://192.168.99.16:9090` 或 `https://100.75.91.75:9090`
- NoMachine：`192.168.99.16:4000` 或 `100.75.91.75:4000`
- Samba 共享对 Windows VM 的访问入口：`\\\\192.168.122.1\\vmshare`

## 网络建议

当前联调阶段继续沿用 `NAT` 口径是可接受的，但必须明确以下规则：

- Windows VM 到 Linux control plane 的访问地址，必须写成宿主机可达地址
- 不能把 `127.0.0.1` 当成最终联调地址
- Linux 访问 Windows VM 时，应优先使用 `192.168.122.66`
- 远程管理宿主机时，可优先使用 `100.75.91.75`

当前推荐的地址选用方式：

- Windows VM -> Linux control plane：
  - 优先使用 `192.168.99.16`
  - 如果只在 libvirt NAT 内联调，也可按实际可达性改用 `192.168.122.1`
- Linux -> Windows VM：
  - 直接使用 `192.168.122.66`
- 外部远程管理 -> Linux：
  - 使用 `100.75.91.75`

## 当前项目的目标运行结构

当前 `ashare-system-v2` 的正式运行口径，与原部署指南保持一致：

```text
Ubuntu Server
├── ashare-system-v2 API / scheduler / review / learning
├── OpenClaw gateway
├── 只读控制面与部署验收接口
└── Linux 侧共享与运维入口

Windows VM
├── QMT
├── Windows Execution Gateway
└── Windows 侧必要通讯组件
```

也就是说：

- Linux 是 `control plane`
- Windows 是 `execution plane`
- 正式口径仍然是 `ASHARE_EXECUTION_PLANE=windows_gateway`

## Linux 与 Windows QMT 的通讯方式

这是本次联调最核心的结论。

### 1. 正确链路

Linux 端程序与 QMT 的通信，不应理解成“Linux 直接连接 QMT 客户端”，而应理解成下面这条链路：

```text
Linux 应用
-> Linux control plane / OpenClaw
-> Windows Execution Gateway
-> QMT
```

这意味着：

- Linux 不直接嵌入或接管 `QMT` 会话
- Windows VM 内必须保留一个可被 Linux 调用的执行桥或网关层
- 真正与 `QMT` 进程打交道的是 Windows 侧桥接程序，而不是 Linux 主业务进程

### 2. 当前已验证的 Windows VM 可达性

以下事实已经验证过：

- Linux 可以访问 `192.168.122.66`
- Windows VM 上的 `58310` 端口当前可被 Linux 连通
- 通过该端口的 HTTP 代理能力已经验证成功

需要特别说明：

- `58310` 当前已验证的是 Windows VM 的代理链路能力
- 这**不是**对 `QMT 执行桥端口` 的最终定义
- 真正的 `Windows Execution Gateway` 业务端口，仍应以 Windows 侧桥接服务的实际配置为准

### 3. 当前联调口径

对项目联调方，建议直接按下面口径理解：

- Linux 侧业务程序只对接：
  - Linux 本机 API
  - Windows Execution Gateway
- Windows VM 负责：
  - 保持 `QMT` 登录会话
  - 接收 Linux 发来的执行请求
  - 把执行结果、健康状态或桥接状态回传给 Linux control plane

## Windows Gateway 联调要求

Windows VM 当前最小要求仍然沿用原方案，只是补充当前网络口径：

1. `QMT` 正常登录并保持会话
2. Windows Execution Gateway 能访问 Linux control plane
3. Linux 能访问 Windows VM 上的桥接服务地址
4. Windows Gateway 能按约定上报执行桥健康状态

当前联调时，至少要确认下面两组地址关系：

- Windows VM 能访问 Linux：
  - `192.168.99.16`
  - 或其他已验证可达的宿主机固定地址
- Linux 能访问 Windows VM：
  - `192.168.122.66`

## 文件交换与调试投递

除接口联调外，当前还有一条已经可用的文件交换链路：

- 宿主机共享根目录：`/srv/vm-share`
- 共享子目录：
  - `/srv/vm-share/commands`
  - `/srv/vm-share/drop`
  - `/srv/vm-share/packages`
- Windows VM 访问路径：
  - `\\\\192.168.122.1\\vmshare`

约定如下：

- 需要 Windows VM 侧执行的调试命令，统一投递到 `/srv/vm-share/commands`
- Windows 与 Linux 间的小文件交换，统一走 `vmshare`
- 不再依赖频繁挂载 ISO 或手动拖文件作为长期方案

## 地址与端口清单

当前可直接给联调方的网络清单如下：

- Linux 宿主机局域网地址：`192.168.99.16`
- Linux 宿主机 Tailscale 地址：`100.75.91.75`
- libvirt NAT 网关：`192.168.122.1`
- Windows VM 地址：`192.168.122.66`
- Cockpit：`9090`
- NoMachine：`4000`
- Windows VM 侧已验证代理端口：`58310`

补充说明：

- `9090`、`4000` 是宿主机管理入口，不是 QMT 执行桥端口
- `58310` 当前仅能确定为已验证可达的 Windows VM 端口，不应擅自当成正式业务协议端口文档化

## 部署后的最小验收

### 1. Linux 本机验收

在 Linux 宿主机上应能完成：

- `ashare-system-v2` 控制面正常运行
- `OpenClaw` 正常运行
- 只读部署检查接口可访问
- 宿主机能访问 Windows VM 地址 `192.168.122.66`

### 2. Windows VM 到 Linux 联通验收

从 Windows VM 上至少确认：

- 能访问 Linux control plane
- 使用的不是 `127.0.0.1`
- 能访问共享路径 `\\\\192.168.122.1\\vmshare`

### 3. 执行桥验收

当 Windows Execution Gateway 完成部署后，应确认：

- Linux 能访问 Windows Gateway 的实际业务地址和端口
- Windows Gateway 能从 Linux control plane 拉取必要配置或回传健康状态
- QMT 会话稳定存在，且桥接层能够把请求传递给 QMT

## 运行建议

当前联调与运维建议如下：

- Linux control plane 继续作为唯一主业务入口
- Windows VM 不扩展成第二套主业务宿主机
- 对外文档统一写“Linux -> Windows Gateway -> QMT”，不要写“Linux 直接连 QMT”
- 共享目录继续作为 Windows 调试命令投递入口
- 远程管理宿主机优先使用：
  - `100.75.91.75`
  - 或 `192.168.99.16`

## 风险点

当前网络与联调层面主要有四个风险点：

1. Windows Execution Gateway 的最终业务端口如果没有固定文档，会导致联调双方各自猜测
2. 如果再次把 `127.0.0.1` 写进跨机配置，Windows 到 Linux 会直接失效
3. 如果 Windows VM 地址后续变化，没有同步更新配置与文档，会造成链路断裂
4. 如果把代理端口误写成 QMT 桥接端口，会导致问题排查方向错误

## 给联调方的结论

本次联调请直接按下面口径执行：

1. Linux 是控制面，不直接接管 QMT
2. Windows VM 是执行面，QMT 只驻留在 Windows 内
3. Linux 与 QMT 的通信链路是：
   - `Linux -> Windows Execution Gateway -> QMT`
4. 当前已确认的基础网络地址是：
   - Linux 宿主机：`192.168.99.16`
   - Tailscale：`100.75.91.75`
   - NAT 网关：`192.168.122.1`
   - Windows VM：`192.168.122.66`
5. 调试文件与命令投递统一走：
   - `/srv/vm-share`
   - `\\\\192.168.122.1\\vmshare`

## 最终结论

基于当前已经验证的网络事实，`ashare-system-v2` 与 Windows 虚拟机内 `QMT` 的联调边界已经明确：

- Linux 负责控制面与主业务
- Windows 负责 `QMT + Windows Execution Gateway`
- 两者通过宿主机与 libvirt NAT 网络完成互通
- 文件交换通过 `vmshare` 完成

这份报告可以直接作为当前项目联调时的网络口径说明文档使用。
