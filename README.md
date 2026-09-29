# Freud

Freud 是一个运行在终端中的轻量级 AI 编程助手。它将兼容 OpenAI 接口的语言模型与受控工具执行环境组合起来，可以根据自然语言任务读取代码、修改文件、执行命令、运行测试，并把工具结果继续反馈给模型完成后续判断。

项目重点提供可复用的 Agent 执行框架，而不是把模型输出直接当作最终结果。工具调用、安全检查、权限确认、会话恢复和执行轨迹都由本地 Harness 管理。

## 主要能力

- 交互式终端会话和单次任务文件两种运行方式；
- 支持 OpenAI 兼容的 Chat Completions API；
- 内置文件读取、文件写入和 Shell 命令工具；
- 基于工具结果循环规划和执行任务；
- 对危险命令进行允许、拒绝或人工确认分类；
- 支持询问确认、自动接受编辑和无人值守三种权限模式；
- 保存会话、笔记和消息历史，可在后续恢复；
- 保存结构化执行轨迹，便于调试和复盘；
- 提供不需要 API Key 的 Mock 模式用于测试流程。

## 运行依赖

### 必需环境

- Python 3.11 或更高版本；
- [`uv`](https://docs.astral.sh/uv/)；
- Git 和可执行常用命令的本地 Shell。

### Python 依赖

项目安装时会自动安装：

- `openai>=1.0.0`

使用真实模型时，还需要一个兼容 OpenAI Chat Completions 协议的 API 地址、模型名称和 API Key。Mock 模式不需要外部模型服务。

## 安装

克隆仓库后，在项目目录执行：

```bash
uv tool install .
```

开发时建议使用可编辑安装，使命令直接跟随源码变化：

```bash
uv tool install --reinstall --editable .
```

也可以不安装全局命令，直接在仓库中运行：

```bash
uv sync
uv run freud --help
```

## 配置

Freud 按以下顺序查找 `config.json`：

1. 当前工作目录；
2. 源码仓库根目录；
3. `~/.agent-harness/config.json`。

示例配置：

```json
{
  "api_key": "your-api-key",
  "model": "your-model-name",
  "api_base": "https://your-provider.example/v1",
  "max_steps": 30,
  "shell_timeout": 30
}
```

环境变量的优先级高于配置文件：

| 环境变量 | 用途 |
| --- | --- |
| `LLM_API_KEY` 或 `DEEPSEEK_API_KEY` | API Key |
| `LLM_MODEL` | 模型名称 |
| `LLM_API_BASE` | OpenAI 兼容 API 地址 |
| `MAX_STEPS` | 单次任务最大 Agent 步数 |
| `SHELL_TIMEOUT` | Shell 命令超时时间，单位为秒 |
| `WORKSPACE` | 工具允许操作的工作目录 |
| `TRACES_DIR` | 执行轨迹保存目录 |

配置优先级为：

```text
代码默认值 < config.json < 环境变量
```

`config.json` 包含凭据，已经由 `.gitignore` 排除，不应提交到版本库，也不应出现在日志、截图或演示视频中。

## 使用方式

### 交互模式

直接启动：

```bash
freud
```

进入交互界面后，可以直接输入自然语言任务，也可以使用斜杠命令：

| 命令 | 作用 |
| --- | --- |
| `/config` | 查看当前模型配置 |
| `/key = ...` | 设置 API Key |
| `/model = ...` | 设置模型名称 |
| `/base-url = ...` | 设置 API 地址 |
| `/run task.md` | 运行 Markdown 任务文件 |
| `/resume` | 恢复历史会话 |
| `/help` | 查看帮助 |
| `/exit` | 退出程序 |

### 任务文件模式

将任务目标和验收要求写入 Markdown 文件，然后运行：

```bash
freud --file task.md --name "任务名称"
```

限制最大执行步数：

```bash
freud --file task.md --max-steps 50
```

### Mock 模式

不访问外部模型，用于验证 CLI、会话和工具链路：

```bash
freud --mock
freud --file task.md --mock
```

### 恢复会话

启动时选择历史会话：

```bash
freud --resume
```

会话文件默认保存在 `~/.agent-harness/sessions/`。每个新任务默认创建独立会话，不会自动继承其他会话的上下文。

## 权限与安全

Freud 对工具操作提供三种权限模式，可在交互界面中使用 `Shift+Tab` 切换：

| 模式 | 文件修改 | 高风险命令 |
| --- | --- | --- |
| `ask before edits` | 修改前询问 | 必须确认或拒绝 |
| `accept edits on` | 自动允许普通编辑 | 仍按安全规则处理 |
| `yolo - auto approve` | 自动允许 | 自动批准需要升级确认的操作，谨慎使用 |

批处理模式可以使用 `--yes` 自动批准需要确认的命令：

```bash
freud --file task.md --yes
```

该选项适合受控的无人值守环境，不建议在包含重要文件或广泛系统权限的工作区中使用。安全护栏明确拒绝的命令不会因为 `--yes` 而绕过。

## 执行流程

一次任务通常经过以下循环：

1. 读取系统配置、会话上下文和用户任务；
2. 调用模型生成工具操作或完成结果；
3. 权限模块和安全护栏检查工具调用；
4. 执行文件或 Shell 工具；
5. 将真实工具结果反馈给模型；
6. 继续执行，直到完成、失败或达到最大步数。

## 数据与日志

| 路径 | 内容 |
| --- | --- |
| `~/.agent-harness/config.json` | 全局模型配置 |
| `~/.agent-harness/sessions/` | 会话、消息和会话内笔记 |
| `~/.agent-harness/traces/` | 每一步模型动作、工具结果和反馈 |
| `tasks/` | 任务文件以及任务执行过程中生成的工作文件 |

## 项目结构

```text
.
├── agent/          # Agent 循环、模型接口、工具、权限、会话和终端界面
├── docs/           # 需求、设计和使用文档
├── tasks/          # 任务描述及任务产物
├── tests/          # 单元测试和回归测试
├── main.py         # 兼容入口，转发到 agent.cli
├── pyproject.toml  # 包信息、依赖和命令入口
└── install.sh      # 安装辅助脚本
```

## 测试

运行全部测试：

```bash
uv run python -m unittest discover -s tests -v
```

查看命令参数：

```bash
freud --help
```

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--file FILE` | 从 Markdown 文件读取任务并直接执行 |
| `--name NAME` | 设置任务名称 |
| `--max-steps N` | 设置最大 Agent 步数 |
| `--mock` | 使用 Mock LLM |
| `--resume` | 选择并恢复历史会话 |
| `--yes` | 自动批准需要确认的高风险操作 |
| `--version` | 显示版本号 |
