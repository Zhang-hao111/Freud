# 迭代一：MovieLens 1M Hadoop 数据治理

需求原文见 [迭代一：Agent 驱动的 Hadoop 数据清洗与五维质量评估](../requirements/迭代一_Hadoop数据清洗与Agent基础.md)，项目总体要求与汇报安排见 [项目引言](../requirements/引言_项目总体要求与汇报安排.md)。

逐文件作用、完整数据流、严格验收记录和约 10 分钟视频台本见 [迭代一文件、流程、验收与视频演示指南](files-flow-and-video.md)；10 分钟汇报的速览要点（实测数字、讲解话术、时间分配）见 [汇报要点](汇报要点.md)。

## 运行

需要 Python 3.11+、可访问 HDFS 的 Hadoop 3 集群、Hadoop Streaming jar，以及集群工作节点上的 `python3`。运行环境必须能执行 `hadoop fs` 和 `hadoop jar`。原始数据不随源码分发；将 GroupLens 的 `users.dat`、`movies.dat`、`ratings.dat` 原样放在 `ml-1m/ml-1m/`，保留 ISO-8859-1 编码与无表头格式。

```bash
export HADOOP_STREAMING_JAR=/path/to/hadoop-streaming-3.x.x.jar
uv sync
uv run freud-governance --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/`，输入清洗及评估需求即可启动完整任务。也可通过 `--source`、`--output`、`--hadoop`、`--hdfs-root`、`--python-command` 配置路径和命令。Web API：

| 接口 | 用途 |
| --- | --- |
| `POST /api/jobs`，`{"prompt":"请清洗并评估 MovieLens 1M"}` | 启动任务，返回任务标识与运行状态；可附带 `source_version`、`rule_version`、`score_version` |
| `GET /api/jobs/{id}` | 查询阶段、失败原因及完成后的实际报告 |
| `POST /api/jobs/{id}/ask`，`{"question":"唯一性如何变化？"}` | 基于该报告追问 |
| `GET /api/jobs/{id}/report` | 下载 Markdown 报告 |
| `GET /api/jobs/{id}/sample` | 查看清洗后的评分样例 |

仅登记了一套默认清洗和评分方案；未指定版本时使用默认方案，指定版本必须匹配已登记版本。原始数据版本不匹配时任务失败。服务会明确报告 Hadoop 缺失、作业失败或输出为空，不会生成模拟分数。追问优先由大模型基于任务报告生成（报告 JSON 是唯一事实源），未配置 API Key 或调用失败时自动回退为确定性模板回答；两种模式都只引用报告内事实，`--no-llm` 可强制模板模式。

## 数据流与产物

1. 上传原始三个 `.dat` 和 Streaming 工作脚本到本次任务的 HDFS 目录；原始文件以 SHA-256 标识。
2. Hadoop `audit-map/audit-reduce` 检查字段、类别、时间、关联和业务键重复或冲突。原始文本以 ISO-8859-1 解码，评分键为 `(UserID, MovieID, Timestamp)`，不同时间的评分保留。
3. Hadoop `score-map/score-combine/score-reduce` 计算清洗前五维得分、异常计数及时间分布。
4. Hadoop `clean-map` 先处理实体表，再使用保留的实体 ID 集合复核评分关联。修复可确定的格式问题；完全相同的业务记录去重；无法安全修复的字段错误、冲突和孤立引用隔离。不会猜测缺失值或替冲突记录任意选择赢家。
5. 清洗产物再次上传 HDFS，以完全相同的检查与评分程序复评。根据清洗后有效评分的时间分布，取 80% 和 90% 的时间分位日末分别为 `T1/T2`。若无法形成严格递增的边界，任务失败。
6. `governance-runs/{task_id}/` 保存 `cleaned/*.dat`、`cleaned/quarantine.jsonl`、`report.json`、`report.md`、中间作业结果与失败信息。HDFS 路径记在报告中。后续迭代应同时核对 `data_version`、`rule_version`、`T1/T2`。

