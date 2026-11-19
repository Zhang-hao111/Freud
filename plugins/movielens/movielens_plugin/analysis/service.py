"""调用已登记的分析工具，并从实际结果回答常见追问。"""

import json
import uuid
from pathlib import Path

from movielens_plugin.analysis.reporting import classifier_metrics, cluster_metrics, reduction_metrics, split_lines, version_lines
from movielens_plugin.analysis.tools import AnalysisRegistry, tool_catalog


TASK_ALGORITHMS = {
    "classification": ("logistic_regression", "random_forest"),
    "clustering": ("kmeans", "dbscan"),
    "reduction": ("pca", "truncated_svd"),
}
ALIASES = {
    "逻辑回归": "logistic_regression", "logistic": "logistic_regression",
    "随机森林": "random_forest", "random forest": "random_forest",
    "k-means": "kmeans", "kmeans": "kmeans", "k 均值": "kmeans",
    "dbscan": "dbscan", "pca": "pca", "主成分": "pca",
    "truncated svd": "truncated_svd", "svd": "truncated_svd",
}


def infer_task(prompt):
    lowered = prompt.lower()
    if any(word in lowered for word in ("降维", "压缩", "二维", "pca", "svd")):
        return "reduction"
    if any(word in lowered for word in ("聚类", "群体", "kmeans", "k-means", "dbscan")):
        return "clustering"
    if any(word in lowered for word in ("分类", "预测", "高分", "不低于 4", "不低于4", ">=4")):
        return "classification"
    raise ValueError("请明确提出分类预测、用户聚类或降维对照问题")


def requested_algorithm(prompt, explicit, task):
    if explicit is not None:
        normalized = str(explicit).strip().lower().replace("-", "_").replace(" ", "_")
        normalized = ALIASES.get(str(explicit).strip().lower(), normalized)
        if normalized not in TASK_ALGORITHMS[task]:
            raise ValueError(f"{task} 未实现算法：{explicit}")
        return [normalized]
    lowered = prompt.lower()
    matches = []
    for alias, algorithm in ALIASES.items():
        if alias in lowered and algorithm in TASK_ALGORITHMS[task] and algorithm not in matches:
            matches.append(algorithm)
    return matches or list(TASK_ALGORITHMS[task])


def grounded_follow_up(job, question):
    if not isinstance(question, str) or not question.strip():
        return "请输入关于本次实际分析结果的问题。"
    question = question.strip()
    results = job["results"]
    if any(word in question for word in ("局限", "可信", "不能", "推测", "未验证")):
        limitations = []
        for result in results:
            for item in result.get("limitations", []):
                if item not in limitations:
                    limitations.append(item)
        return "本次结果局限：" + "；".join(limitations)
    items = {item["algorithm"]: item for item in results}
    if any(word in question.lower() for word in ("版本", "参数", "t1", "t2", "时间", "切分")):
        first = results[0]
        return "\n".join([f"分析版本 `{first['analysis_version']}`。", "", *version_lines(items), "",
                          *split_lines(first["data_split"], first["time_boundaries"])])
    if any(word in question for word in ("指标", "评价", "效果", "对比", "差异")):
        render = {"classification": classifier_metrics, "clustering": cluster_metrics,
                  "reduction": reduction_metrics}[job["task"]]
        return "实际登记的评价结果\n\n" + "\n".join(render(items))
    if job["task"] == "classification":
        return "；".join(
            f"{item['algorithm']} 对 UserID {item['user_id']}、MovieID {item['movie_id']} 的预测类别为 "
            f"{item['prediction']}，高分概率 {item['positive_probability']}"
            for item in results)
    if job["task"] == "clustering":
        return "；".join(
            f"{item['algorithm']}：" + "，".join(
                f"UserID {user['user_id']} 属于群体 {user['cluster']}"
                + ("（噪声点）" if user["noise"] else "") for user in item["users"])
            for item in results)
    return "；".join(
        f"{item['algorithm']} 将 {item['original_dimensions']} 维压缩为 {item['actual_dimensions']} 维，"
        f"信息保留 {item['information_retained']}，聚类 ARI {item['cluster_comparison']['adjusted_rand_index']}"
        for item in results)


class AnalysisService:
    def __init__(self, results_path, expected_input=None):
        self.results_path = Path(results_path)
        self.expected_input = expected_input

    def run(self, prompt, user_id=None, movie_id=None, user_ids=None, algorithm=None,
            parameters=None, target_dimensions=None, information_threshold=None,
            cluster_algorithm=None, cluster_parameters=None, *, on_progress=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请输入自然语言分析问题")
        task = infer_task(prompt)
        algorithms = requested_algorithm(prompt, algorithm, task)
        if task == "classification":
            if user_id is None and isinstance(user_ids, list) and len(user_ids) == 1:
                user_id = user_ids[0]
            if user_id is None or movie_id is None:
                raise ValueError("分类任务必须提供 UserID 和 MovieID")
            arguments = {"user_id": int(user_id), "movie_id": int(movie_id)}
        else:
            selected = user_ids if user_ids is not None else ([user_id] if user_id is not None else None)
            if not isinstance(selected, list) or not selected:
                raise ValueError("聚类和降维任务必须提供非空用户集合")
            arguments = {"user_ids": [int(value) for value in selected]}
            if task == "clustering" and parameters is not None:
                arguments["parameters"] = parameters
            if task == "reduction":
                for name, value in (("target_dimensions", target_dimensions),
                                    ("information_threshold", information_threshold),
                                    ("cluster_algorithm", cluster_algorithm),
                                    ("cluster_parameters", cluster_parameters)):
                    if value is not None:
                        arguments[name] = value
        phase = "加载已登记结果"
        if on_progress:
            on_progress(phase)
        try:
            registry = AnalysisRegistry(self.results_path)
            registered_input = registry.data["input"]
            if self.expected_input and any(registered_input.get(key) != self.expected_input[key]
                                           for key in ("data_version", "cleaned_sha256", "T1", "T2")):
                raise ValueError("模型引用的数据版本或 T1/T2 与当前清洗结果不一致，请先用该清洗结果训练")
            tools = tool_catalog(registry)
            phase = "调用分析工具"
            if on_progress:
                on_progress(phase)
            results = []
            for algorithm in algorithms:
                result = tools[algorithm].execute(arguments)
                if not result.success:
                    raise ValueError(result.error)
                results.append(json.loads(result.data))
        except (ValueError, FileNotFoundError) as error:
            raise ValueError(f"{phase}失败：{error}") from error

        if on_progress:
            on_progress("已完成")
        return {"task_id": uuid.uuid4().hex, "status": "completed", "task": task,
                "algorithms": algorithms, "results": results, "input": registered_input,
                "registry_dir": str(registry.run_dir)}
