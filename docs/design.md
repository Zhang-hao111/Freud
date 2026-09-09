# 编程智能体设计文档

## 1. 概述

本文档描述编程智能体（Coding Agent）的系统设计。该智能体通过与 LLM 交互，自主完成"读取任务 → 编写代码 → 运行测试 → 根据结果迭代"的闭环，最终解决指定的编程问题。

核心理念：**Agent = LLM × Harness**。LLM 提供推理能力（CPU），Harness 提供使其可靠运行的操作系统。关键原则：安全检测、反馈回路、解析逻辑等**机制必须是代码而非提示词**，使每一层都可以脱离 LLM 独立测试。

## 2. 架构总览

系统分为五个垂直层：

```
┌──────────────────────────────────────────────────────────┐
│                  CLI Layer (agent/cli.py)                  │
│       命令行解析（--file/--mock/--name）、配置加载         │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│                   Harness Core (agent/core.py)            │
│                                                           │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐  │
│  │ Agent Loop  │  │  Guardrail  │  │  Thinking Export │  │
│  │ (ReAct 循环) │  │ (三态护栏)  │  │ (思考过程→.md)   │  │
│  └──────┬──────┘  └─────────────┘  └──────────────────┘  │
│         │                                                  │
│  ┌──────▼──────┐  ┌─────────────┐  ┌──────────────────┐  │
│  │  Memory     │  │  Tracer     │  │  Config/Env      │  │
│  │ (持久化KV)  │  │ (可观测性)  │  │ (凭证管理)       │  │
│  └─────────────┘  └─────────────┘  └──────────────────┘  │
└──────────────────────────┬───────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌──────────────┐ ┌────────────┐ ┌──────────────┐
   │  LLM Layer   │ │ Tool System│ │  Guardrail   │
   │ (Provider +  │ │ (read_file │ │  (pattern    │
   │  MockLLM)    │ │ write_file │ │  匹配拦截)   │
   └──────────────┘ │ shell)     │ └──────────────┘
                    └────────────┘
```

## 3. Agent Loop（核心循环）

Agent Loop 采用 **ReAct（Reasoning + Acting）** 模式，每一轮迭代包含五个阶段：

```
1. Context Assembly  — 组装上下文：system prompt + memory + task + 历史消息
2. LLM Invocation   — 发送给 LLM，接收结构化 Action
3. Guardrail Check  — 拦截 Action，三态安全分类
4. Tool Execution   — 分发到对应工具执行
5. Feedback Injection — 将结果/错误追加回 conversation
```

伪代码：

```
def run_agent(goal: str) -> str:
    messages = build_context(goal)
    steps = 0

    while not done and steps < max_steps:
        # 1. 调用 LLM
        response = llm.chat(messages, tools=list_tools())

        # 2. 解析 Action
        action = parse_action(response)
        messages.append(response.message)

        # 3. Guardrail 检查
        guard = guardrail_check(action)
        if guard.disposition == 'deny':
            messages.append(error_message(guard.reason))
            steps += 1
            continue
        elif guard.disposition == 'escalate':
            approved = await approver(action)
            if not approved:
                messages.append(error_message("操作被拒绝: " + guard.reason))
                steps += 1
                continue

        # 4. 分发执行
        if action.type == 'done':
            answer = action.answer
            done = True
        elif action.type == 'call_tool':
            result = tool_registry.execute(action.tool, action.args)
            messages.append(tool_message(result))
        elif action.type == 'take_note':
            memory.write(action.note_key, action.note_value)
            messages.append(tool_message("已记录"))

        # 5. 反馈注入（重点）
        if result and not result.success:
            inject_feedback(messages, action, result)

        steps += 1

    memory.consolidate()
    tracer.flush()
    
    # 6. 导出思考过程
    write_thoughts_md(task_name, steps_data, output_path)
    return answer or f"达到最大步数({max_steps})，任务未完成"
```

### 关键设计决策

#### 决策 1：反馈回路是核心机制

反馈是 harness 工程的灵魂。MVP 阶段使用简单的 try/catch 将错误信息回灌；后续可演化为完整的传感器流水线：

```
Tool.execute() →原始输出
  → Parser.parse() → 结构化错误
    → Classifier.classify() → 带标签的错误分类
      → Injector.inject() → 格式化反馈文本 → 追加到 context
```

#### 决策 2：Action 模型

Action 是 LLM 与 Harness 之间的契约，采用可扩展的联合类型：

