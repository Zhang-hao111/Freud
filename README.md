# Coding Agent

一个通过与大语言模型（LLM）交互，自主完成编程任务的智能体系统。

## 安装

```bash
# 下载源码后，在项目目录下执行一行安装
uv tool install .

# 开发模式（推荐）：全局命令实时跟随源码改动，无需重复安装
uv tool install --reinstall --editable .
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

  ██ ██ ██
  ██ ██ ██         Freud  v0.1.0
  █████████        deepseek-chat
     ███           /home/h
     ██

> 帮我写一个快速排序
  ⏺ Write(tasks/quick_sort.py)
    ⎿ 已写入 /home/h/tasks/quick_sort.py
  ⏺ Bash(python tasks/quick_sort.py)
    ⎿ [5, 3, 8, 1, 9] -> [1, 3, 5, 8, 9]
  ⏺ Done
  快速排序已完成并通过测试
```

输入框钉在终端底部（上下框线 + 权限模式栏），对话内容完整保留在终端回滚区，可随时上翻。

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
├── config.json             # 配置（API Key、模型参数等，已被 .gitignore 忽略）
├── .gitignore
├── pyproject.toml          # 项目元数据与依赖
├── README.md
│
├── agent/                  # 智能体核心代码
│   ├── cli.py              # CLI 入口：命令行解析、REPL 与任务编排
│   ├── core.py             # 核心循环（ReAct Loop）+ 记忆注入
│   ├── llm.py              # LLM 接口 + OpenAI/DeepSeek 实现（重试）+ MockLLM
│   ├── registry.py         # 工具注册表：read_file / write_file / shell
│   ├── guardrail.py        # 安全护栏：危险命令三态分类
│   ├── permissions.py      # 权限模式：ask / accept / yolo（Shift+Tab 切换）
│   ├── session.py          # 会话模型：每会话独立记忆与消息历史，/resume 恢复
│   ├── memory.py           # 文件级持久化 KV 存储（遗留，被 session 取代）
│   ├── tracer.py           # 可观测性：记录每一步的日志
│   ├── term.py             # 终端交互：raw-mode 行编辑器、方向键选择器、输入框
│   ├── ui.py               # 输出渲染：⏺/⎿ 行式布局、粉色主题、CJK 宽度对齐
│   ├── config.py           # 配置加载（config.json → 环境变量）
│   └── types.py            # 共享类型定义
│
├── tasks/                  # 智能体生成的解决方案代码（运行时自动创建）
├── tests/                  # harness 回归/单元测试（67 个用例，unittest）
│
└── docs/
    ├── design.md           # 架构设计文档
    └── debug-log-lab2.md   # Lab2 调试与完善记录
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

### 权限模式（Shift+Tab 循环切换）

在安全护栏之上还有一层用户可控的执行策略，终端底部模式栏实时显示当前状态：

| 模式 | 文件改动（write_file） | Shell 命令 |
|------|------|----------|
| `⏵⏵ ask before edits`（默认） | 每次询问，可回 `a` 切到 accept | 按护栏三态规则 |
| `⏵⏵ accept edits on` | 自动允许 | 按护栏三态规则 |
| `⏵⏵⏵ yolo - auto approve` | 自动允许 | escalate 级也自动批准（deny 仍拒绝） |

### 会话管理

- 每次启动都是**全新会话**：记忆从零开始，不携带任何历史笔记；
- 会话过程中 LLM 的 `take_note` 笔记与完整对话历史自动保存到 `~/.agent-harness/sessions/`；
- `/resume`（或启动参数 `--resume`）列出历史会话（TTY 下方向键选择），恢复其记忆与对话上下文，无缝续聊；
- batch 模式每次 `--file` 运行也是一个独立会话。

## 可观测性

每次运行中，agent 的每一步决策与执行结果都会记录到 tracer（`~/.agent-harness/traces/trace-<时间戳>.json`），可被外部工具加载复盘：

| 栏目 | 内容 |
|------|------|
| **step** | 步骤编号（一次 LLM 调用为一步） |
| **action** | 动作类型与参数（call_tool / take_note / done） |
| **result** | 工具执行返回的结果摘要 |
| **feedback** | 失败时注入给 LLM 的反馈文本 |

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
| `freud --file task.md --yes` | 自动批准 escalate 级危险操作（无人值守） |
| `freud --resume` | 启动时选择恢复一个历史会话 |
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
| 恢复会话 | `/resume` | "恢复会话" |
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

- **不要**把 `config.json` 提交到 git（`.gitignore` 已自动忽略，内含 API Key）
- 如需切换模型（如 OpenAI），修改 `config.json` 中的 `model` 和 `api_base`，或用环境变量 `LLM_MODEL` / `LLM_API_BASE` 覆盖
- 智能体运行中的每一步日志会保存在 `~/.agent-harness/traces/`
- LLM 调用内置指数退避重试（最多 3 次）；单次工具输出超过 20000 字符会自动截断
- 每次启动都是全新会话，记忆从零开始；`/resume`（或 `--resume`）可恢复历史会话的 take_note 笔记与对话上下文，会话文件存于 `~/.agent-harness/sessions/`
- batch 模式下 escalate 级危险操作：交互终端会人工确认，加 `--yes` 自动批准（慎用），非交互环境默认拒绝
- 调试与完善过程记录见 `docs/debug-log-lab2.md`