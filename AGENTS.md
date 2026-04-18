# AGENTS 全局与项目规范

## 适用与优先级

- 本文件同时包含`全局规范`与`项目规范`。
- 冲突优先级：
  1. 用户当前回合直接指令
  2. 更近目录的 `AGENTS.md`
  3. 本文件中的`全局规范`
  4. 本文件中的`项目规范`
- 执行时优先阅读本节与`全局规范`，再按需查看`项目规范`。

## 全局规范

### 语言规范

- 只允许使用中文回答，所有思考、分析、解释和回答均使用中文。
- 优先使用中文术语、中文表达方式和中文命名说明。
- 生成的代码注释、设计说明和文档默认使用中文。
- 分析问题和组织方案时，默认使用中文思维展开。

### 基本原则

1. 质量第一：代码质量与系统安全不可妥协。
2. 思考先行：编码前必须先完成充分分析与规划。
3. 工具优先：优先使用稳定、验证过的最佳工具链。
4. 透明记录：关键决策、关键变更与关键风险必须可追溯。
5. 持续改进：从每次执行中沉淀经验并持续优化。
6. 结果导向：以目标达成和可验证结果作为最终评判标准。

### 质量标准

#### 工程原则

- 遵循 SOLID、DRY、关注点分离、YAGNI。
- 保持清晰命名、合理抽象和一致结构。
- 在关键流程、核心逻辑和重点难点处补充必要的中文注释。
- 删除无用代码；功能修改完成后，不保留无意义的旧兼容代码。
- 禁止使用 MVP、占位实现、TODO 代替完整交付；提交内容必须完整且可运行。

#### 性能标准

- 设计与实现时考虑时间复杂度和空间复杂度。
- 关注内存占用、磁盘 IO 与网络 IO 的资源开销。
- 显式处理异常情况、边界条件和回退路径。

#### 测试要求

- 默认采用可测试设计，优先补充单元测试覆盖。
- 执行后台单元测试时单次任务最长不得超过 60 秒，避免卡死。
- 在可行范围内执行静态检查、格式化、自动化测试和集成验证。

### 工具使用指南

#### Sequential Thinking

- 适用于复杂问题拆解、多步规划、方案评估。
- 输出以可执行步骤为目标，不暴露冗长中间推理。
- 标准拆解为 6 到 10 步；若受限可降级为 3 到 5 步核心流程。

#### Context7

- 适用于 SDK、API、框架官方文档查询。
- 采用 `resolve-library-id -> get-library-docs -> 抽取关键段落` 流程。
- 输出需包含库标识、版本、关键结论、引用链接和定位信息。
- 若不可用，降级为 Exa 的代码上下文能力或保守本地结论。

#### Exa

- 适用于最新网页信息、官方入口、新闻公告与漏洞验证。
- 搜索关键词控制在 12 个以内，优先官方来源和高时效来源。
- 输出需包含标题、简述、URL 和抓取时间，并过滤低质量站点。

#### mcp-deepwiki

- 适用于深度文档语义检索、技术概念说明和多源知识聚合。
- 输出需包含关键要点、来源与时间戳，避免直接搬运原文。
- 若不可用，按 `mcp-deepwiki -> Context7 -> Exa` 顺序降级。

#### Serena

- 适用于符号级检索、引用分析和语义化重构。
- 常用能力包括 `find_symbol`、`find_referencing_symbols`、`get_symbols_overview`、`insert_before_symbol`、`insert_after_symbol`、`replace_symbol_body`、`search_for_pattern`、`find_file`、`read_file`。
- 进行重构时优先使用符号级工具，避免无边界的文本替换。
- 输出变更时记录文件路径、位置和修改说明。
- 若不可用，降级为 `rg` 等文本搜索。

#### AGENTS.md 编写约定

- `AGENTS.md` 只记录长期稳定、可执行、与仓库或目录强相关的约束，不写一次性任务要求和临时排障结论。
- 规则尽量写成“触发条件 + 执行动作 + 失败降级”的形式，避免空泛口号和不可验证表述。
- 根级 `AGENTS.md` 负责全局原则；子目录 `AGENTS.md` 只补充更具体的局部约束，避免重复搬运父级大段内容。
- 涉及命令、路径、环境变量和测试入口时，优先给出可直接执行的示例，减少解释性废话。
- 若规范依赖具体工具能力，需同时写明首选工具、适用范围和降级方案，避免执行时出现“知道要做什么，但不知道用什么做”。

