# Freud

Freud 是一个大数据课程实验项目，包含终端编程助手，以及基于 MovieLens 1M 的数据治理和分析系统。

- **编程助手**：结合 OpenAI 兼容模型与本地工具，读取和修改文件、执行命令、运行测试，并提供权限检查、会话恢复和执行轨迹。
- **迭代一：数据治理**：通过 Hadoop Streaming 检查和清洗数据，比较清洗前后的五维质量评分，生成版本化数据、报告和时间边界。
- **迭代二：数据分析**：使用迭代一产物进行分类、聚类和降维，通过网页查看结果、评价指标和图表。

分析流程为：`MovieLens 原始数据 → 迭代一清洗与评估 → 迭代二训练与查询`。迭代二要求迭代一产物的版本、哈希和 `T1/T2` 有效；数据或结果不可用时返回明确错误。迭代三是课程计划中的后续工作，当前仓库尚未实现。

## 数据治理与分析

### 迭代一：清洗与评估

准备 Python 3.11+、`uv`、Hadoop 3 和 Hadoop Streaming jar，将 MovieLens 的 `users.dat`、`movies.dat`、`ratings.dat` 放入 `ml-1m/ml-1m/`。原始数据和运行产物不随仓库分发。

```bash
uv sync
export HADOOP_STREAMING_JAR=/path/to/hadoop-streaming-3.x.x.jar
uv run freud-web
```

按[网页交互](#网页交互)首次构建前端，打开 `http://127.0.0.1:8875/`，在「＋ 数据任务」选择「清洗与质量评估」。成功后的 `governance-runs/<任务ID>/` 保存清洗数据、报告、数据版本和 `T1/T2`。Hadoop 本地模式可以运行实验，但不代表多节点集群部署。环境配置和评分口径见 [迭代一说明](docs/iteration1/README.md)。

### 迭代二：分类、聚类和降维

| 任务 | 已实现算法 |
| --- | --- |
| 分类 | Logistic Regression、Random Forest |
| 聚类 | K-Means、DBSCAN |
| 降维 | PCA、Truncated SVD |

在同一网页选择「训练分析模型」和「查询分析结果」。也可以从命令行使用迭代一产物训练：

```bash
RUN_ID=你的迭代一任务ID
uv run freud-analysis-train --iteration1-run "governance-runs/$RUN_ID" --output analysis-runs
```

训练完成后可回到同一个 Freud 网页查询。模型和报告保存在 `analysis-runs/`，不会随源码分发。分类与画像使用训练期数据，验证期用于模型选择，测试期用于最终评价。任务识别使用登记规则和工具。配置 LLM 后可解读已登记结果及回答追问。算法参数、数据口径和结果文件说明见 [迭代二说明](docs/iteration2/README.md)。

实验指标以具体数据版本的实际报告为准。

## 编程助手

### 主要能力

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
- `numpy>=2.0.0`、`joblib>=1.4.0`、`scikit-learn>=1.6.0`（数据分析）

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
| `WORKSPACE` | 默认工作区的目录 |
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
├── governance/     # Hadoop 数据治理流程与网页
├── analysis/       # 分类、聚类、降维训练与网页
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

## 网页交互

Freud 的 React 网页直接连接本项目的 Python agent。前端参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/39163dd30f703a79079e2d2446545cff3c453738/packages/client)，复用其主题、部分侧栏和输入框样式，以及 Markdown 中文加粗规则。版权和 MIT 许可见 [web/LICENSE](web/LICENSE)。

会话沿用 SessionStore。工具调用显示在对话中，写入文件和护栏要求审批的命令需要在页面确认。模型配置沿用上文的 `config.json` 和环境变量，API Key 由 Python 后端读取。

在源码目录首次启动前构建前端（需要 Node.js 22.12+ 和 pnpm）：

```bash
cd web
pnpm install --frozen-lockfile
pnpm build
cd ..
uv run freud-web --mock
```

打开 `http://127.0.0.1:8875/`。Mock 模式模拟文件写入审批，不调用模型、不执行工具；去掉 `--mock` 后使用现有模型配置和真实工具。当前版本按模型响应和工具事件逐条更新页面，不支持逐 token 输出或中途停止；只监听本机地址。修改前端后需要重新执行 `pnpm build` 并刷新页面，修改 Python 后端后需要重启 `freud-web`。页面暂从源码目录启动，前端构建产物不包含在 Python wheel 中。

### MovieLens 插件与会话删除

在网页输入框底部点击“＋ 数据任务”选择功能：

