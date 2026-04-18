# 飞书事件订阅接线说明

最近更新：2026-04-13

## 目标

把飞书群里的催办回执消息自动接到控制面，让消息如：

- `研究已收到催办，转入处理`
- `风控已处理`
- `审计已确认`

直接回写到：

- `POST /system/feishu/events`
- 再由程序内部转到 `POST /system/agents/supervision/ack`

## 长连接优先口径

如果飞书后台选择的是“使用长连接接收事件 / 回调”，则不需要公网回调 URL。

按飞书官方 Python SDK 文档：

- 只要运行环境能够访问公网即可
- 不需要提供公网 IP 或域名
- `im.message.receive_v1` 与 `url.preview.get` 都支持长连接

本仓库已新增长连接 worker，详见：

- `docs/feishu_long_connection_setup.md`
- 运行命令：`/srv/projects/ashare-system-v2/.venv/bin/python -m ashare_system.run feishu-longconn`

## 程序侧已具备的接口

- 回调入口：`POST /system/feishu/events`
- 配置查看：`GET /system/feishu/events/config`
- 直接文本回写：`POST /system/feishu/supervision/ack`

## 环境变量

建议至少配置：

```env
ASHARE_PUBLIC_BASE_URL=https://你的可公网访问域名
ASHARE_FEISHU_VERIFICATION_TOKEN=你在飞书后台设置的 verification token
```

说明：

- `ASHARE_PUBLIC_BASE_URL` 用于生成飞书后台应填写的回调地址。
- 若不配置，程序会退回当前请求的 `base_url`，这通常只适合本地调试，不适合正式回调。
- `ASHARE_FEISHU_VERIFICATION_TOKEN` 为可选但建议开启；启用后，程序会校验飞书事件包中的 `token`。

### 带路径前缀的正式入口写法

如果控制面挂在反向代理子路径后，不要只写域名根地址，要把前缀一并写入 `ASHARE_PUBLIC_BASE_URL`。

例如当前候选入口：

```env
ASHARE_PUBLIC_BASE_URL=https://yxzmini.com/pach
```

此时程序生成给飞书后台的回调地址会是：

```text
https://yxzmini.com/pach/system/feishu/events
```

这要求你的反向代理把 `/pach/system/feishu/events` 正确转发到控制面实际监听的 `http://127.0.0.1:8100/system/feishu/events`。

## 飞书后台建议填写

以控制面 `GET /system/feishu/events/config` 返回为准，重点字段：

- `callback_url`
- `expected_event_types`
- `verification_token_configured`

当前程序支持的事件类型：

- `im.message.receive_v1`

如后续要启用 `url.preview.get`：

- 需要先到飞书开放平台对应应用里开启“链接预览”能力。
- 再在事件订阅里补 `url.preview.get`。
- 还要确保飞书消息中的 URL 命中你在后台配置的预览规则。
- 当前仓库已支持 `url.preview.get` 入站，并返回 `inline` 预览摘要；现阶段除链接预览外，正式使用的消息事件仍以 `im.message.receive_v1` 为主。

## 当前入站行为

### 1. URL 校验

飞书发送 `url_verification` 时，程序会直接返回 challenge。

### 2. 消息事件

飞书发送 `im.message.receive_v1` 时，程序会尝试从消息内容提取文本，并解析以下模式：

- `研究已收到催办`
- `策略已处理`
- `风控已确认`
- `审计转入处理`
- `执行已接手`

解析成功后，会自动回写对应 agent 的 supervision ack。

## 调试方式

### 查看程序建议配置

```bash
curl -sS "http://127.0.0.1:8100/system/feishu/events/config"
```

### 本地模拟 URL 校验

```bash
curl -sS -X POST "http://127.0.0.1:8100/system/feishu/events" \
  -H "Content-Type: application/json" \
  -d '{"type":"url_verification","challenge":"local-challenge"}'
```

### 本地模拟消息事件

```bash
curl -sS -X POST "http://127.0.0.1:8100/system/feishu/events" \
  -H "Content-Type: application/json" \
  -d '{
    "schema":"2.0",
    "header":{"event_type":"im.message.receive_v1"},
    "event":{
      "sender":{"sender_id":{"open_id":"ou_local_test"}},
      "message":{
        "chat_id":"oc_test_chat",
        "content":"{\"text\":\"研究已收到催办，转入处理\"}"
      }
    }
  }'
```

## 现阶段边界

- 当前只做了文本型 supervision ack 回写，不处理复杂卡片交互。
- 当前只消费和监督确认有关的消息，其他普通群消息会忽略，不会强行入库。
- 若后续需要更细粒度的人机交互，可继续扩展成卡片按钮或 slash command 风格。
- 若采用长连接模式，则公网 URL 相关配置可降级为备用方案，不再是主阻断。