#### MCP 与 Skills 协作规范

- 外部系统、在线知识库、远程服务接入优先走 MCP；可复用的本地工作流、模板、脚本封装优先走 skills。
- 复杂多步任务优先使用 `Sequential Thinking`；官方或版本敏感文档优先 `openaiDeveloperDocs` 或 `Context7`；最新网页信息优先 `Exa`；多源知识聚合优先 `deepwiki`；大仓库语义检索与重构优先 `Serena`。
- 涉及 OpenAI 产品、API、模型、配置时，优先使用 `openaiDeveloperDocs` MCP 或 `openai-docs` skill；只有官方资料不足时，才补充其他来源。
- skills 设计保持单一职责、小而专注、按需触发；能通过引用脚本、模板、示例或资源文件复用的，不在技能主体里重复粘贴长说明。
- skills 采用渐进披露：主说明只写触发条件、适用范围、输入输出和依赖；细节实现放在引用文件中，避免一次性加载过多上下文。
- MCP 配置优先项目作用域；若项目作用域受路径、同名文件或共享策略限制，再退回用户级配置，并记录原因与影响。

#### 推荐 MCP 基线

- `openaiDeveloperDocs`：查询 OpenAI 官方开发文档、配置说明与最新能力。
- `context7`：查询第三方库、框架和 SDK 的官方文档与 API 用法。
- `deepwiki`：聚合仓库文档、技术说明和多源知识摘要。
- `exa`：执行最新网页搜索、官方入口确认和时效性验证。
- `sequential-thinking`：处理复杂问题拆解、执行计划和多步分析。
- `serena`：处理大仓库语义检索、引用分析和符号级编辑。

#### 推荐 Skills 基线

- `openai-docs`：处理 OpenAI API、模型、Agents SDK 与官方配置问题。
- `doc`：生成或整理技术文档、说明文档、设计稿和交付文档。
- `playwright`：处理浏览器自动化、端到端验证、页面检查和 UI 回归。
- `playwright-interactive`：处理需要交互式浏览器探索、复杂页面操作和调试的任务。
- `screenshot`：处理页面截图、界面取证和视觉结果留档。
- `pdf`：处理 PDF 资料提取、分析、摘要和结构化引用。
- `frontend-skill`：处理前端页面实现、界面重构和交互体验优化。
- `gh-fix-ci`：处理 GitHub Actions 失败、CI 排障和自动化流水线修复。
- `gh-address-comments`：处理 PR 评论整改、审查意见落实和回帖前核对。
- `jupyter-notebook`：处理 Notebook 分析、实验性数据探索和原型验证。
- `security-best-practices`：处理通用安全基线、敏感配置治理和安全设计审查。
- `security-ownership-map`：处理系统资产、责任边界和安全归属梳理。
- `security-threat-model`：处理威胁建模、攻击面分析和风险登记。
- `sentry`：处理线上异常分析、报错聚合和告警排查。

#### Skills 选用顺序

- 涉及官方产品或 SDK 文档，先选 `openai-docs`。
- 涉及文档生成或资料整理，先选 `doc`、`pdf`。
- 涉及页面验证、浏览器排障或 UI 回归，先选 `playwright`；需要人工探索式操作时再用 `playwright-interactive`。
- 涉及前端实现与界面改造，先选 `frontend-skill`，必要时配合 `playwright` 与 `screenshot` 做验证。
- 涉及 CI、PR 流程或代码审查整改，先选 `gh-fix-ci`、`gh-address-comments`。
- 涉及安全设计或合规检查，按 `security-best-practices -> security-ownership-map -> security-threat-model` 逐层深入。

### 命令执行标准

