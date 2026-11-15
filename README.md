# Freud · MovieLens 数据分析 Agent

Freud 是大数据分析课程项目，以 MovieLens 1M 为数据基础，将 Hadoop 数据治理、机器学习分析和 Agent 对话整合到同一个网页中。用户可以提交数据任务、查看执行进度与报告，并针对实际结果继续提问。

项目目前完成迭代一和迭代二。迭代三的知识图谱与图分析是课程后续内容，尚未实现。

```text
MovieLens 1M
    │
    ▼
迭代一 · Hadoop 数据检查、清洗和五维质量评估
    │  清洗数据、数据版本、报告、T1/T2
    ▼
迭代二 · 分类、聚类和降维训练
    │  模型、用户画像、指标、坐标和图表
    ▼
网页查询、结果解读与追问
```

## 已实现的课程功能

### 迭代一：数据治理

通过 Hadoop Streaming 检查并清洗用户、电影和评分数据，对清洗前后数据使用同一套规则评估 Accurate、Complete、Unique、Up-to-date 和 Consistent 五个质量维度。任务保存清洗数据、异常处置记录、数据与规则版本、评估报告，以及供后续训练使用的时间边界 T1/T2。

实现细节与评分口径见 [迭代一说明](docs/iteration1/README.md)。

### 迭代二：数据分析

迭代二读取已完成的迭代一产物，核对数据版本、文件哈希和时间边界，再训练并登记六种算法。

| 任务 | 算法 | 主要结果 |
| --- | --- | --- |
| 分类 | Logistic Regression、Random Forest | 用户对电影给出至少 4 分的预测概率与类别、验证及测试指标 |
| 聚类 | K-Means、DBSCAN | 用户群体、群体画像、噪声标记及聚类指标 |
| 降维 | PCA、Truncated SVD | 用户坐标、保留方差、二维图及压缩前后聚类对照 |

分类与用户画像使用训练期数据。验证期用于分类模型选型，测试期用于最终评价。聚类和降维默认要求用户在训练期至少有两条评分，历史不足时不会生成该用户的画像。

查询会返回已登记的实际结果，并可下载报告。配置 LLM 后，系统还会根据结果生成解读并回答追问。算法参数、评价指标和数据口径见 [迭代二说明](docs/iteration2/README.md)。

## 环境准备

