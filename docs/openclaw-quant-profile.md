# OpenClaw Quant Profile

## Purpose

`quant` 是独立于通用 `main` 实例的量化专用 OpenClaw profile。

特征：
- 状态目录：`~/.openclaw-quant`
- 网关端口：`18890`
- 默认前台 agent：`ashare`
- 仅保留量化团队：
  - `ashare`
  - `ashare-runtime`
  - `ashare-research`
  - `ashare-strategy`
  - `ashare-risk`
  - `ashare-executor`
  - `ashare-audit`

默认关闭外部渠道绑定，避免与通用实例争用飞书/Discord。
默认只保留本地 `webchat -> ashare` 直连路由。

## Files

- 配置文件：`~/.openclaw-quant/openclaw.json`
- systemd unit：`~/.config/systemd/user/openclaw-gateway-quant.service`
- 启停脚本：`ashare-system-v2/scripts/openclaw_quant_service.sh`

## Manual Usage

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
./scripts/openclaw_quant_service.sh start
./scripts/openclaw_quant_service.sh status
./scripts/openclaw_quant_service.sh stop
```

## Direct Agent Usage

量化实例不经过 `main`，直接与 `ashare` 交互：

```bash
openclaw --profile quant agent --agent ashare --message "检查当前量化服务健康，只返回简短结论。"
```

## Notes

- `quant` 与默认实例会话、记忆、workspace、sessions 彼此隔离。
- 当前未启用飞书；如需量化实例单独接飞书，建议配置独立渠道或独立 app，不要与通用实例共用同一入口。
- `proxy on/off` 已同步 systemd 环境；如果 `openclaw-gateway-quant.service` 正在运行，会跟随当前动态代理地址切换。