```python
@dataclass
class Action:
    type: Literal['call_tool', 'done', 'take_note']
    tool: str | None = None          # call_tool 时使用
    args: dict | None = None         # call_tool 时使用
    answer: str | None = None        # done 时使用
    note_key: str | None = None      # take_note 时使用
    note_value: str | None = None    # take_note 时使用
    tool_call_id: str | None = None  # LLM 返回的 tool call ID
```

## 4. LLM Layer

### 4.1 接口抽象

```python
class LLMProvider(ABC):
    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef],
    ) -> LLMResponse:
        ...
```

### 4.2 实现

| 实现 | 用途 |
|------|------|
| `OpenAIProvider` | 生产使用，兼容 OpenAI / DeepSeek / OpenRouter 等 |
| `MockLLM` | 测试使用，返回预定义的响应序列，实现"无需 API key 的确定性测试" |

### 4.3 Tool Definitions 构建

将系统内部的 ToolDef 列表合成为 OpenAI 兼容的 tool 定义数组，每个工具生成 `{ type: 'function', function: { name, description, parameters } }`。额外注入两个控制工具：

- **`done`** — LLM 调用此工具表示任务完成，携带最终答案
- **`take_note`** — LLM 调用此工具存储一条记忆（key-value pair）

### 4.4 解析 LLM 响应

LLM 响应有两种路径：

1. **`tool_calls` 存在** → 解析为 Action 对象（`call_tool` / `done` / `take_note`）
2. **纯文本回复** → 视为普通 assistant message，继续下一轮循环

## 5. Tool System

### 5.1 ToolDef 接口

参考仓库的插件式设计，每个工具实现统一接口：

```python
@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict  # JSON Schema

class BaseTool(ABC):
    name: str
    description: str
    parameters: dict

    @abstractmethod
    def execute(self, args: dict) -> ToolResult:
        ...
```

### 5.2 预定义工具

| 工具名 | 功能 | 参数 |
|--------|------|------|
| `read_file` | 读取文件内容 | `path: str` |
| `write_file` | 写入文件，自动创建父目录 | `path: str, content: str` |
| `shell` | 执行 Shell 命令（带超时） | `command: str` |

说明：与初版设计相比，去掉了独立的 `edit_file` 和 `read_task` 工具。`read_task` 可通过 `read_file` 完成；`edit_file` 在 MVP 阶段不是必需的（`write_file` 已可覆盖），后续可按需添加。

### 5.3 ToolRegistry

管理工具的注册、查找和执行：

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool): ...
    def list(self) -> list[BaseTool]: ...
    def get(self, name: str) -> BaseTool | None: ...
    def execute(self, name: str, args: dict) -> ToolResult: ...
```

## 6. Guardrail（安全护栏）

### 6.1 三态分类

Guardrail 是一个纯函数，对 Action 进行确定性检查。**拦截逻辑是代码而非提示词。**

| 分类 | 含义 | 行为 |
|------|------|------|
| `allow` | 安全 | 工具正常执行 |
| `deny` | 已拦截但可恢复 | 错误信息回馈给智能体，LLM 可尝试其他方法 |
| `escalate` | 需人工判断 | 暂停循环，调用 approver() 等待人决策 |

### 6.2 危险模式表（示例）

```python
DEFAULT_DANGEROUS_PATTERNS = [
    # escalate — 可能破坏系统
    (r'rm\s+-rf\s+/',       'escalate', '删除文件系统'),
    (r'mkfs',               'escalate', '格式化磁盘'),
    (r'dd\s+if=',           'escalate', '覆写磁盘'),
    (r'>\s*/dev/sda',       'escalate', '覆写磁盘'),
    (r'fdisk',              'escalate', '改分区表'),
    # deny — 拒绝执行
    (r':\(\)\{\s*:\|\|:\&\s*\};:', 'deny', 'fork 炸弹'),
]
```

### 6.3 Approver 注入

`approver` 通过依赖注入传入，生产环境使用 `readline` 交互式确认，测试环境使用 deterministic mock。这使得人工在环（HITL）的状态机完全可以测试。

## 7. Memory（记忆系统）

文件级持久化 key-value 存储，用于跨会话保持上下文。

```python
class FileMemory:
    def __init__(self, file_path: str): ...  # 从磁盘加载
    def read(self, key: str) -> str | None: ...
    def write(self, key: str, value: str): ...
    def consolidate(self): ...  # 脏数据写回磁盘
