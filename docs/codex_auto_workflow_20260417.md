# Codex 自动推进与 Resume 兜底说明

## 1. 目标

本说明用于解决两个问题：

1. 让 Codex 进入项目后，按照 `task.md` 与主线文档持续推进，不要每做一项就停下来问“要不要继续”。
2. 当 API key 或供应商切换后，即使 `resume` 不稳定，也能自动回到项目主线继续干活。

## 2. 当前采用的方案

### 2.1 自动推进原则

Codex 的连续工作能力不依赖“远端线程一定能 resume 成功”，而依赖以下四件事：

- 项目内有持续更新的 `task.md`
- 主线文档明确当前目标和优先级
- 启动提示词明确要求“连续推进，不频繁确认”
- 本地开启 response storage，保留本地会话记录

### 2.2 Resume 兜底原则

`codex auto` 的行为定义为：

1. 若存在固定会话 ID，则优先尝试恢复该会话
2. 若无固定会话 ID，则尝试 `resume --last`
3. 若最近会话不存在、resume 失败、供应商切换导致上下文丢失，则自动新开一轮
4. 无论是 resume 还是新开，都会强制读取以下存档：
   - `task.md`
   - `docs/agent_autonomy_factor_library_plan_20260417.md`
   - 本说明文档

这样即使远端线程断了，项目连续性仍然由本地文档维持。

补充说明：

- `codex resume --last` 不能稳定附带一大段启动 prompt。
- `codex resume --last` 只会取“当前记录里的最后一个会话”，如果中间 fallback 新开过一轮，会把真正想续的老线程顶掉。
- 因此 `codex auto` 现在支持“固定会话 ID 文件”：
  - `~/.codex/prompts/ashare_auto_session_id.txt`
  - 若文件中存在合法 UUID，优先恢复该会话
- 当存在固定 UUID 时，`codex auto` 会执行：
  - `codex resume -C <project> <UUID> "<主线提示词>"`
  - 即恢复旧会话的同时，把主线提示词作为新一轮输入直接送进去
- 若在 `resume` 命令后直接拼接长文本，CLI 会优先把该文本当作 `SESSION_ID` 解析，从而报：
  - `No saved session found with ID ...`
- 因此 `codex auto` 的正确策略是：
  - 固定 ID 恢复成功：直接进入旧会话
  - 固定 ID 不可用：再尝试 `--last`
  - 恢复失败：再新开会话并注入主线 prompt

## 3. 当前主线文档

`codex auto` 默认读取以下文件作为项目存档：

- [task.md](/srv/projects/ashare-system-v2/task.md)
- [agent_autonomy_factor_library_plan_20260417.md](/srv/projects/ashare-system-v2/docs/agent_autonomy_factor_library_plan_20260417.md)
- [codex_auto_workflow_20260417.md](/srv/projects/ashare-system-v2/docs/codex_auto_workflow_20260417.md)

## 4. codex auto 的默认工作规则

进入项目后，默认遵守以下规则：

- 从 `task.md` 中未完成项继续
- 优先推进 P0，再推进 P1，再推进 P2
- 不要每做一项就停下来确认
- 若没有真实阻断，则持续向下推进
- 每完成一个小里程碑，都要更新 `task.md`
- 不要偏离当前主线去做无关优化
- 做完实现、验证、文档更新后，再进入下一项

## 5. Resume 找不到的真实原因

切换 API key 或供应商后，`resume` 不稳定通常来自两层原因：

### 5.1 本地层

若本地配置里启用了：

```toml
disable_response_storage = true
```

则本地响应/会话记录不会正常保留，`resume` 的稳定性会显著下降。

当前已改为：

```toml
disable_response_storage = false
```

### 5.2 供应商层

当以下任一项发生变化时，旧会话上下文可能无法继续复用：

- `base_url`
- `model_provider`
- 认证方式
- 供应商侧会话存储策略

因此项目连续性不能只依赖远端线程，必须依赖本地文档与任务板。

## 6. 使用方法

### 6.1 默认自动推进

```bash
codex auto
```

行为：

- 优先尝试恢复固定会话 ID
- 若未固定，则恢复最近一次会话
- 失败则自动新开
- 自动读取项目存档并进入主线连续推进模式

### 6.1.1 指定要恢复的会话 ID

```bash
codex auto 019d8032-3098-7732-9b27-4b8c1b431946
```

行为：

- 将该 UUID 写入 `~/.codex/prompts/ashare_auto_session_id.txt`
- 本次优先恢复该线程
- 后续 `codex auto` 默认也优先恢复该线程

### 6.2 带补充要求启动

```bash
codex auto 先做 P0-A1 到 P0-A3，不要碰控制台样式
```

行为：

- 在默认自动推进规则之上，追加该条补充要求

### 6.3 原始 Codex 命令

若不想走自动包装，仍可使用原命令：

```bash
codex_safe ...
```

或直接：

```bash
/home/yxz/.npm-global/bin/codex ...
```

## 7. 判断标准

若 `codex auto` 行为正常，应满足：

- 能进入 `/srv/projects/ashare-system-v2`
- 能自动读取 `task.md` 与主线文档
- 能从未完成项继续推进
- 不频繁询问“是否继续”
- `resume` 失败时能自动新开并继续主线

## 8. 结论

本方案的关键不是“保证远端线程永不断”，而是把项目连续性从远端线程收口到：

- `task.md`
- 主线文档
- 本地 prompt 模板
- 本地 response storage

这样切换供应商、切换 key、甚至 resume 失败，都不会中断项目主线推进。
