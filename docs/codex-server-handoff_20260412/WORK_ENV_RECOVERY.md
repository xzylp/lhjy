# Codex 工作环境恢复说明

这份文档用于在服务器上恢复与当前会话尽量一致的工作环境。目标不是精确复制会话状态，而是恢复继续工作的关键约束、工具栈和执行口径。

## 1. 先说限制

以下内容可以通过文档恢复：

- 当前项目主线与边界
- 语言与工程规范
- 常用工具与优先级
- 推荐 MCP/skills 清单
- 关键路径、入口、接口、验收顺序

以下内容不能直接被“复制”过去，只能重新配置：

- 当前会话的瞬时上下文窗口
- 已激活的 agent 内存状态
- 本机侧 Codex 用户级 MCP 登录态
- 已批准的命令白名单
- 本地桌面技能依赖的宿主环境

因此，服务器上的新实例需要：

1. 重新读取交接文档
2. 重新激活项目
3. 重新配置或确认 MCP
4. 重新确认 skills 可用性

## 2. 必读文档顺序

新实例接手时，建议按这个顺序阅读：

1. `docs/codex-server-handoff_20260412/HANDOFF.md`
2. `docs/codex-server-handoff_20260412/WORK_ENV_RECOVERY.md`
3. `docs/codex-server-handoff_20260412/ubuntu-server-windows-qmt-guide.md`
4. `docs/codex-server-handoff_20260412/linux-windows-qmt-network-report.md`
5. Windows 侧补充材料：
   - `\\192.168.122.1\vmshare\commands\WINDOWS_执行桥与QMT守护服务技术要求.md`
   - `\\192.168.122.1\vmshare\commands\CODEX_VM_CONTEXT_HANDOFF_20260412.md`

## 3. 语言与回复规则

当前会话的固定约束：

- 只用中文回答
- 分析、解释、代码说明、文档默认中文
- 优先中文术语和中文命名说明

输出风格：

- 直接、简洁、可执行
- 避免空泛解释
- 复杂任务先给主线判断，再给步骤
- 阶段性进展用短句说明

## 4. 工程规则

必须延续的工程约束：

- Python 3.11+
- 4 空格缩进，UTF-8
- 公共函数尽量带类型注解
- 模块/函数/变量使用 `snake_case`
- 类使用 `PascalCase`
- 常量使用 `UPPER_SNAKE_CASE`
- 绝对导入优先
- 不引入平行系统，优先接到现有主链
- 修改时优先保持 API 兼容
- 中文注释只写必要部分，不写废话注释

测试与交付规则：

- 默认补单测或至少做验收
- 后台单测单次尽量控制在 60 秒内
- 优先可验证结果，不交付占位实现

## 5. 当前业务边界

当前主线只围绕：

```text
Linux control plane -> Windows Execution Gateway -> QMT
```

必须坚持：

- Linux 不直接持有 QMT 会话
- Windows Execution Gateway 是唯一正式执行写口
- Linux 负责 control plane / OpenClaw / review / orchestration
- Windows 负责 QMT / XtQuant / 执行桥 / 运维接口

不要再走这些错误方向：

- Linux 直连 QMT
- 把 `58310` 当正式业务端口
- 混淆 OpenClaw、control plane、execution gateway 的职责
- 在 Linux 侧伪造执行成功态

## 6. 项目路径与关键目录

服务器侧：

- 项目目录：`/srv/projects/ashare-system-v2`
- 状态目录：`/srv/data/ashare-state`
- 交接目录：`/srv/projects/ashare-system-v2/docs/codex-server-handoff_20260412`

共享目录：

- `\\192.168.122.1\vmshare\commands`
- `\\192.168.122.1\vmshare\packages`

OpenClaw：

- 有效目录：`/home/yxz/.openclaw-quant`
- `~/.openclaw` 已指向该目录

## 7. 工具使用优先级

### 本地代码理解与编辑

优先级：

1. Serena
2. `rg`
3. 普通 shell 读取

约束：

- 先用符号级工具理解，不要上来整文件通读
- 如果只改小段文本，再考虑精确文本编辑
- 搜索优先 `rg`

### 复杂任务拆解

优先 `sequential-thinking`

适用：

- 多步联调
- 部署方案收口
- 复杂故障定位
- 多阶段验收计划

### 文档与官方资料

- OpenAI 相关：优先 `openaiDeveloperDocs`
- 第三方库/框架：优先 `context7`
- 最新网页信息：优先 `exa`
- 大仓库语义检索：优先 `serena`
- 多源文档聚合：优先 `deepwiki`