```

- 使用内存中 `dict` 做索引，JSON 序列化到磁盘
- `dirty` 标志避免无变更时的重复写盘
- LLM 通过 `take_note` 工具写入，通过 `read` 方法检索

## 8. Tracer（可观测性）

记录每一步的决策与执行结果，用于调试和展示。

```python
class Tracer:
    def __init__(self, dir: str): ...

    def record(
        self,
        step: int,
        action: Action,
        result: str,
        feedback: str | None = None,
    ): ...

    def get_trace(self) -> list[TraceEntry]: ...
    def flush(self): ...  # 写入 JSON 文件
```

每条记录包含：`step`、`action`、`result`、`timestamp`、`feedback`。输出为 `trace-<时间戳>.json`，可被外部工具加载查看。

## 9. CLI 层（任务入口）

CLI 层提供 `coding-agent` 命令作为统一入口，支持以下模式：

| 参数 | 说明 |
|------|------|
| `--file <path>` | 从 markdown 文件读取任务描述 |
| `--name <name>` | 自定义任务名称（默认"自定义任务"） |
| `--mock` | 使用 Mock LLM（无需 API Key） |
| `--max-steps <N>` | 自定义最大迭代步数（默认 30） |

任务输入流程：

```
coding-agent --file task.md
      │
      ▼
读取任务描述文件（markdown）
      │
      ▼
build_context(goal + memory) → Agent Loop (ReAct)
      │
      ▼
循环直至完成或达到最大步数
      │
      ▼
输出结果 + 保存思考过程到 docs/
```

- 不指定 `--file` 时从 stdin 读取，支持管道传递任务描述
- 不再内置固定的任务列表，每次执行处理一个任务

## 10. 数据流

```
用户在终端输入 coding-agent --file task.md
    │
    ▼
agent/cli.py 解析参数 → 加载配置（.env + 环境变量）
    │
    ▼
读取任务描述文件（或 stdin）
    │
    ▼
build_context(goal + memory)
    │
    ▼
Agent Loop:
  1. LLM(context, tool_defs) → Action
  2. Guardrail 检查 Action
     ├─ allow → 执行工具
     ├─ deny → 注入 "操作被拦截" 消息，继续循环
     └─ escalate → 暂停，调用 approver()，决定后放行或拒绝
  3. 工具执行 (call_tool / done / take_note)
  4. 工具结果注入 context
  5. 检查步数上限（默认 30）
  6. 如果未完成，回到步骤 1
    │
    ▼
Agent Loop 结束（done 或达到最大步数）
    │
    ▼
memory.consolidate() → tracer.flush()
    │
    ▼
write_thoughts_md() → 保存思考过程到 docs/thinking-*.md
    │
    ▼
输出结果到终端

## 11. 提示词设计

### System Prompt 结构

```
你是 coding-agent-harness，一个编程智能体。
你有以下工具可用：
- read_file: 读取文件内容
- write_file: 写入文件内容（自动创建父目录）
- shell: 执行 Shell 命令

你的工作流程：
1. 读取任务描述文件，理解需要实现什么
2. 编写解决方案代码
3. 编写测试代码覆盖验收用例
4. 运行测试，根据测试结果迭代修改代码
5. 所有测试通过后，调用 done(answer) 完成任务

文件存放规范：
- 解决方案代码放到 tasks/ 目录下
- 测试代码放到 tests/ 目录下

重要约束：
- 使用通用的算法实现，不得针对特定输入硬编码返回值
- 每个任务独立完成，不要在前一个任务的代码基础上修改
- 如果 shell 命令被拦截，尝试用其他方法实现

当前工作目录：{workspace}
```

### 关键原则

- 安全检测不用 prompt 实现（由 Guardrail 代码层负责）
- 错误反馈通过代码注入结构化信息，而非依赖 LLM "记住"之前的错误
- prompt 只负责描述工具和流程，不负责策略约束

## 12. 凭证安全策略

采用两层策略：

1. **主方案：环境变量** — `LLM_API_KEY` 通过 `.env` 文件或 Shell export 传入，代码中通过 `os.environ` 读取
2. **扩展方案（可选）：加密存储** — 参考仓库使用 AES-256-GCM 加密存储认证凭据

安全约束：
- API Key 绝不写入代码、日志或 trace 文件
- 所有配置读取走独立模块（`agent/config.py`），统一管理

## 13. 错误处理与健壮性

