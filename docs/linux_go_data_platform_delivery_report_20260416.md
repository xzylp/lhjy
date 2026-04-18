# ashare-system-v2 Linux 侧多 Agent 并发数据面交付文档

## 1. 交付概览
本项目已完成 Linux 侧统一 Go 并发数据平台的构建与接线。该平台作为 Python 主程序与 Windows 数据面之间的“智能中转站”，解决了多 Agent 并发拉取数据时的阻塞、重复请求及系统雪崩风险。

## 2. 核心组件
### 2.1 Go 并发数据平台 (`go_data_platform`)
- **位置**：`/srv/projects/ashare-system-v2/go_data_platform`
- **监听端口**：`127.0.0.1:18793`
- **技术特性**：
    - **四层 Lane 调度**：实现了 `quote` (行情), `account_fast` (账户), `trade_slow` (交易), `internal` (内部服务) 流量硬隔离。
    - **Singleflight 请求合并**：毫秒级重复请求仅向 Windows 发起一次调用，极大减轻带宽压力。
    - **高性能短时缓存**：对行情和资产数据提供可配置的 TTL 缓存（默认 1000ms），提升多 Agent 研究效率。
    - **可观测性**：集成 Prometheus 指标接口 (`/metrics`)。

### 2.2 Python 适配层
- **适配器**：新增 `GoPlatformMarketDataAdapter` 与 `GoPlatformExecutionAdapter`。
- **客户端**：新增 `src/ashare_system/infra/go_client.py`，负责注入优先级 Header。

## 3. 部署与运维
### 3.1 服务管理
Go 平台已作为 `systemd` 服务安装：
- **启动/重启**：`sudo systemctl restart ashare-go-data-platform`
- **查看状态**：`systemctl status ashare-go-data-platform`
- **日志查看**：`journalctl -u ashare-go-data-platform -f`

### 3.2 关键配置 (`.env`)
```bash
ASHARE_MARKET_MODE=go_platform      # 启用 Go 行情适配器
ASHARE_EXECUTION_MODE=go_platform   # 启用 Go 执行适配器
ASHARE_GO_PLATFORM_ENABLED=true     # 开启平台支持
ASHARE_GO_PLATFORM_BASE_URL=http://127.0.0.1:18793
```

## 4. 性能与验证结果 (2026-04-16)
- **连通性**：通过 `curl` 验证 18793 端口可正常获取 QMT 实盘资产。
- **并发表现**：模拟 10 个 Agent 并发请求同一标的行情，Windows 侧实收请求仅为 1 次 (Singleflight 生效)。
- **缓存验证**：二次请求相同接口返回 `X-Cache-Hit: true`，响应时间从 ~500ms 降低至 <1ms。

## 5. 后续扩展建议
- **监控集成**：建议将 Prometheus 指标接入 Grafana，实现 Agent 请求热力图可视化。
- **持久化缓存**：针对代码表等冷数据，可考虑引入更长效的本地持久化。
- **异常熔断**：进一步强化对 Windows 网关响应超时的动态熔断阈值调整。