- **迭代一 · 清洗评估**：输入包含“清洗”“评估”或 MovieLens 的需求。回复展示 Hadoop 实际计算的五维前后对照、评分依据、处置数量和原因、清洗后的样例和数据版本，并提供报告。课程数据包含异常时，报告会区分本次输入数量与标准版基准。
- **迭代二 · 训练**：填写已经完成的迭代一任务 ID。同一对话中已运行迭代一时可留空。训练回复按三类并排比较六种算法，分类为逻辑回归和随机森林，聚类为 K-Means 和 DBSCAN，降维为 PCA 和 Truncated SVD。高级参数可用 JSON 对象指定，例如 `{"clusters": 3}`。
- **迭代二 · 分析**：在问题中明确分类、聚类或降维，填写用户 ID。分类还需电影 ID。多个用户用逗号分隔。不指定算法时展示对应类别的两种算法，正文提供结果、指标与参数，降维回复另附坐标和图表。高级参数接受 `algorithm`、`parameters`、`target_dimensions`、`information_threshold`、`cluster_algorithm` 和 `cluster_parameters`。
- **继续提问**：任务完成后可直接询问相关数据、指标或参数，或发送“查看完整结果”重新展示已登记结果。在完成训练的对话里可发送“查看六种算法”回看训练总览。点击“结束结果上下文”可回到普通对话。

配置模型后，Freud 先显示本地计算结果，再在同一条消息中补充 LLM 解读。任务解释和追问会把本次问题、同一任务最近的问题及相关登记指标发送给已配置的模型服务，治理原始记录和完整数据文件留在本地。模型不可用时仍可查看本地结果和规则回复。解读与普通对话使用同一模型配置，报告和图表直接使用登记产物。

以上数据任务需去掉 `--mock` 启动，且仍需满足各迭代的原始数据、Hadoop 和结果产物要求。迭代二分析优先使用当前对话中训练出的结果，也可读取 `analysis-runs/latest.json` 指向的结果。有清洗上下文时会核对清洗版本、哈希和 T1/T2。清洗和训练会在 `governance-runs/`、`analysis-runs/` 创建真实产物。网页检测到 Hadoop 默认文件系统为 `file:///` 时，将中间文件放到本机临时目录。使用 HDFS 时默认放在 `/freud/governance`，也可以通过 `GOVERNANCE_HDFS_ROOT` 指定。

普通智能对话的输入框底部可切换权限模式：默认询问写入和需升级的命令；“自动允许编辑”跳过写入确认，但需升级的命令仍需审批；“自动批准（危险）”自动批准写入及需升级的命令，护栏明确拒绝的操作仍被拦截。切换或新建会话时重置为默认询问。

每个侧栏对话右侧的“×”可删除该会话，删除前需要确认；运行中的会话不能删除。网页与终端共享 SessionStore，因此删除也会移除终端的对应会话记录，但不会删除数据任务的结果文件。

### 架构

Freud 参考 [DeepSeek Harness 的架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)，把入口、对话循环和任务能力分开，在启动时装配本项目的 Python 插件。

- `agent/runtime.py` 装配共享服务。`agent/core.py` 驱动 Web、REPL 和批处理的对话循环。`agent/tool_execution.py` 统一执行护栏、审批策略与工具调用，入口负责显示与接收审批。
- `agent/workspaces.py` 登记 Web 工作区。默认工作区沿用启动配置的 `WORKSPACE`，未配置时使用启动目录。侧栏的「＋」可添加已有项目目录。新对话在首次发送前选择工作区，会话按保存的目录分组，普通聊天的相对路径和 Shell 命令在该目录执行。项目目录登记在 `~/.agent-harness/workspaces.json`。
- `agent/workflows.py` 统一登记任务、管理结果上下文和发送回复事件。`agent/movielens.py` 注册任务、表单、追问与产物。网页从 `GET /api/workflows` 生成菜单与表单。
- `governance/service.py` 调用 Hadoop 工具，`analysis/service.py` 调用六个已登记工具。阶段进度直接传回统一网页。
- `agent/conversations.py` 管理会话回合、工作区归属及普通对话和任务追问的路由。回合开始时读取已落盘的最新会话，避免旧请求覆盖上个回合或恢复已清除的结果上下文。
- `agent/web.py` 提供网页入口 `freud-web`，负责 HTTP、审批交互及 NDJSON 传输。页面通过同一会话查看两轮产物、追问和下载报告。普通聊天和数据解读共享模型连接。

`freud-analysis-train` 仍可离线训练。插件在 Python 进程启动时登记，暂未实现 Cordis 运行时和动态插件安装。