Web 任务在后台运行并逐阶段更新状态。页面展示真实五维分数、数量、异常样例、版本、时间边界与报告下载；追问优先由大模型基于保存的报告生成，无法回答报告未包含的事实。

## 评分口径

每个维度为 `100 × 通过规则的记录数 / 适用记录数`，单位 0–100 分。清洗前后运行同一 Hadoop 评分代码。空分母返回 `null`，不会伪造 100 分。

| 维度 | 通过条件与范围 |
| --- | --- |
| Accurate | 必填、类型、值域、时间范围符合 MovieLens 说明；仅代表可验证的表面正确性，不能核验现实真实性 |
| Complete | 三表必填字段齐全、非空且字段数量正确 |
| Unique | 业务键无重复或冲突；相同用户与电影的不同时间评分不是重复 |
| Up-to-date | 仅评分表，时间戳落在 2002-02-28 00:00:00 至 2003-02-28 23:59:59 UTC；参照数据集发布时期，不评价今日时效 |
| Consistent | 字段类型、类别、格式、评分外键与同业务键内容无矛盾 |

修复包括边缘空白与重复类型标签等确定性规范化。异常标签可重叠，所以各标签计数之和未必等于处置记录数。邮编按字符串保留前导零。电影标题缺年份、同名电影、用户自报属性真实性与评分行为异常仅列为局限，不自动改写或删除。历史基准数据年代久远也不等于数据错误。

## 设计调研

- [Apache Hadoop Streaming](https://hadoop.apache.org/docs/stable/hadoop-streaming/HadoopStreaming.html)（[源码仓库](https://github.com/apache/hadoop)）：使用标准输入输出的 mapper/reducer、`-files` 分发工作脚本及关联文件、`mapreduce.job.reduces=0` 运行清洗映射；实际评分和处置均在 Hadoop 作业中执行。
- [Deequ](https://github.com/awslabs/deequ)：借鉴其显式定义完整性、唯一性和值域检查、结构化验证结果及行级通过/失败证据的思路。这里选择 Hadoop Streaming 实现，以满足本轮指定的执行平台。
- [Great Expectations](https://github.com/fivetran/great_expectations)：借鉴可解释的验证结果与自动生成文档；报告同时保存规则、计数、样例和未验证范围，Agent 追问按这些结构化结果组合回答。
- [Soda Core](https://github.com/sodadata/soda-core)：借鉴数据契约与入口校验思路；Web API 明确要求 JSON 对象，未登记配置和错误载荷在任务执行前失败。
- [OpenLineage](https://github.com/OpenLineage/OpenLineage)：借鉴 `run/job/dataset` 的可追溯标识；每次运行保留任务 ID、原始与清洗数据哈希、规则版本、HDFS 路径和时间边界。

Hadoop shuffle 不保证同一键下记录的到达顺序。为使同一数据版本和规则版本产生稳定的处置统计，reducer 会按异常严重程度、异常集合、规范值和原始值确定性排序，优先保留问题最少的规范记录，再将其余同值记录标记为重复。

## 验证与限制

`uv run python -m unittest tests.test_governance -v` 使用含重复、值域错误、孤立引用和冲突的微型数据验证 Streaming 工作脚本。完整端到端实验必须在可用 Hadoop 集群上运行；没有成功的 Hadoop 作业时，不应把本地单元测试当作实际 MovieLens 评分。当前代码不会把未运行的实验值写入报告。

2026-09-29 已在本机 Hadoop 3.4.1 本地模式完成全量端到端运行（1,010,132 条记录，76 秒，任务 `559e89ba341a43cbb212c91e65500fea`），结果与严格验收结论见 files-flow-and-video.md 第 5 节；分布式 HDFS/YARN 仍未验证。

## 本机演示启动（备查）

```bash
source /home/h/hadoop-env.sh
cd /home/h/h/大数据分析/Lab-coding-agent
.venv/bin/python -m governance.web --host 127.0.0.1 --port 8765 --hdfs-root /home/h/hadoop-local-fs/governance
```