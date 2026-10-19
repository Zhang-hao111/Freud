# 迭代二：分类、聚类、降维与分析 Agent

本目录只说明迭代二实现。需求原文位于 `docs/requirements/迭代二_分类聚类降维与分析Agent.md`。

## 实现范围

| 任务 | 算法一 | 算法二 | 主要实际输出 |
| --- | --- | --- | --- |
| 分类 | Logistic Regression | Random Forest | 类别、正类概率、模型版本、参数、验证与测试指标 |
| 聚类 | K-Means | DBSCAN | 群体归属、群体摘要、噪声标记、结果版本、参数、指标 |
| 降维 | PCA | Truncated SVD | 坐标、维数、信息保留、聚类对照、资源开销、SVG 图 |

仓库中没有随附课程 PPT 文件，因此实现采用上述六种标准算法。选择依据是：逻辑回归提供可复现的线性基线，随机森林提供非线性对照；K-Means 提供固定群体划分，DBSCAN 提供密度聚类和噪声点；PCA 与 Truncated SVD 都能记录解释方差，并适合对用户画像做低维表示。

## 前置条件

训练入口只接受迭代一成功任务目录。该目录必须包含：

- `report.json`，且状态为 `completed`；
- `data_version`、`rule_version`、`score_version`、`T1`、`T2` 和三份清洗文件哈希；
- `cleaned/users.dat`、`cleaned/movies.dat`、`cleaned/ratings.dat`；
- 与报告完全一致的数据版本和 SHA-256。

任何文件缺失、哈希不一致、版本不一致、空时间段或非法记录都会终止训练并写入明确失败信息，不会生成替代结果。

## 运行方式

安装或同步依赖：

```bash
uv sync
```

训练并登记六种算法的实际结果：

```bash
uv run freud-analysis-train \
  --iteration1-run governance-runs/<迭代一任务ID> \
  --output analysis-runs \
  --min-history 2 \
  --clusters 3 \
  --dbscan-eps 1.5 \
  --dbscan-min-samples 2 \
  --target-dimensions 2
```

启动分析 Agent 和前端：

```bash
uv run freud-analysis --results analysis-runs --host 127.0.0.1 --port 8865
```

浏览器访问 `http://127.0.0.1:8865/`。该功能不调用外部大模型，也不消耗大模型 API Token；自然语言任务识别和追问均只基于已登记结果。

## 数据与评价口径

- 训练期：评分时间 `<= T1`。
- 验证期：`T1 < 评分时间 <= T2`，只用于分类算法选择和参数判断。
- 测试期：评分时间 `> T2`，只在方案确定后做最终评价。
- 分类正类：`Rating >= 4`。
- 分类事件特征严格按时间顺序构造，每条事件只读取该事件之前的统计。
- 在线分类查询只使用截至 `T1` 的历史状态。
- 用户画像只由训练期评分生成；训练期历史少于 `min_history` 的用户不进入聚类和降维。
- 分类记录 Accuracy、Precision、Recall、F1、ROC-AUC 和混淆矩阵。
- 聚类记录群体数、噪声数/比例、Silhouette 和 Davies-Bouldin；指标不适用时为 `null`。
- 降维记录解释方差比例、原始/降维聚类指标、ARI、耗时和峰值追踪内存。

## 查询能力

未指定算法时，Agent 对同一任务调用两种算法；指定算法时只调用该工具。

- 分类需要 `UserID` 和 `MovieID`。
- 聚类需要一个或多个 `UserID`，可为 K-Means 指定 `n_clusters`、`n_init`，或为 DBSCAN 指定 `eps`、`min_samples`。
- 降维需要一个或多个 `UserID`，可指定 `target_dimensions` 或 `information_threshold`，两者不能同时指定；还可指定 K-Means 或 DBSCAN 作为压缩前后对照算法。

自定义参数会触发真实拟合，并将模型、结果 JSON 和图表登记到本次分析目录的 `queries/<结果版本>/`，不会返回示例结果。

## 结果目录

```text
analysis-runs/
├── latest.json
└── <任务ID>/
    ├── registry.json
    ├── report.md
    ├── feature-state.json
    ├── profiles.json
    ├── models/
    ├── clusters/
    ├── reductions/
    ├── plots/
    └── queries/              # 仅在使用自定义参数后出现
```

`registry.json` 是静态训练结果的登记入口，`latest.json` 指向最近一次成功任务。Agent 会检查登记状态和文件路径；模型、结果或用户不可用时返回明确失败状态。

## 测试说明

专项测试命令：

```bash
uv run python -m unittest tests.test_analysis -v
```

完整回归命令：

```bash
uv run python -m unittest discover -s tests -v
```

测试使用带真实哈希、数据版本和 `T1/T2` 的小型 MovieLens 格式数据，实际运行 `scikit-learn` 训练、预测、聚类和降维，不使用 mock 模型。该数据用于控制资源开销和验证行为，不代表 MovieLens 1M 全量评价结果；全量指标必须以用户自己的迭代一全量成功产物重新训练后生成。

文件作用、完整处理流程、接口、测试矩阵和视频演示顺序见 `docs/iteration2/files-flow-and-testing.md`。