- 始终优先使用双引号包裹文件路径。
- 优先使用正斜杠 `/` 作为路径分隔符，兼顾跨平台兼容性。
- 内容搜索优先使用 `rg`，其次才是 `grep`。
- 优先使用专用读写编辑工具，其次才是通用系统命令。
- 能批量并行处理时，优先使用批量工具调用提高效率。

### 危险操作确认机制

#### 高风险操作清单

执行以下操作前必须获得明确确认：

- 删除文件、目录或执行大范围文件改写。
- 执行 `git commit`、`git push`、`git reset --hard` 等 Git 高风险命令。
- 修改系统配置、环境变量、权限或服务级设置。
- 删除数据、变更数据库结构或执行批量更新。
- 向外部发送敏感数据或调用生产环境 API。
- 全局安装、卸载或升级核心依赖。

#### 确认模板

```text
⚠️ 危险操作检测！
操作类型：[具体操作]
影响范围：[详细说明]
风险评估：[潜在后果]

请确认是否继续？[需要明确的“是”“确认”“继续”]
```

### 关键检查点

#### 任务开始

- 调用 Serena 的 `read_memory`，回显关键约束，例如代码规范、特定函数实现要求。
- 根据任务特征选择适配策略。
- 确认工具可用性及降级方案。

#### 编码前

- 完成 Sequential Thinking 分析。
- 使用 Serena 等工具理解现有代码。
- 制定实施计划、验证方案和质量标准。

#### 实施中

- 严格遵循已选质量标准。
- 记录重要决策、关键风险和变更理由。
- 及时处理异常、边界情况与兼容问题。
- 若涉及重构，优先使用 Serena 的符号级能力，例如 `rename_symbol`。

#### 完成后

- 验证功能正确性和代码质量。
- 同步更新相关测试和文档。
- 总结经验、关键约束和最佳实践，并调用 Serena 的 `write_memory` 写入。

### 终端输出风格指南

#### 语言与语气

- 保持专业、自然、直接，避免生硬书面语。
- 可适度使用 emoji 强化视觉引导，但不要喧宾夺主。
- 开篇先用一句话概括核心结论，复杂问题优先给出主线判断。

#### 内容组织

- 使用标题和小节组织长内容。
- 长段落拆成短句或短列表，每点只表达一个核心意思。
- 多步骤任务使用有序列表，并列信息使用无序列表。
- 通过空行或分隔线提升可读性，避免终端内容拥挤。
- 除确有必要外，避免使用复杂表格。

#### 技术内容展示

- 多行代码、配置、日志必须使用带语言标识的 Markdown 代码块。
- 示例聚焦核心逻辑，省略无关样板。
- 变更对比可使用 `+` / `-` 辅助标记。
- 必要时补充行号或定位信息帮助排查。

#### 交互体验

- 对较长任务提供及时的阶段性反馈。
- 对关键操作显示当前状态或进展。
- 出错时明确说明原因，并给出可操作的下一步建议。

#### 输出收尾

- 复杂任务结束时附简短总结，重申核心结果。
- 给出下一步建议或可继续执行的动作。

## 项目规范

## Project Structure & Module Organization
This workspace contains three related Python projects for QMT-based A-share trading.

