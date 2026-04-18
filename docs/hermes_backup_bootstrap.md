# Hermes 备用控制面启动手册

## 目标

把 `ashare-backup` profile 变成一个可值班的 Hermes 备用控制面，承担：

- 盘前准备
- 盘中巡检
- 持仓复核
- 盘后学习
- 夜间沙盘

## 已完成的前置条件

- `ashare-backup` profile 已创建
- 默认模型已固定为 `minimax-cn / MiniMax-M2.7`
- 项目级规则已落在 `/.hermes.md`
- 角色与 cron 模板已落在 `/hermes/prompts/`

## 一次性装配 cron 任务

在仓库根目录执行：

```bash
bash /srv/projects/ashare-system-v2/scripts/bootstrap_hermes_ashare_backup.sh
```

默认写入以下任务：

- `ashare-preopen-readiness`
- `ashare-intraday-watch-am`
- `ashare-intraday-watch-pm`
- `ashare-position-watch-am`
- `ashare-position-watch-pm`
- `ashare-postclose-learning`
- `ashare-nightly-sandbox`

默认投递目标是 `local`。若后续要把 cron 结果发往 Feishu，可在执行前临时指定：

```bash
HERMES_CRON_DELIVER=feishu bash /srv/projects/ashare-system-v2/scripts/bootstrap_hermes_ashare_backup.sh
```

## 启动 gateway

Hermes 的 cron 依赖 gateway tick。没有 gateway，任务不会自动触发。

```bash
/home/yxz/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup gateway install
/home/yxz/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup gateway start
/home/yxz/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup gateway status
```

WSL 或手工联调时，也可以直接前台运行：

```bash
/home/yxz/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup gateway run
```

## Feishu 配置建议

按 Hermes 官方文档，优先使用 WebSocket 模式。在 `~/.hermes/profiles/ashare-backup/.env` 中至少准备：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=secret_xxx
FEISHU_DOMAIN=feishu
FEISHU_CONNECTION_MODE=websocket
FEISHU_ALLOWED_USERS=ou_xxx
FEISHU_HOME_CHANNEL=oc_xxx
```

然后执行：

```bash
/home/yxz/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup gateway setup
```

## 当前建议

- 在没有最终 Feishu 参数前，先用 `local` 投递把 cron 跑通。
- Feishu 接好后，再把盘中巡检、盘后学习、夜间沙盘结果切到 `feishu`。
- 若后续要与默认 profile 并行运行，不要复用同一 bot token。