- Python 3.11 或更新版本，以及 [uv](https://docs.astral.sh/uv/)
- Node.js 22.12 或更新版本，以及 pnpm
- Hadoop 3、Hadoop Streaming jar 和工作节点上的 Python 3
- MovieLens 1M 的三个原始文件
- 可选：兼容 OpenAI Chat Completions API 的模型服务，用于普通对话、结果解读和追问

在项目根目录放置原始数据：

```text
ml-1m/ml-1m/
├── users.dat
├── movies.dat
└── ratings.dat
```

保留原始文件格式和编码。数据、API Key、模型和运行结果不随仓库分发。

## 安装与启动

在项目根目录安装 Python 依赖并构建网页：

```bash
uv sync
cd web
pnpm install --frozen-lockfile
pnpm build
cd ..
```

配置 Hadoop Streaming jar，路径根据本机安装位置填写：

```bash
export HADOOP_STREAMING_JAR=/path/to/hadoop-streaming-3.x.x.jar
```

若需要 LLM 对话和结果解读，在项目根目录创建本地 `config.json`：

```json
{
  "api_key": "your-api-key",
  "model": "your-model-name",
  "api_base": "https://your-provider.example/v1"
}
```

也可以使用 `LLM_API_KEY`（或 `DEEPSEEK_API_KEY`）、`LLM_MODEL` 和 `LLM_API_BASE` 环境变量。配置优先级为环境变量、`config.json`、程序默认值。数据任务无需 LLM 即可执行和展示登记结果。

启动网页：

```bash
uv run freud-web
```

访问 `http://127.0.0.1:8875/`。网页只监听本机地址。修改前端后重新运行 `pnpm build` 并刷新页面，修改 Python 后端后重启 `freud-web`。

## 网页交互

### 运行两次迭代

1. 新建对话，在输入框下方点击「＋ 数据任务」，选择「清洗与质量评估」。
2. 输入「请清洗并评估 MovieLens 1M 数据」，等待 Hadoop 各阶段完成。页面展示五维评分对照、异常处置和数据版本，并提供报告下载。
3. 在同一对话选择「训练分析模型」，输入「请使用清洗后的数据训练分析模型」。迭代一任务 ID 留空时，使用当前对话最近一次成功清洗结果。
4. 训练完成后查看六种算法总览，也可以发送「查看六种算法」或下载训练报告。
5. 选择「查询分析结果」，填写 UserID。分类查询还需要 MovieID，聚类和降维可输入多个以逗号分隔的 UserID。问题示例：
   - 「比较两种分类算法对该用户给这部电影打至少 4 分的预测。」
   - 「比较这些用户的群体归属，并标记噪声点。」
   - 「对比两种降维算法的坐标、信息保留和聚类差异。」
6. 查询完成后可在同一对话继续追问指标、参数和结果。点击「结束结果上下文」可切回普通对话。

训练也可指定其他已完成的迭代一任务 ID。页面提供高级参数输入，参数和命令行训练方式见 [迭代二说明](docs/iteration2/README.md)。

网页还支持添加项目工作区、新建与删除对话，以及切换工具审批模式。已有会话保留创建时绑定的工作目录。普通 Agent 对话中的相对文件路径和 Shell 命令以该目录为工作目录。

## 结果与数据目录

```text
governance-runs/<任务ID>/    迭代一清洗数据、隔离记录、评估报告及中间结果
analysis-runs/<任务ID>/      迭代二登记信息、模型、画像、群体、降维结果和图表
analysis-runs/latest.json    最近一次成功的迭代二训练登记
~/.agent-harness/sessions/   对话记录
~/.agent-harness/workspaces.json   网页项目工作区登记
~/.agent-harness/traces/     Agent 执行轨迹
```

网页中迭代二查询优先使用当前对话的训练结果，也可读取 `analysis-runs/latest.json` 指向的结果。使用当前清洗上下文时会核对对应版本与时间边界。Hadoop 默认文件系统为本地文件系统时，网页使用本机临时目录存放中间文件；使用 HDFS 时默认根目录为 `/freud/governance`，可通过 `GOVERNANCE_HDFS_ROOT` 调整。

## 架构与项目结构

```text
网页 React
   │ HTTP / NDJSON
   ▼
agent/web.py              网页接口与审批交互
agent/conversations.py    会话回合、工作区和追问路由
agent/workflows.py        任务、表单、追问与报告注册
agent/movielens.py        MovieLens 功能装配
   │
   ├── governance/        Hadoop 清洗与质量评估
   └── analysis/          六种算法的训练、查询和报告
```

`agent/runtime.py` 装配模型、工具和对话循环等服务。网页、终端 REPL 和批处理共享对话循环及工具执行逻辑。MovieLens 任务在 Python 进程启动时注册，网页依据任务目录生成菜单和表单。

前端参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/39163dd30f703a79079e2d2446545cff3c453738/packages/client)，复用了其主题、部分侧栏与输入框样式以及 Markdown 中文加粗规则。版权与许可见 [web/LICENSE](web/LICENSE)。

## 测试

```bash
uv run python -m unittest discover -s tests
cd web
pnpm type
pnpm build
```

自动化测试包括工具调用协议、会话和网页任务、Hadoop 工作脚本，以及小型 MovieLens 格式数据上的模型训练与查询。实际数据的评分和分析结论以对应任务生成的报告为准。

## 其他入口

终端交互和批处理可用于编程任务：

```bash
uv run freud
uv run freud --file task.md
```

`uv run freud --mock` 和 `uv run freud-web --mock` 可用于检查模拟交互；网页 Mock 模式不会运行数据任务。

## 课程文档

- [项目总体要求与汇报安排](docs/requirements/引言_项目总体要求与汇报安排.md)
- [迭代一需求](docs/requirements/迭代一_Hadoop数据清洗与Agent基础.md) · [运行与实现说明](docs/iteration1/README.md)
- [迭代二需求](docs/requirements/迭代二_分类聚类降维与分析Agent.md) · [运行与实现说明](docs/iteration2/README.md)