## 8. 推荐恢复的 MCP 清单

服务器侧如果要恢复接近当前工作环境，至少应确保这些 MCP 可用：

- `serena`
- `sequential-thinking`
- `exa`
- `context7`
- `deepwiki`
- `openaiDeveloperDocs`

推荐用途：

- `serena`：符号级检索、引用分析、精确改动
- `sequential-thinking`：联调拆解与执行计划
- `exa`：查最新网络/产品/公告/页面
- `context7`：查库和框架官方文档
- `deepwiki`：读 GitHub 仓库文档结构与摘要
- `openaiDeveloperDocs`：只在处理 OpenAI 产品时用

如果服务器实例不能恢复全部 MCP，最低可接受组合：

- `serena`
- `sequential-thinking`
- shell + `rg`

## 9. 推荐恢复的 skills 清单

当前会话中常用或建议保留的 skills：

- `openai-docs`
- `doc`
- `playwright`
- `playwright-interactive`
- `screenshot`
- `pdf`
- `frontend-skill`
- `gh-fix-ci`
- `gh-address-comments`
- `jupyter-notebook`
- `security-best-practices`
- `security-ownership-map`
- `security-threat-model`
- `sentry`

但本项目当前主线最相关的是：

- `doc`
- `playwright`
- `screenshot`

原因：

- `doc`：整理部署/交接/运行文档
- `playwright`：后续如果要测 Web 控制面或 OpenClaw 页面会用到
- `screenshot`：保留界面证据

## 10. 服务器侧继续工作时的默认流程

新实例接手后，默认按这个流程继续：

1. 激活项目：`/srv/projects/ashare-system-v2`
2. 读取交接文档
3. 假设 Windows 已通，先验收：
   - execution bridge health
   - account access / identity
   - receipt latest
4. 再验收：
   - execution intent queue -> claim -> receipt 闭环
5. 最后才回头补：
   - 飞书主动推送
   - 参数自然语言别名
   - 只读聚合美化

## 11. 建议直接使用的服务器命令口径

项目根目录：

```bash
cd /srv/projects/ashare-system-v2
```

主控服务状态：

```bash
systemctl status ashare-system-v2.service --no-pager
systemctl status openclaw-gateway.service --no-pager
```

关键只读验收：

```bash
curl -s http://127.0.0.1:8100/system/readiness
curl -s http://127.0.0.1:8100/system/reports/serving-latest-index
curl -s http://127.0.0.1:8100/system/reports/postclose-master
curl -s http://127.0.0.1:8100/system/reports/review-board
curl -s http://127.0.0.1:8100/system/monitor/execution-bridge-health/template
curl -s http://127.0.0.1:8100/system/execution/gateway/receipts/latest
```

## 12. 当前最重要的验收标准

服务器侧继续推进时，最低成功标准是：

- Linux 收到来自 `windows-vm-a` 的 execution bridge 上报
- `bridge_path` 正确
- precheck 不再出现纯假零资产
- 至少一条 execution intent 完整形成 `queue -> claim -> receipt` 闭环
- `postclose-master` / `review-board` 能读到真实执行桥摘要

## 13. 如果要进一步恢复“像当前会话一样”

建议额外做：

1. 确保服务器侧 Codex 也能读取项目根 `AGENTS.md`
2. 确保服务器侧有 Serena
3. 确保服务器侧 shell 可直接访问：
   - `ssh`
   - `curl`
   - `rg`
   - `systemctl`
4. 把共享目录相关约定一并保留

## 14. 结论

恢复工作环境不需要追求“完全复制当前会话”，关键是恢复：

- 规则
- 工具
- 主线边界
- 验收顺序
- 关键文档

做到这几点，服务器侧的新实例就能直接继续当前主线，而不会偏离方向。

## 附加说明：服务器侧已适配的 Codex 全局规则

服务器上的 Codex 全局规则文件已重写为服务器适配版：

- /home/yxz/.codex/rules/default.rules

交接备份位置：

- /srv/projects/ashare-system-v2/docs/codex-server-handoff_20260412/codex-global/default.rules

说明：

- 旧的本机 Windows/WSL 路径白名单已删除
- 当前只保留服务器常用命令的 allow 规则
- 主要覆盖：
  - /srv/projects/ashare-system-v2 下的常用 pytest
  - 本机 127.0.0.1:8100 的只读验收 curl
  - systemctl status
  - openclaw status --all
