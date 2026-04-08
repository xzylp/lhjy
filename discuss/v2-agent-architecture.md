# 归档说明

本文件原先记录过一版探索性的 8-agent 量化架构设想，已不再作为当前正式方案。

当前正式方案以以下文件为准：

- `task.md`
- `HANDOFF.md`
- `docs/technical-manual.md`
- `docs/subsystem-specs.md`
- `openclaw/workflow.final.json`
- `openclaw/team_registry.final.json`

当前正式拓扑：

```text
Feishu / WebChat
  -> OpenClaw Gateway
  -> main
  -> ashare
  -> ashare-runtime / ashare-research / ashare-strategy / ashare-risk / ashare-executor / ashare-audit
  -> ashare-system-v2
```

说明：

- `main` 是唯一前台入口。
- `ashare` 是量化中台，不再拆成旧的 `ashare-data / ashare-analyst / ashare-brain / ashare-monitor / ashare-reviewer` 体系。
- WSL 与 Windows 服务访问走直连，不再保留旧的中转式做法。
