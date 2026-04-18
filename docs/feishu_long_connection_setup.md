# 飞书长连接接线说明

最近更新：2026-04-13

## 结论

按飞书官方服务端 SDK 文档，事件与回调都支持“使用长连接接收”模式。

这条模式的核心前提是：

- 服务器能够访问公网
- 不需要提供公网 IP
- 不需要提供事件/回调公网 URL

## 官方依据

- 服务端 SDK 概述：SDK 支持“基于长连接的事件回调”
- Python SDK 开发前准备：安装命令是 `pip install lark-oapi -U`
- Python SDK 处理事件：长连接模式“无需提供公网 IP 或域名”
- Python SDK 处理回调：`url.preview.get` 支持通过长连接接收

## 仓库内已落地的长连接方案

运行命令：

```bash
/srv/projects/ashare-system-v2/.venv/bin/python -m ashare_system.run feishu-longconn
```

包装脚本：

```bash
./scripts/ashare_feishu_longconn_service.sh
```

systemd 管理脚本：

```bash
./scripts/ashare_feishu_longconn_ctl.sh --user status
./scripts/ashare_feishu_longconn_ctl.sh --user restart
./scripts/ashare_feishu_longconn_ctl.sh --user verify
./scripts/ashare_feishu_longconn_ctl.sh --user logs 100
```

systemd 安装脚本：

```bash
./scripts/install_feishu_longconn_service.sh --user
```

仓库内 unit 模板：

```bash
./deploy/systemd/ashare-feishu-longconn.service
```

## 处理链路

```text
飞书开放平台
-> 官方 SDK 长连接 WebSocket
-> ashare_system.feishu_longconn
-> http://127.0.0.1:8100/system/feishu/events
-> 现有 supervision ack / url.preview.get 处理逻辑
```

## 环境变量

```env
ASHARE_FEISHU_APP_ID=你的飞书应用 App ID
ASHARE_FEISHU_APP_SECRET=你的飞书应用 App Secret
ASHARE_FEISHU_CONTROL_PLANE_BASE_URL=http://127.0.0.1:8100
```

## 飞书后台选择

事件订阅：

- 选择 `使用长连接接收事件`
- 订阅 `im.message.receive_v1`

回调订阅：

- 选择 `使用长连接接收回调`
- 订阅 `url.preview.get`

## 当前建议

当前建议优先切长连接，把外网回调 URL 降为备用通道。

正式运维时建议同时查看：

- `GET /system/feishu/longconn/status`
- `GET /system/operations/components`
- `systemctl --user status ashare-feishu-longconn.service --no-pager`
- `journalctl --user -u ashare-feishu-longconn.service -n 100 --no-pager`

## 当前推荐运维口径

优先使用用户级 systemd：

```bash
bash scripts/install_feishu_longconn_service.sh --user
bash scripts/ashare_feishu_longconn_ctl.sh --user enable
bash scripts/ashare_feishu_longconn_ctl.sh --user start
bash scripts/ashare_feishu_longconn_ctl.sh --user verify
```

说明：

- `start/restart` 现在会等待 `/system/feishu/longconn/status` 回到 `connected + is_fresh=true`
- `verify` 可单独用于恢复压测后的健康确认
- 如果要保证“无人登录后也自动拉起”，还需要补 `sudo loginctl enable-linger yxz`
