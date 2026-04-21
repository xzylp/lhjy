# Windows K 线链路去旧 pandas 改造与压测说明

文档日期：2026-04-20  
项目目录：`C:\Users\yxzzh\Desktop\XTQ`

## 1. 改造背景

原 K 线链路虽然已经能对外提供 `/qmt/quote/kline`，但历史实现依赖 `xtquant` 旧包装层：

- `xtdata.get_market_data(...)`
- `xtdata.get_market_data_ex(...)`
- 旧 `pandas/numpy` 组装逻辑
- K 线在异常或兼容场景下会回退到旧 `Python 3.6` 子进程链路

已确认 `xtquant` 官方包中，真正向 QMT 取 K 线数据的底层调用是：

- `xtdata.get_client().get_market_data3(...)`

而 `pandas/numpy` 依赖来自 Python 包装层，不是 QMT 通讯本身必须依赖的部分。因此本次改造目标是：

- K 线链路彻底绕开旧 `pandas` 包装
- 取消 K 线回退到旧 `Python 3.6` 的特殊逻辑
- 统一到 `Python 3.11 + market_data3(v3)` 直取
- 保持外部 Linux 侧接口路径、鉴权方式、响应结构约定不变

## 2. 本次改造内容

### 2.1 K 线取数内核替换

已将 K 线取数从旧包装：

- `xtdata.get_market_data(...)`

改为直接调用：

- `xtdata.get_client().get_market_data3(..., "v3", ...)`

并在业务层自行完成裸数据解码和 bars 组装，不再依赖 `pandas`。

### 2.2 新增原始数据解码逻辑

在 K 线处理代码中新增了：

- 原始 `dict/list` 结构识别
- `time/stime/open/high/low/close/volume/amount` 序列解码
- `trade_time` 生成
- `preClose` 顺序回填
- `15m/60m` 聚合兼容

这样即使运行环境中缺少旧版 `pandas/numpy/pytz`，K 线链路仍可正常工作。

### 2.3 取消 K 线专属旧运行时回退

此前代理层存在：

- K 线优先回退到旧 `runtime/qmt_python36/pythonw.exe`

本次已移除这条特殊逻辑，K 线与 Tick 统一走：

- `Python 3.11`
- `quote bridge`
- `market_data3(v3)`

### 2.4 启停脚本重构

为避免重复拉起多个 quote bridge 或 manager 实例，新增并调整了启停逻辑：

- 启动前先清理旧监听端口和旧进程
- 启动后做 `health` 校验
- 停止时一次性释放：
  - `18889` 管理台
  - `18791` 对外代理
  - `18793` quote bridge

## 3. 涉及文件

主要改造文件如下：

- `scripts/qmt_rpc_py36.py`
- `ashare_system/windows_ops_proxy.py`
- `go_manager/proxy.go`
- `scripts/start_qmt_quote_bridge.ps1`
- `scripts/start_go_manager_hidden.ps1`
- `scripts/stop_xtq_stack.ps1`
- `stop_xtq_go_manager.bat`

桌面入口已同步：

- `C:\Users\yxzzh\Desktop\start_xtq_go_manager.bat`
- `C:\Users\yxzzh\Desktop\stop_xtq_go_manager.bat`

## 4. 当前运行口径

当前运行态对外端口如下：

- `18791`：Windows 对外代理口
- `18793`：本机 quote bridge
- `18889`：Go manager 控制台

当前启停要求：

- 启动：只允许单一 `xtq-go-manager` 实例
- 启动：只允许单一 `18793` quote bridge 实例
- 停止：`18791/18793/18889` 不应残留 `LISTENING`

## 5. 压测环境

压测时间：2026-04-20  
压测主机：当前 Windows XTQ 运行机  
运行时：

- K 线方法：`xtdata.get_client().get_market_data3(v3)`
- Python：`3.11.0`

压测标的与参数：

- 标的：`600000.SH`
- 路径：
  - `http://127.0.0.1:18793/qmt/quote/kline`
  - `http://127.0.0.1:18791/qmt/quote/kline`