- **ashare-system/** - Main application (Python 3.11+)
  - Business code: `ashare-system/src/ashare_system/`
  - Tests: `ashare-system/tests/` (filenames match `test_*.py`)
  - PowerShell automation: `ashare-system/scripts/`
  - Agent workflow notes: `ashare-system/openclaw/`
- **xtquantservice/** - FastAPI wrapper around bundled `xtquant/` SDK
- **XtQuant/** - SDK examples, research notes, supporting assets

## Build, Test, and Development Commands

### ashare-system
```bash
# Install in editable mode
cd ashare-system && python -m pip install -e .

# Run all tests
cd ashare-system && python -m unittest discover -s tests

# Run specific test file
cd ashare-system && python -m unittest tests.test_apps

# Run single test method
cd ashare-system && python -m unittest tests.test_apps.RuntimeAppTests.test_runtime_generates_buy_decision

# Run main application
cd ashare-system && python -m ashare_system.run

# Run with specific command (deployment smoke check)
cd ashare-system && python -m ashare_system.run deployment-smoke --host 127.0.0.1 --port 8100
```

### xtquantservice
```bash
cd xtquantservice && uvicorn xtquantservice.main:app --host 0.0.0.0 --port 8000
```

### Windows-only (PowerShell)
```powershell
cd ashare-system && pwsh ./scripts/start_local_services.ps1
```

## Code Style & Naming Conventions

### General
- **Python version**: 3.11+
- **Indentation**: 4 spaces (no tabs)
- **Line length**: Target under 120 characters
- **Encoding**: UTF-8

### Type Hints
- Use type hints on all public function signatures
- Use `X | None` syntax (Python 3.10+) over `Optional[X]`
- Use `X | Y` syntax over `Union[X, Y]`

```python
# Good
def process_decision(symbol: str, confidence: float) -> TradingIntent | None:
    ...

# Avoid
def process_decision(symbol, confidence):
    """Process decision."""
    ...
```

### Naming
- **Modules/functions/variables**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private methods**: prefix with `_`
- **Test names**: descriptive, e.g., `test_runtime_generates_buy_decision`

### Imports
- Order: standard library → third-party → local
- Use absolute imports within package
- Group by type: `from X import Y, Z`

```python
import logging
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel

from .adapters import ExecutionAdapter, MarketDataAdapter
from .contracts import TradingIntent, RuntimeDecision
```

### Classes
- Keep classes small and focused (single responsibility)
- Use dataclasses or Pydantic models for data containers
- Document public methods with docstrings

### Error Handling
- Use exceptions for exceptional cases
- Catch specific exceptions, avoid bare `except:`
- Return meaningful error responses in FastAPI endpoints

```python
# Good
try:
    result = adapter.get_stock_data(symbol)
except ValueError as e:
    raise HTTPException(status_code=400, detail=str(e))
```

### FastAPI Routes
- Keep route modules under `ashare_system/apps/`
- Use dependency injection for shared resources
- Return Pydantic models from endpoints

### Configuration
- Environment-specific settings in `settings.py` or environment variables
- Never hardcode credentials or paths
- Use mock/fallback modes for testing without live QMT

## Testing Guidelines

- Use `unittest` with `fastapi.testclient`
- Test filenames: `test_*.py`
- Keep fixtures self-contained (no live QMT session required)
- Test both happy paths and fallback behaviors
- Mock external dependencies (xtquant, HTTP calls)

```python
class RuntimeAppTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_container()
        self.client = TestClient(create_runtime_app())

    def test_runtime_generates_buy_decision(self) -> None:
        # test code here
        self.assertEqual(result.action, "BUY")
```

## Security & Configuration

- **NEVER** commit broker credentials, account IDs, or local QMT paths
- Use environment variables: `ASHARE_XTQUANT_USERDATA`, `ASHARE_ACCOUNT_ID`
- Keep sensitive config in `settings.py` overrides, not in code
- Use mock or fallback modes before enabling live trading

## Commit & Pull Request Guidelines

- Use short Chinese summaries for commits (e.g., `上传说明`, `实时技术面特征提取`)
- Prefer concise, imperative commit titles
- PR scope: specify affected subproject (`ashare-system`, `xtquantservice`, or `XtQuant`)
- Include test evidence and sample payloads for API updates

## Key Environment Variables

| Variable | Description |
|----------|-------------|
| `ASHARE_XTQUANT_USERDATA` | QMT userdata path |
| `ASHARE_ACCOUNT_ID` | Trading account ID |
| `ASHARE_EXECUTION_MODE` | Execution adapter mode (xtquant/mock/sim) |
| `ASHARE_MARKET_MODE` | Market data mode (xtquant/mock/akshare) |
| `ASHARE_RUN_MODE` | Run mode (live/dry-run) |
| `ASHARE_MIN_CONFIDENCE` | Minimum confidence threshold |

## Architecture Overview

```
ashare-system/
├── src/ashare_system/
│   ├── apps/              # FastAPI route modules
│   ├── adapters/          # Execution & market data adapters
│   ├── contracts/         # Pydantic request/response models
│   ├── run.py             # CLI entrypoint
│   └── settings.py        # Configuration
└── tests/                 # Test suite
```
