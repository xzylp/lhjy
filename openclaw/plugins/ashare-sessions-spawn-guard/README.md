# ashare-sessions-spawn-guard

用于修复 OpenClaw `2026.4.10` 下 `sessions_spawn` schema 同时暴露 ACP 与 subagent 字段，导致 `runtime=subagent` 仍可能被模型或运行时注入 `streamTo` / `resumeSessionId`，从而直接报错的问题。

## 当前保护范围

- 仅拦截 `before_tool_call`
- 仅处理 `toolName="sessions_spawn"`
- 仅在 `runtime != "acp"` 或未显式声明 `runtime` 时生效
- 当前只剥离已被 OpenClaw 工具实现明确判定为 subagent 非法的字段：
  - `streamTo`
  - `resumeSessionId`

## 为什么不删更多字段

根据本地官方文档与 `openclaw-tools-Co0S582U.js` 实现：

- `cleanup`
- `thread`
- `sandbox`
- `runTimeoutSeconds`
- `timeoutSeconds`
- `lightContext`
- `attachments`
- `attachAs`

这些并不都是 subagent 非法字段，直接删除反而可能改变业务语义，因此本插件保持最小修复面，只处理当前已证实会阻断子代理启动的键。

## 建议安装方式

优先使用链接安装，保留共享仓库源码作为单一事实来源：

```bash
openclaw plugins install -l "/srv/projects/ashare-system-v2/openclaw/plugins/ashare-sessions-spawn-guard"
openclaw plugins inspect ashare-sessions-spawn-guard
openclaw plugins list --enabled
```

## 最小验证

安装后重新执行：

```bash
openclaw agent --agent ashare --message "请委派 ashare-runtime 做最小健康探测，只返回健康结论，不要行情总结。" --json --timeout 120
```

然后检查对应 session transcript，确认 `sessions_spawn` 不再携带 `streamTo` 或 `resumeSessionId`，再继续真实行情联调。
