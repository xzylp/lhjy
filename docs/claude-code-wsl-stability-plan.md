# Claude Code 稳定运行方案（WSL2 / Windows 机器）

> 适用对象：当前开发机器为 Windows + WSL2，主要使用 VS Code、Claude Code、OpenClaw、A 股量化项目  
> 日期：2026-04-06

---

## 1. 结论

对这台机器，Claude Code 的推荐主运行方案是：

- **运行环境：WSL2**
- **安装方式：官方 native installer**
- **更新通道：`stable`**
- **编辑器：VS Code Remote - WSL**
- **仓库长期位置：WSL Linux 文件系统 (`~/code/...`)**

不推荐长期使用的组合：

- native Windows Claude Code
- npm 版 Claude Code 作为主安装方式
- Windows 本地 VS Code + WSL 挂载路径混用
- 核心仓库长期放在 `/mnt/d/...`

---

## 2. 当前机器状态

本机已确认状态：

- OS：WSL2
- Node：`v22.22.2`
- npm：`10.9.7`
- Claude Code：`2.1.92`
- 当前安装方式：**npm 全局安装**
- 当前命令路径：`/home/yxz/.npm-global/bin/claude`
- 当前工作目录：`/mnt/d/Coding/lhjy`

说明：

- 当前并不是原生 Windows Claude Code，而是 **WSL 中的 npm 版 Claude Code**
- 这已经比 native Windows 稳一些
- 但仍不是最稳方案，因为：
  - npm 安装方式已被官方标记为 deprecated
  - 核心仓库仍在 `/mnt/d/...` 挂载盘

---

## 3. 为什么推荐这套方案

## 3.1 原因一：官方方向就是 WSL / native installer

Claude Code 官方文档的关键信息：

- 推荐安装方式：**native install**
- npm 安装方式：**deprecated**
- Windows 推荐方式：**Git Bash 或 WSL**
- WSL2 支持 sandboxing
- 官方提供了从 npm 迁移到 native 的方式

参考：

- <https://code.claude.com/docs/en/setup>

## 3.2 原因二：Windows 文件写入链路历史问题较多

Claude Code 在 Windows / VS Code / WSL 混用环境下，历史上出现过多类文件写入或路径解析问题：

- Windows 文件系统 provider 异常
- 读过文件后仍提示“未读取 / 已修改”
- Windows 路径与 `/mnt/...` 路径混淆

因此，不建议把核心开发主链路建立在 native Windows 路线上。

---

## 4. 推荐运行架构

```text
Windows
  └─ VS Code
       └─ Remote - WSL
            └─ WSL2 Ubuntu / Debian
                 ├─ Claude Code (native installer)
                 ├─ Node / npm
                 ├─ Git
                 └─ Repo in ~/code/...
```

设计原则：

- **Claude Code 在 WSL 内运行**
- **VS Code 只作为前端**
- **仓库优先放在 Linux 文件系统**
- **避免 Windows provider 参与实际文件写入链路**

---

## 5. 目标状态

迁移完成后，理想状态应为：

- `claude` 不是 npm 全局包，而是 native 安装版本
- `claude --version` 正常
- `claude doctor` 正常
- VS Code 始终通过 Remote - WSL 打开仓库
- 常用核心仓库路径形如：

```bash
~/code/ashare-system-v2
~/code/ashare-system
~/code/xtquantservice
```

---

## 6. 迁移步骤

## 6.1 第一步：安装 native 版 Claude Code

在 WSL 中执行：

```bash
curl -fsSL https://claude.ai/install.sh | bash -s stable
```

说明：

- 推荐 `stable`
- 不推荐直接用 `latest`
- `stable` 通常比最新版本滞后约一周，适合减少刚发布回归问题

安装完成后检查：

```bash
which claude
claude --version
claude doctor
```

目标：

- `which claude` 不再指向 `~/.npm-global/bin/claude`
- `claude doctor` 无关键错误

---

## 6.2 第二步：确认 native 版可用后卸载 npm 版

