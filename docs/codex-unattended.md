# Codex 无人值守执行说明

适用场景：

- 你已经把任务拆成顺序队列
- 希望 Codex 连续执行，不盯着 TUI
- 希望保留上一任务上下文继续干

这套方案基于 `codex exec`，不是交互式 `codex` TUI。

## 1. 文件说明

- `scripts/run_codex_tasks.sh`
  - 顺序读取任务文件
  - 默认第一条新建会话，后续任务走 `codex exec resume --last`
  - 每条任务都保存 `jsonl` 日志和最后回复
- `scripts/start_codex_tmux.sh`
  - 用 `tmux` 后台启动任务队列
- `scripts/stop_codex_tmux.sh`
  - 停止后台 `tmux` 会话
- `discuss/codex-quant-task-queue.txt`
  - 面向当前量化改造的示例任务队列

## 2. 任务文件格式

任务文件支持多行任务，使用一行 `---` 分隔：

```text
先做第一件事
补测试并记录结果
---
继续上一会话
做第二件事
```

规则：

- 空行允许存在
- 任务块开始前的 `#` 注释会被忽略
- 每个任务块会被单独保存到日志目录，便于复盘

## 3. 前台直接跑

在仓库根目录执行：

```bash
bash scripts/run_codex_tasks.sh \
  --task-file discuss/codex-quant-task-queue.txt
```

常用参数：

- `--continue-on-error`
  - 单个任务失败后继续下一个任务
- `--fresh-each-task`
  - 每个任务都新建会话，不续接上一条上下文
- `--model gpt-5.4`
  - 覆盖模型
- `--profile your-profile`
  - 使用本地 Codex profile
- `--search`
  - 允许任务里直接做联网搜索
- `--sandbox read-only|workspace-write|danger-full-access`
  - 默认 `workspace-write`
- `--danger-full-access`
  - 彻底跳过审批和沙箱，只建议在独立环境里使用

## 4. 后台持续跑

建议用 `tmux`：

```bash
bash scripts/start_codex_tmux.sh \
  --task-file discuss/codex-quant-task-queue.txt
```

查看：

```bash
tmux attach -t codex-queue
```

停止：

```bash
bash scripts/stop_codex_tmux.sh
```

如果你想换会话名：

```bash
bash scripts/start_codex_tmux.sh \
  --task-file discuss/codex-quant-task-queue.txt \
  --session-name quant-m3
```

## 5. 日志与产物

每次运行都会生成一个目录：

```text
logs/codex/YYYYMMDD-HHMMSS/
```

里面会有：

- `runner.log`
  - 队列执行总日志
- `summary.tsv`
  - 每条任务的状态、退出码、日志文件位置
- `task-001.jsonl`
  - Codex 事件流
- `task-001.last.txt`
  - 该任务最后一条回复
- `tasks/task-001.md`
  - 实际发给 Codex 的任务文本

## 6. 关键约束

- 无人值守时，脚本固定使用 `codex -a never`
- 如果任务涉及联网、安装依赖、危险命令，仍可能因为环境约束失败
- 当前 `ashare-system-v2` 目录不是 Git 仓库，脚本已自动带 `--skip-git-repo-check`
- 续跑模式依赖“上一条就是刚执行的最近会话”，所以建议一个队列对应一个终端或一个 `tmux` 会话

## 7. 推荐做法

适合无人值守的任务：

- 已拆解、边界清楚的代码改造
- 补测试、补文档、补脚本
- 固定顺序的多阶段实现

不适合直接无人值守的任务：

- 需要频繁人工拍板的架构争议
- 涉及生产环境危险操作
- 强依赖外部网站登录态或验证码
