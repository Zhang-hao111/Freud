"""用登记结果生成分析报告和聊天回复。"""

import json

NAMES = {
    "logistic_regression": "逻辑回归", "random_forest": "随机森林",
    "kmeans": "K-Means", "dbscan": "DBSCAN", "pca": "PCA", "truncated_svd": "Truncated SVD",
}
METRICS = {
    "accuracy": "Accuracy", "precision": "Precision", "recall": "Recall",
    "f1": "F1", "roc_auc": "ROC AUC",
}
SPLITS = {"train": "训练期 · ≤ T1", "validation": "验证期 · (T1, T2]", "test": "测试期 · > T2"}
NOISE_NOTE = "Silhouette 和 Davies–Bouldin 只在非噪声用户上计算。噪声比例高时，指标只能反映少数已分组用户。"
REDUCTION_METRIC_NOTE = (
    "保留方差指低维表示解释的原始特征方差比例。ARI 衡量两次分组对用户对是否同簇的判断有多一致，"
    "并校正随机一致性。ARI 越接近 1，分组越相似，不能换算成归属不变的用户比例。"
    "簇编号可以重排，判断某个用户的归属是否变化，需要先看两次分组的成员如何对应。"
)


def metric(value):
    return "不可评估" if value is None else str(value)


def split_lines(data, boundaries):
    return [
        f"T1 **{boundaries['T1']}**，T2 **{boundaries['T2']}**（UTC）。", "",
        "| 时间段 | 评分数 | ≥ 4 分 | < 4 分 |", "| --- | ---: | ---: | ---: |",
        *[f"| {SPLITS[name]} | {row['records']:,} | {row['positive']:,} | {row['negative']:,} |"
          for name, row in data.items()],
    ]


def classifier_metrics(items):
    return [
        "| 算法 | 评估集 | " + " | ".join(METRICS.values()) + " |",
        "| --- | --- | " + " | ".join(["---:"] * len(METRICS)) + " |",
        *[f"| {NAMES[name]} | {label} | "
          + " | ".join(metric(item[key].get(k)) for k in METRICS) + " |"
          for name, item in items.items() for label, key in
          (("验证期", "validation_metrics"), ("测试期", "test_metrics"))],
    ]