确认 native 版工作正常后，再执行：

```bash
npm uninstall -g @anthropic-ai/claude-code
```

这样做的目的：

- 避免 PATH 命中旧版本
- 避免混用两个 `claude`

卸载后复查：

```bash
which claude
claude --version
```

---

## 6.3 第三步：固定更新通道到 stable

建议在 Claude Code 配置中设置：

```json
{
  "autoUpdatesChannel": "stable"
}
```

理由：

- 开发机以稳定优先
- 避免每次撞最新回归

---

## 6.4 第四步：固定 VS Code 使用方式

以后统一这样使用：

1. 从 Windows 打开 VS Code
2. 使用 **Remote - WSL**
3. 在 WSL 中打开项目目录
4. 集成终端使用 WSL shell

不建议：

- 直接在 Windows 本地窗口打开 WSL 仓库
- 在同一个仓库上同时混用 Windows 终端和 WSL 终端

---

## 6.5 第五步：仓库路径迁移策略

### 短期方案

现有仓库可暂时继续放在：

```bash
/mnt/d/Coding/lhjy
```

优点：

- 迁移成本低
- 不影响现有脚本和路径引用

缺点：

- 仍然属于 Windows 挂载盘
- 文件写入、路径、大小写、watcher 类问题概率更高

### 长期推荐方案

将高频开发仓库迁到：

```bash
~/code/
```

推荐布局：

```bash
~/code/
  ashare-system-v2/
  ashare-system/
  xtquantservice/
~/data/
  qmt/
  exports/
  archives/
```

优先迁移的仓库：

- `ashare-system-v2`
- 任何需要频繁用 Claude Code 改写文件的仓库

可以暂时不迁的内容：

- 导出文件
- 大型静态数据
- 临时脚本
- 归档资料

---

## 7. 建议的执行策略

不要一次性全迁。

建议采用两步走：

### 阶段 A：先迁安装方式

- 把 Claude Code 从 npm 迁到 native
- 保持仓库仍在 `/mnt/d/...`
- 观察 1 到 3 天

### 阶段 B：如仍有写文件问题，再迁仓库

- 先迁 `ashare-system-v2`
- 再迁 `ashare-system`
- 最后再决定是否迁其他仓库

这样做的好处：

- 风险最小
- 容易定位问题究竟来自安装方式还是仓库路径

---

## 8. 高风险场景避坑清单

以下场景应尽量避免：

- native Windows Claude Code 直接操作 WSL 仓库
- 同时使用 Windows 终端和 WSL 终端改同一仓库
- 仓库放在 OneDrive 下
- 将 Claude Code 主工作区长期放在 `/mnt/c`、`/mnt/d`
- PATH 中同时存在 native 版和 npm 版 `claude`
- 用最新实验版本作为主工作版本

---

## 9. 推荐检查命令

## 9.1 环境检查

```bash
uname -a
node -v
npm -v
which claude
claude --version
claude doctor
```

## 9.2 仓库位置检查

```bash
pwd
```

如果路径类似：

```bash
/mnt/d/...
```

说明当前仍在 Windows 挂载盘。

如果路径类似：

```bash
/home/<user>/code/...
```

说明当前已经在 WSL Linux 文件系统中。

---

## 10. 推荐决策

对本机的实际建议如下：

### 现在立刻做

1. 安装 native 版 Claude Code
2. 切到 `stable` 更新通道
3. 卸载 npm 版
4. 固定使用 VS Code Remote - WSL

### 暂时不急着做

1. 不必马上迁所有仓库
2. 不必马上改所有脚本路径

### 如果后续仍出现写文件异常

优先执行：

1. 先将 `ashare-system-v2` 迁到 `~/code/ashare-system-v2`
2. 再观察 Claude Code 文件写入是否恢复稳定

---

## 11. 一句话版本

这台机器上，Claude Code 的长期稳定方案应当是：

**WSL2 + native installer + stable channel + VS Code Remote - WSL + 核心仓库逐步迁到 `~/code`。**
