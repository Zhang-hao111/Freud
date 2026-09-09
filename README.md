# Coding Agent

一个通过与大语言模型（LLM）交互，自主完成编程任务的智能体系统。

## 安装

```bash
# 下载源码后，在项目目录下执行一行安装
uv tool install .
```

安装后 `freud` 成为全局命令，任意目录可用。

## 快速开始

### 前置条件

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)（包管理器）

```bash
# 安装 uv（如果没有）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 交互模式（推荐）

直接运行 `freud` 进入交互式 REPL，用自然语言或斜杠命令操作：

```bash
$ freud

  ╭──────────────────────────────────────╮
  │     Coding Agent  交互模式            │
  │                                       │
  │  输入 /help 查看斜杠命令              │
  │  或直接输入自然语言描述需求            │
  │  例如: "帮我运行 task.md"             │
  ╰──────────────────────────────────────╯

> 设置 API Key 为 sk-xxxxxxxxxxxxxxxx
  ✓ 已更新 API Key = sk-xxxx...

> 看看当前配置
  API Key:        sk-xxxx... (已设置)
  Model:          deepseek-chat
  Base URL:       https://api.deepseek.com

> 帮我运行 task.md
  [Agent 开始执行任务...]
```

### 命令行模式（一次性）

```bash
# 直接运行一个任务文件
freud --file task.md

# Mock 模式（无需 API Key）
freud --file task.md --mock
```

## 项目结构

```
.
├── main.py                 # 入口（兼容），委托给 agent.cli
├── .env                    # 配置（API Key、模型参数等）
├── .env.example            # .env 模板
├── .gitignore
├── pyproject.toml          # 项目元数据与依赖
├── README.md
│
├── agent/                  # 智能体核心代码
│   ├── cli.py              # CLI 入口：命令行解析与任务编排
│   ├── core.py             # 核心循环（ReAct Loop）+ 思考过程导出
│   ├── llm.py              # LLM 接口 + OpenAI/DeepSeek 实现 + MockLLM
│   ├── registry.py         # 工具注册表：read_file / write_file / shell
│   ├── guardrail.py        # 安全护栏：拦截危险 Shell 命令
│   ├── memory.py           # 文件级持久化 KV 存储
│   ├── tracer.py           # 可观测性：记录每一步的日志
│   ├── config.py           # 配置加载（.env → 环境变量）
│   └── types.py            # 共享类型定义
│
├── tasks/                  # 智能体生成的解决方案代码（运行时自动创建）
├── tests/                  # 智能体生成的测试代码（运行时自动创建）
│
└── docs/
    ├── design.md           # 架构设计文档
    └── thinking-*.md       # 思考过程记录（运行时自动生成）
```

## 架构设计

核心公式：**Agent = LLM × Harness**

| 组件 | 职责 |
|------|------|
| **LLM** | 推理引擎，决定"做什么"（读文件、写代码、跑测试） |
| **Harness** | 基础设施，提供"怎么做"（工具执行、安全检查、反馈回灌） |

### Agent Loop（ReAct 循环）

```
          ┌──────────────────────────────────┐
          │         Agent Loop (ReAct)        │
          │                                   │
          │   ① 组装上下文（系统提示 + 任务）  │
          │   ② 调用 LLM → 得到 Action        │
          │   ③ 安全护栏检查                  │
          │   ④ 执行工具或完成任务             │
          │   ⑤ 工具结果回灌 → 回到 ①         │
          │                                   │
          │   每步的思考 & 动作自动记录为 .md   │
          └──────────────────────────────────┘
```

### 行动模型（Action）

LLM 通过三种方式与系统交互：

| 类型 | 用途 | 示例 |
|------|------|------|
| `call_tool` | 调用工具 | 读文件、写代码、执行命令 |
| `take_note` | 存储记忆 | 记录关键信息供后续使用 |
| `done` | 完成任务 | 提交最终答案 |

### 工具系统

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件内容 |
| `write_file` | 写入/创建文件（自动创建父目录） |
| `shell` | 执行 Shell 命令（带超时 + 安全护栏） |

### 安全护栏

对 Shell 命令进行三态分类：

| 分类 | 含义 | 处理方式 |
|------|------|----------|
| `allow` | 安全 | 正常执行 |
| `deny` | 危险但可恢复 | 拦截，告知 LLM 换方法 |
| `escalate` | 破坏性操作 | 等待人工确认 |

## 思考过程记录

每次运行结束后，智能体的完整思考过程会自动保存到 `docs/` 目录下。每步记录包含：

| 栏目 | 内容 |
|------|------|
| **思考** | LLM 的推理过程 |
| **动作** | 调用的工具及参数 |
| **结果** | 工具执行返回的结果 |
| **安全护栏** | 如被拦截，记录拦截原因 |

## 配置说明

所有配置统一在 `config.json` 文件中设置（自动创建在运行目录下）：

```json
{
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "model": "deepseek-chat",
  "api_base": "https://api.deepseek.com"
}
```

也可通过环境变量覆盖：

```bash
export LLM_MODEL="gpt-4o"
freud --file task.md
```

### 配置优先级

```
代码默认值 < config.json < 环境变量
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `freud` | 进入交互式 REPL（自然语言 + 斜杠命令） |
| `freud --file task.md` | 直接运行指定任务文件 |
| `freud --file task.md --mock` | Mock 模式（无需 API Key，测试流程用） |
| `freud --mock` | 以 Mock 模式进入 REPL |
| `freud --max-steps 50 --file task.md` | 自定义最大步数 |
| `freud --help` | 查看帮助 |
| `freud --version` | 显示版本号 |
| `uv run python main.py --file task.md` | 兼容方式：通过 python 启动 |
| `uv run python -m pytest tests/ -v` | 运行智能体生成的测试（需安装 pytest） |

### REPL 命令参考

进入交互模式后，支持斜杠命令和自然语言两种输入：

| 意图 | 斜杠命令 | 自然语言示例 |
|------|----------|-------------|
| 查看配置 | `/config` | "看看当前配置" |
| 设置 API Key | `/key = sk-xxx` | "设置 key 为 sk-xxx" |
| 设置模型 | `/model = gpt-4o` | "把 model 改成 gpt-4o" |
| 设置 API 地址 | `/base-url = <url>` | "设置 base url 为 ..." |
| 运行任务 | `/run task.md` | "帮我运行 task.md" |
| 帮助 | `/help` | "帮助" |
| 退出 | `/exit` | "退出" |

## 使用示例

```bash
# 从文件读取任务
echo "
实现一个斐波那契数列计算函数 fib(n)，返回第 n 项。
验收用例：
- fib(1) = 1
- fib(5) = 5
- fib(10) = 55
" > my_task.md

freud --file my_task.md --name "斐波那契数列"
```

## 给使用者的提示

- **不要**把 `.env` 提交到 git（`.gitignore` 已自动忽略）
- 如需切换模型（如 OpenAI），修改 `.env` 中的 `LLM_MODEL` 和 `LLM_API_BASE`
- 智能体运行中的每一步日志会保存在 `~/.agent-harness/traces/`
- 智能体的思考过程会保存在 `docs/thinking-<时间戳>.md`