- 参数：
  - `period=1m&count=20`
  - `period=1d&count=20`

说明：

- `18793` 为直连 quote bridge
- `18791` 为 Linux/外部实际使用的代理口
- 首包通常包含冷启动抖动，因此同时给出全样本平均值与去首包稳态均值

## 6. 压测结果

### 6.1 1m K 线

#### 18793 直连

- 样本数：20
- 平均：`47.34ms`
- P50：`17.44ms`
- P95：`60.86ms`
- 最小：`6.04ms`
- 最大：`530.58ms`
- 去首包稳态均值：`21.91ms`

样本明细：

```text
[530.58, 17.44, 42.13, 9.19, 6.2, 37.11, 20.06, 9.15, 8.57, 16.8, 38.46, 44.41, 25.66, 9.33, 6.04, 19.29, 60.86, 28.32, 7.2, 10.03]
```

#### 18791 代理

- 样本数：20
- 平均：`37.55ms`
- P50：`31.03ms`
- P95：`67.99ms`
- 最小：`8.99ms`
- 最大：`79.18ms`
- 去首包稳态均值：`35.97ms`

样本明细：

```text
[67.51, 8.99, 9.05, 52.96, 50.43, 17.05, 31.03, 79.18, 13.33, 11.71, 65.67, 43.03, 12.36, 60.1, 60.06, 9.36, 20.79, 67.99, 50.49, 19.89]
```

### 6.2 1d K 线

#### 18793 直连

- 样本数：10
- 平均：`22.95ms`
- P50：`12.87ms`
- P95：`53.00ms`
- 最小：`7.94ms`
- 最大：`53.00ms`
- 去首包稳态均值：`21.27ms`

样本明细：

```text
[38.07, 53.0, 10.86, 11.89, 9.53, 7.94, 42.43, 26.99, 15.93, 12.87]
```

#### 18791 代理

- 样本数：10
- 平均：`39.47ms`
- P50：`37.03ms`
- P95：`66.99ms`
- 最小：`10.41ms`
- 最大：`66.99ms`
- 去首包稳态均值：`39.46ms`

样本明细：

```text
[39.56, 55.39, 10.41, 19.14, 66.99, 27.93, 55.61, 59.34, 37.03, 23.34]
```

## 7. 压测结论

### 7.1 功能结论

本次压测中，四组 K 线请求全部确认：

- 返回成功
- 命中 `xtdata.get_client().get_market_data3(v3)`
- 运行在 `Python 3.11.0`
- 未再回退到旧 `pandas/py36` 链路

### 7.2 性能结论

当前稳态下：

- `18793 1m`：约 `21.91ms`
- `18791 1m`：约 `35.97ms`
- `18793 1d`：约 `21.27ms`
- `18791 1d`：约 `39.46ms`

说明：

- K 线链路已经从旧秒级路径降到几十毫秒量级
- `18791` 比 `18793` 多一层代理开销，但仍处于稳定可用区间
- 当前最慢样本主要来自冷启动首包，不代表稳态性能

## 8. 启停验证结论

已实际验证：

- 执行停止脚本后，`18791/18793/18889` 不再残留监听
- 启动后：
  - `18791` 只由单一 `xtq-go-manager` 监听
  - `18889` 只由单一 `xtq-go-manager` 监听
  - `18793` 只由单一 quote bridge 监听

即：

- 开得干净
- 停得干净

## 9. 当前建议

建议后续维持以下运行原则：

- 对外只使用 `18791`
- 内部 quote 数据统一走 `18793`
- 不再恢复 K 线到旧 `Python 3.6` 回退逻辑
- 若后续继续做高并发优化，可优先观察：
  - 首包冷启动
  - 高频连续请求时的 P95 抖动
  - 多标的批量请求下的尾延迟

## 10. 文档结论

本次 Windows K 线链路改造已经完成以下目标：

- 去除对旧 `pandas` 包装链路的依赖
- 统一到 `market_data3(v3)` 原生取数
- 保持外部接口不变
- 完成启停脚本清理和单实例控制
- 压测确认性能已进入稳定毫秒级