def cluster_metrics(items):
    lines = [
        "| 算法 | 群体数 | 噪声用户 | Silhouette | Davies–Bouldin |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, item in items.items():
        m = item["metrics"]
        lines.append(f"| {NAMES[name]} | {m['clusters']} | {m['noise_records']:,}/{m['records']:,}"
                     f"（{m['noise_ratio']:.2%}） | {metric(m['silhouette'])} | {metric(m['davies_bouldin'])} |")
    return lines + ["", NOISE_NOTE]


def reduction_metrics(items):
    lines = [
        "| 算法 | 维数 | 保留方差 | 聚类对照 | Silhouette 原始 → 降维 | ARI |",
        "| --- | --- | ---: | --- | --- | ---: |",
    ]
    for name, item in items.items():
        comparison = item["cluster_comparison"]
        if "original_metrics" in comparison:
            old, new = comparison["original_metrics"]["silhouette"], comparison["reduced_metrics"]["silhouette"]
        else:
            old, new = comparison["original_silhouette"], comparison["reduced_silhouette"]
        lines.append(
            f"| {NAMES[name]} | {item['original_dimensions']} → {item['actual_dimensions']} | "
            f"{item['information_retained']:.2%} | {NAMES[comparison['algorithm']]} | "
            f"{metric(old)} → {metric(new)} | {metric(comparison['adjusted_rand_index'])} |")
    lines += ["", REDUCTION_METRIC_NOTE,
              "", "| 算法 | 聚类耗时 原始 → 降维 | 已登记流程耗时 | Python 分配峰值 |",
              "| --- | --- | ---: | ---: |"]
    for name, item in items.items():
        usage = item["resource_usage"]
        stages = "未分别记录"
        if "original_clustering_seconds" in usage:
            stages = f"{usage['original_clustering_seconds']:.6f} s → {usage['reduced_clustering_seconds']:.6f} s"
        lines.append(f"| {NAMES[name]} | {stages} | {usage['runtime_seconds']:.6f} s | "
                     f"{usage['peak_memory_bytes']:,} B |")
    lines += ["", "内存峰值来自 Python tracemalloc。独立的聚类耗时只测量模型拟合与分组。"]
    if any(item["cluster_comparison"]["algorithm"] == "dbscan" for item in items.values()):
        lines += ["", NOISE_NOTE]
        for name, item in items.items():
            comparison = item["cluster_comparison"]
            if comparison["algorithm"] == "dbscan":
                old = comparison["original_metrics"]["noise_ratio"]
                new = comparison["reduced_metrics"]["noise_ratio"]
                lines.append(f"- {NAMES[name]} 的 DBSCAN 噪声比例由 {old:.2%} 变为 {new:.2%}。")
    return lines


def version_lines(items):
    lines = []
    for name, item in items.items():
        parameters = item["parameters"]
        comparison = item.get("cluster_comparison")
        if comparison and "cluster_parameters" not in parameters:
            parameters = {**parameters, "cluster_algorithm": comparison["algorithm"],
                          "cluster_parameters": (comparison["parameters"] if "parameters" in comparison
                                                 else {"n_clusters": comparison["clusters"]})}
        settings = "、".join(f"`{key}={json.dumps(value, ensure_ascii=False)}`" for key, value in parameters.items())
        lines.append(f"- {NAMES[name]} `{item.get('model_version') or item['result_version']}` · {settings}")
    return lines


def training_overview(registry):
    classification = registry["classification"]
    profiles = registry["profiles"]
    lines = [
        "### 迭代二训练已完成", "",
        "| 任务 | 算法 | 本次结果 |", "| --- | --- | --- |",
    ]
    for name, item in classification["algorithms"].items():
        lines.append(f"| 分类 | {NAMES[name]} | 验证 F1 {item['validation_metrics']['f1']:.4f} · "
                     f"测试 F1 {item['test_metrics']['f1']:.4f} |")
    for name, item in registry["clustering"]["algorithms"].items():
        m = item["metrics"]
        lines.append(f"| 聚类 | {NAMES[name]} | {m['clusters']} 个群体 · 噪声 {m['noise_ratio']:.2%} |")
    for name, item in registry["reduction"]["algorithms"].items():
        lines.append(f"| 降维 | {NAMES[name]} | {item['original_dimensions']} → {item['actual_dimensions']} 维 · "
                     f"保留方差 {item['information_retained']:.2%} |")
    lines += ["", f"分类按验证期 F1 选出 **{NAMES[classification['selected_algorithm']]}**。"
              f"共生成 {profiles['users']:,} 份用户画像，"
              f"有 {len(profiles['insufficient_user_ids']):,} 人训练期评分少于 {profiles['min_history']} 条。"]
    return "\n".join(lines)


def training_details(registry):
    source = registry["input"]
    classification = registry["classification"]
    lines = [
        f"训练任务 `{registry['task_id']}`，分析版本 `{registry['analysis_version']}`。"
        f"迭代一任务 `{source['iteration1_task_id']}`，清洗数据 `{source['data_version']}`。"
        f"规则 `{source['rule_version']}`，评分 `{source['score_version']}`。", "",
        "#### 分类对照", "",
        "逻辑回归提供线性基线，随机森林提供非线性对照。正类为发生评分时给出至少 4 分。"
        f"选型依据为 {classification['selection_basis']}。", "",
        *classifier_metrics(classification["algorithms"]),
        "", "#### 聚类对照", "",
        "K-Means 将画像划分为固定数量的群体。DBSCAN 按密度识别群体和噪声。", "",
        *cluster_metrics(registry["clustering"]["algorithms"]),
        "", "#### 降维对照", "",
        "PCA 与 Truncated SVD 都提供低维坐标和解释方差，便于对照信息保留情况。"
        + registry["reduction"]["comparison_basis"] + "。", "",
        *reduction_metrics(registry["reduction"]["algorithms"]),
        "", "#### 时间切分", "", *split_lines(registry["split"], source),
        "", "#### 实际参数与模型版本", "",
        *[line for task in ("classification", "clustering", "reduction")
          for line in version_lines(registry[task]["algorithms"])],
        "", "#### 局限", "", *[f"- {item}" for item in registry["limitations"]],
    ]
    return "\n".join(lines)


def query_markdown(job, artifact_url, include_report_link=True):
    results = {item["algorithm"]: item for item in job["results"]}
    first = job["results"][0]
    lines = []
    if job["task"] == "classification":
        lines += [
            f"### UserID {first['user_id']} · MovieID {first['movie_id']}", "",
            f"**{first['movie_title']}**。假设该用户发生评分，预测是否会给出 **≥ 4 分**。", "",
            "| 算法 | 高分概率 | 预测类别 | 分类阈值 |",
            "| --- | ---: | --- | ---: |",
            *[f"| {NAMES[name]} | {item['positive_probability']:.2%} | "
              f"{'≥ 4 分' if item['prediction'] else '< 4 分'} | {item['parameters']['threshold']:.2%} |"
              for name, item in results.items()],
        ]
        if len(results) == 2:
            left, right = results.values()
            delta = abs(left['positive_probability'] - right['positive_probability']) * 100
            agreement = "给出相同的预测类别" if left["prediction"] == right["prediction"] else "给出不同的预测类别"
            lines += ["", f"本次两种模型{agreement}，高分概率相差 **{delta:.2f} 个百分点**。"]
        lines += ["", "#### 验证与测试指标", "", *classifier_metrics(results)]
    elif job["task"] == "clustering":
        lines += ["### 用户群体与画像", "", *cluster_metrics(results)]
        for name, item in results.items():
            lines += ["", f"#### {NAMES[name]}", ""]
            groups = {}
            for user in item["users"]:
                label = user["cluster"]
                group = user["group_summary"]
                profile = user["profile"]
                groups[label] = group
                genres = "、".join(genre["genre"] for genre in profile["top_genres"]) or "暂无"
                assignment = "噪声点（未分配群体）" if user["noise"] else f"群体 {label}"
                lines.append(f"- UserID **{user['user_id']}** · {assignment}。训练期 {profile['history_count']} 条评分，"
                             f"均分 {profile['mean_rating']}，偏好类型 {genres}。")
            for label, group in groups.items():
                if label == -1:
                    continue
                genres = "、".join(genre["genre"] for genre in group["top_genres"]) or "暂无"
                examples = "、".join(str(uid) for uid in group["representative_user_ids"])
                lines.append(f"- **群体 {label}** 共 {group['size']:,} 人，偏好类型 {genres}。"
                             f"成员样例 UserID {examples}。")
    else:
        lines += ["### 降维与聚类对照", "", *reduction_metrics(results)]
        for name, item in results.items():
            comparison = item["cluster_comparison"]
            lines += ["", f"#### {NAMES[name]} 坐标及分组", "",
                      "| UserID | 降维坐标 | 原始群体 | 降维后群体 |",
                      "| --- | --- | ---: | ---: |"]
            for point, groups in zip(item["coordinates"], comparison["user_assignments"]):
                coords = ", ".join(str(n) for n in point["values"])
                old, new = groups["original_cluster"], groups["reduced_cluster"]
                lines.append(f"| {point['user_id']} | {coords} | "
                             f"{'噪声' if old == -1 else old} | {'噪声' if new == -1 else new} |")
            if item.get("plot_path"):
                lines += ["", f"![{NAMES[name]} 全量训练期画像二维图]({artifact_url}/plot/{job['task_id']}/{name})",
                          "", "图中每点对应参与实验的一份训练期画像，使用坐标前两维，按降维后的群体着色。上表列出本次查询用户。"]
    lines += ["", "#### 版本与参数", "", f"分析版本 `{first['analysis_version']}`。", "",
              *version_lines(results)]
    if job.get("input"):
        source = job["input"]
        lines += ["", f"迭代一任务 `{source['iteration1_task_id']}`，清洗数据 `{source['data_version']}`。"]
    limitations = dict.fromkeys(item for result in results.values() for item in result["limitations"])
    lines += ["", *split_lines(first["data_split"], first["time_boundaries"]),
              "", "#### 适用范围", "", *[f"- {item}" for item in limitations]]
    if include_report_link:
        lines += ["", f"[下载本次查询报告]({artifact_url}/analysis-report/{job['task_id']})"]
    return "\n".join(lines)