| 场景 | 策略 |
|------|------|
| LLM API 调用失败（网络、rate limit） | 指数退避重试，最多 3 次 |
| LLM 返回无效 tool call（JSON 解析失败） | 重新请求，附加纠错提示 |
| 工具执行失败（文件不存在、命令报错） | 将错误信息作为 observation 返回，让 LLM 自行纠错 |
| 同一错误反复出现 | 步数上限（30 轮）兜底 |
| Shell 命令超时 | 默认 30s 超时，捕获超时异常 |
| Guardrail 拦截 | deny 自动回退；escalate 交人工决策 |

## 14. 测试策略

参考仓库的"每层独立验证"思路：

1. **MockLLM 测试** — 给定 goal 和预定义响应序列，验证 harness 能在 ≤5 轮循环内返回答案
2. **Guardrail 测试** — 每个危险模式触发正确的 disposition；mock approver 返回 false 时智能体收到拒绝反馈并改变策略
3. **反馈回路测试** — 第一步失败后，第二步智能体能选择不同的 action
4. **Memory 测试** — 写入 key-value，读取验证
5. **Trace 测试** — 每一步都记录了 action + result + timestamp

## 15. 项目结构

```
.
├── agent/                  # 智能体核心代码
│   ├── __init__.py
│   ├── cli.py              # CLI 入口：argparse 解析 + 任务编排
│   ├── core.py             # 主循环控制器 (Agent Loop) + 思考过程导出
│   ├── llm.py              # LLM Provider (OpenAI/DeepSeek) + MockLLM
│   ├── registry.py         # 工具注册表 (read_file/write_file/shell)
│   ├── guardrail.py        # 三态安全护栏
│   ├── memory.py           # 文件级持久化 KV 存储
│   ├── tracer.py           # 可观测性记录
│   ├── config.py           # 配置加载 (API key、模型等)
│   └── types.py            # 共享类型定义
├── tasks/                  # 智能体生成的解决方案代码（运行时自动创建）
├── tests/                  # 智能体生成的测试代码（运行时自动创建）
├── docs/
│   ├── 实验要求.md          # 实验说明
│   ├── design.md           # 本文档
│   └── thinking-*.md       # 思考过程记录（运行时自动生成）
├── main.py                 # CLI 入口（兼容，委托给 agent.cli）
├── pyproject.toml          # 项目元数据与依赖（含 console_scripts 入口）
├── .env                    # 配置（API Key、模型参数等）
├── .env.example            # .env 模板
├── .gitignore
└── README.md
```

## 16. 技术选型

| 层面 | 选择 | 理由 |
|------|------|------|
| 编程语言 | Python 3.11+ | LLM 生态成熟、工具链完善 |
| LLM API 协议 | OpenAI Chat Completions API | 事实标准，兼容 DeepSeek / OpenRouter 等 |
| API 客户端 | `openai` Python SDK | 官方维护，支持 tool calling 和 streaming |
| 依赖管理 | `uv` | 项目已有约定 |
| 测试框架（自身） | `pytest` | Python 生态最广泛 |
| 测试框架（智能体生成） | `pytest` | 通用、轻量，适合编程题型 |

## 17. 边界与约束

1. **编程语言由配置决定**：默认 Python，可通过 `pyproject.toml` 配置切换
2. **不依赖智能体框架**：直接调用 API，不引入 LangChain / OpenAI Agents SDK 等
3. **单任务模式**：每次执行处理一个任务，通过 `--file` 参数指定任务描述
4. **安全约束**：
   - API Key 通过环境变量注入
   - Shell 命令有 Guardrail 防护 + 30s 超时
   - 所有凭证和敏感信息不写入代码、日志和 trace 文件

## 18. 与参考仓库的关键差异

本项目基于 [coding-agent-harness](https://github.com/Zhang-hao111/coding-agent-harness) 的设计理念，但做了以下调整：

| 差异项 | 参考仓库 | 本项目 |
|--------|----------|--------|
| 实现语言 | TypeScript | Python |
| CLI 框架 | commander | 内置 argparse（`agent/cli.py`） |
| 任务模型 | 单一 goal 输入 | 通用 `--file` 参数，支持文件/stdin 输入 |
| WebUI | Express + Open Design 调试面板 | 暂不实现，通过 tracer JSON + 思考过程 .md 查看 |
| 凭证管理 | AES-256-GCM + env fallback | env 为主（SOCKS 代理自动降级） |
| Mock LLM | 有 | 有（集成在 `llm.py` 中） |
| 迭代上限 | 50 步 | 30 步（可通过 `--max-steps` 调节） |