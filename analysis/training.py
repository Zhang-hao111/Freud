"""Train and register iteration-two MovieLens analysis artifacts."""

import argparse
import hashlib
import json
import time
import tracemalloc
import uuid
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.cluster import DBSCAN, KMeans
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, adjusted_rand_score, confusion_matrix,
                             davies_bouldin_score, f1_score, precision_score,
                             recall_score, roc_auc_score, silhouette_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from analysis.data import (FEATURE_VERSION, PROFILE_VERSION, IterationOneDataset,
                           event_features, user_profiles)


CLASSIFIERS = ("logistic_regression", "random_forest")
CLUSTERERS = ("kmeans", "dbscan")
REDUCERS = ("pca", "truncated_svd")
EXPERIMENT_VERSION = "ml1m-analysis-experiment-v1"
RANDOM_STATE = 20260930


def json_ready(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def classification_metrics(target, probability):
    prediction = (probability >= 0.5).astype(int)
    result = {
        "records": int(len(target)),
        "accuracy": round(float(accuracy_score(target, prediction)), 6),
        "precision": round(float(precision_score(target, prediction, zero_division=0)), 6),
        "recall": round(float(recall_score(target, prediction, zero_division=0)), 6),
        "f1": round(float(f1_score(target, prediction, zero_division=0)), 6),
        "confusion_matrix": confusion_matrix(target, prediction, labels=[0, 1]).tolist(),
        "roc_auc": None,
    }
    if len(set(target.tolist())) == 2:
        result["roc_auc"] = round(float(roc_auc_score(target, probability)), 6)
    return result


def clustering_metrics(matrix, labels):
    labels = np.asarray(labels)
    non_noise = labels >= 0
    cluster_count = len(set(labels[non_noise].tolist()))
    result = {
        "records": int(len(labels)),
        "clusters": int(cluster_count),
        "noise_records": int(np.sum(~non_noise)),
        "noise_ratio": round(float(np.mean(~non_noise)), 6),
        "silhouette": None,
        "davies_bouldin": None,
    }
    if cluster_count >= 2 and np.sum(non_noise) > cluster_count:
        result["silhouette"] = round(float(silhouette_score(matrix[non_noise], labels[non_noise])), 6)
        result["davies_bouldin"] = round(float(davies_bouldin_score(matrix[non_noise], labels[non_noise])), 6)
    return result


def cluster_summaries(labels, profiles):
    members = defaultdict(list)
    for user_id, label in zip(profiles["user_ids"], labels):
        members[int(label)].append(user_id)
    summaries = {}
    for label, user_ids in sorted(members.items()):
        genre_scores = defaultdict(list)
        for user_id in user_ids:
            for item in profiles["details"][str(user_id)]["top_genres"]:
                genre_scores[item["genre"]].append(item["mean_rating"])
        top_genres = sorted(
            ((genre, sum(values) / len(values)) for genre, values in genre_scores.items()),
            key=lambda item: (-item[1], item[0]))[:3]
        summaries[str(label)] = {
            "label": label,
            "noise": label == -1,
            "size": len(user_ids),
            "representative_user_ids": user_ids[:5],
            "top_genres": [{"genre": genre, "mean_rating": round(score, 4)}
                           for genre, score in top_genres],
            "description": ("噪声点：与已识别群体均不接近" if label == -1 else
                            "偏好类型：" + "、".join(genre for genre, _ in top_genres)),
        }
    return summaries


def svg_plot(user_ids, coordinates, labels, title):
    width, height, margin = 760, 480, 48
    coordinates = np.asarray(coordinates, dtype=float)
    if coordinates.shape[1] == 1:
        coordinates = np.column_stack((coordinates[:, 0], np.zeros(len(coordinates))))
    minimum = coordinates.min(axis=0)
    maximum = coordinates.max(axis=0)
    span = np.where(maximum > minimum, maximum - minimum, 1.0)
    scaled = (coordinates - minimum) / span
    colors = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2")
    circles = []
    for user_id, point, label in zip(user_ids, scaled, labels):
        x_value = margin + point[0] * (width - 2 * margin)
        y_value = height - margin - point[1] * (height - 2 * margin)
        color = "#64748b" if int(label) == -1 else colors[int(label) % len(colors)]
        circles.append(
            f'<circle cx="{x_value:.2f}" cy="{y_value:.2f}" r="5" fill="{color}">'
            f'<title>UserID {user_id}，群体 {int(label)}</title></circle>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
            '<rect width="100%" height="100%" fill="white"/>'
            f'<text x="{margin}" y="28" font-family="sans-serif" font-size="18">{title}</text>'
            f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="#94a3b8"/>'
            f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="#94a3b8"/>'
            + "".join(circles) + '</svg>')


class AnalysisTrainer:
    def __init__(self, iteration1_run, output_dir, min_history=2, clusters=3,
                 dbscan_eps=1.5, dbscan_min_samples=2, target_dimensions=2):
        self.dataset = IterationOneDataset(iteration1_run)
        self.output_dir = Path(output_dir).resolve()
        self.min_history = int(min_history)
        self.clusters = int(clusters)
        self.dbscan_eps = float(dbscan_eps)
        self.dbscan_min_samples = int(dbscan_min_samples)
        self.target_dimensions = int(target_dimensions)
        if self.min_history < 1:
            raise ValueError("min_history 必须大于 0")
        if self.clusters < 2:
            raise ValueError("clusters 必须至少为 2")
        if self.target_dimensions < 2:
            raise ValueError("target_dimensions 必须至少为 2")

    def run(self, task_id=None, on_progress=None):
        run_id = task_id or uuid.uuid4().hex
        run_dir = self.output_dir / run_id
        run_dir.mkdir(parents=True)
        for directory in ("models", "clusters", "reductions", "plots"):
            (run_dir / directory).mkdir()
        phase = "准备训练"

        def notify(value):
            nonlocal phase
            phase = value
            if on_progress:
                on_progress(value)

        try:
            notify("构造时间安全的分类特征")
            matrices, targets, feature_state = event_features(self.dataset)
            self._validate_classification_targets(targets)
            (run_dir / "feature-state.json").write_text(
                json.dumps(json_ready(feature_state), ensure_ascii=False, indent=2), encoding="utf-8")
            notify("训练并评价两种分类算法")
            classification = self._train_classifiers(run_dir, matrices, targets)
            notify("构建训练期用户画像")
            profiles = user_profiles(self.dataset, self.min_history)
            profile_payload = {key: value for key, value in profiles.items() if key != "matrix"}
            profile_payload["vectors"] = {str(user_id): profiles["matrix"][index].tolist()
                                          for index, user_id in enumerate(profiles["user_ids"])}
            (run_dir / "profiles.json").write_text(
                json.dumps(json_ready(profile_payload), ensure_ascii=False, indent=2), encoding="utf-8")
            notify("运行两种聚类算法")
            clustering, scaled_profiles, profile_scaler = self._train_clusters(run_dir, profiles)
            notify("运行两种降维与聚类对照")
            reduction = self._train_reducers(run_dir, profiles, scaled_profiles, profile_scaler)
            analysis_version = hashlib.sha256(json.dumps({
                "data_version": self.dataset.report["data_version"],
                "experiment_version": EXPERIMENT_VERSION,
                "feature_version": FEATURE_VERSION,
                "profile_version": PROFILE_VERSION,
                "parameters": {"min_history": self.min_history, "clusters": self.clusters,
                               "dbscan_eps": self.dbscan_eps,
                               "dbscan_min_samples": self.dbscan_min_samples,
                               "target_dimensions": self.target_dimensions},
            }, sort_keys=True).encode()).hexdigest()[:16]
            registry = {
                "status": "completed", "task_id": run_id, "analysis_version": analysis_version,
                "experiment_version": EXPERIMENT_VERSION,
                "input": {"iteration1_task_id": self.dataset.report.get("task_id"),
                          "data_version": self.dataset.report["data_version"],
                          "rule_version": self.dataset.report["rule_version"],
                          "score_version": self.dataset.report["score_version"],
                          "T1": self.dataset.report["T1"], "T2": self.dataset.report["T2"],
                          "cleaned_sha256": self.dataset.report["cleaned_sha256"]},
                "split": self.dataset.split_summary(),
                "classification": classification,
                "clustering": clustering,
                "reduction": reduction,
                "profiles": {"version": PROFILE_VERSION, "users": len(profiles["user_ids"]),
                             "original_dimensions": len(profiles["feature_names"]),
                             "min_history": self.min_history,
                             "insufficient_user_ids": profiles["insufficient_user_ids"]},
                "limitations": [
                    "分类是基于历史数据的概率预测，不是用户未来行为事实。",
                    "事件特征只使用该事件发生前的统计；在线查询固定使用 T1 以内历史，避免测试期信息进入模型输入。",
                    "用户画像只使用训练期评分；历史少于阈值的用户不参与聚类和降维。",
                    "群体摘要是统计描述，不代表用户身份、意图或因果关系。",
                    "二维图仅用于展示结构，压缩会丢失未被保留的信息。",
                ],
            }
            (run_dir / "registry.json").write_text(
                json.dumps(json_ready(registry), ensure_ascii=False, indent=2), encoding="utf-8")
            (run_dir / "report.md").write_text(self.markdown(registry), encoding="utf-8")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "latest.json").write_text(
                json.dumps({"task_id": run_id, "analysis_version": analysis_version}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            notify("已完成")
            return registry
        except Exception as error:
            (run_dir / "failure.json").write_text(json.dumps(
                {"status": "failed", "task_id": run_id, "phase": phase, "error": str(error)},
                ensure_ascii=False, indent=2), encoding="utf-8")
            raise

    @staticmethod
    def _validate_classification_targets(targets):
        for name, values in targets.items():
            if len(set(values.tolist())) < 2:
                raise ValueError(f"{name} 时间段缺少正类或负类，无法进行可靠分类评价")

    def _train_classifiers(self, run_dir, matrices, targets):
        definitions = {
            "logistic_regression": (Pipeline([
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=1000, class_weight="balanced",
                                             random_state=RANDOM_STATE)),
            ]), {"max_iter": 1000, "class_weight": "balanced", "threshold": 0.5}),
            "random_forest": (RandomForestClassifier(
                n_estimators=200, max_depth=12, min_samples_leaf=2,
                class_weight="balanced", random_state=RANDOM_STATE, n_jobs=1),
                {"n_estimators": 200, "max_depth": 12, "min_samples_leaf": 2,
                 "class_weight": "balanced", "threshold": 0.5}),
        }
        results = {}
        for name, (model, parameters) in definitions.items():
            started = time.perf_counter()
            model.fit(matrices["train"], targets["train"])
            validation_probability = model.predict_proba(matrices["validation"])[:, 1]
            validation = classification_metrics(targets["validation"], validation_probability)
            test_probability = model.predict_proba(matrices["test"])[:, 1]
            test = classification_metrics(targets["test"], test_probability)
            model_path = run_dir / "models" / f"{name}.joblib"
            joblib.dump(model, model_path)
            model_version = hashlib.sha256(
                f"{self.dataset.report['data_version']}:{FEATURE_VERSION}:{name}:{parameters}".encode()).hexdigest()[:16]
            results[name] = {
                "algorithm": name, "model_version": model_version,
                "model_path": str(model_path.relative_to(run_dir)), "parameters": parameters,
                "validation_metrics": validation, "test_metrics": test,
                "training_seconds": round(time.perf_counter() - started, 6),
                "selection_score": validation["f1"],
                "feature_version": FEATURE_VERSION,
                "training_cutoff": self.dataset.report["T1"],
            }
        selected = sorted(results, key=lambda name: (-results[name]["selection_score"], name))[0]
        return {"algorithms": results, "selected_algorithm": selected,
                "selection_basis": "验证期 F1 最高；并列时按算法名确定，测试期不参与选择",
                "positive_class": "Rating>=4"}

    def _train_clusters(self, run_dir, profiles):
        scaler = StandardScaler()
        scaled = scaler.fit_transform(profiles["matrix"])
        cluster_count = min(self.clusters, len(profiles["user_ids"]) - 1)
        definitions = {
            "kmeans": (KMeans(n_clusters=cluster_count, n_init=20, random_state=RANDOM_STATE),
                       {"n_clusters": cluster_count, "n_init": 20, "random_state": RANDOM_STATE}),
            "dbscan": (DBSCAN(eps=self.dbscan_eps, min_samples=self.dbscan_min_samples),
                       {"eps": self.dbscan_eps, "min_samples": self.dbscan_min_samples}),
        }
        results = {}
        for name, (model, parameters) in definitions.items():
            started = time.perf_counter()
            labels = model.fit_predict(scaled)
            model_path = run_dir / "models" / f"{name}.joblib"
            joblib.dump({"scaler": scaler, "model": model}, model_path)
            result_path = run_dir / "clusters" / f"{name}.json"
            payload = {
                "algorithm": name,
                "result_version": hashlib.sha256(
                    f"{self.dataset.report['data_version']}:{PROFILE_VERSION}:{name}:{parameters}".encode()).hexdigest()[:16],
                "parameters": parameters,
                "metrics": clustering_metrics(scaled, labels),
                "assignments": {str(user_id): int(label)
                                for user_id, label in zip(profiles["user_ids"], labels)},
                "summaries": cluster_summaries(labels, profiles),
                "model_path": str(model_path.relative_to(run_dir)),
                "runtime_seconds": round(time.perf_counter() - started, 6),
                "profile_version": PROFILE_VERSION,
                "training_cutoff": self.dataset.report["T1"],
            }
            result_path.write_text(json.dumps(json_ready(payload), ensure_ascii=False, indent=2), encoding="utf-8")
            results[name] = {key: value for key, value in payload.items()
                             if key not in {"assignments", "summaries"}}
            results[name]["result_path"] = str(result_path.relative_to(run_dir))
        return {"algorithms": results}, scaled, scaler

    def _train_reducers(self, run_dir, profiles, scaled_profiles, profile_scaler):
        maximum = min(self.target_dimensions, scaled_profiles.shape[1], len(scaled_profiles) - 1)
        cluster_count = min(self.clusters, len(profiles["user_ids"]) - 1)
        original_cluster = KMeans(n_clusters=cluster_count, n_init=20,
                                  random_state=RANDOM_STATE).fit_predict(scaled_profiles)
        definitions = {
            "pca": PCA(n_components=maximum, random_state=RANDOM_STATE),
            "truncated_svd": TruncatedSVD(n_components=maximum, random_state=RANDOM_STATE),
        }
        results = {}
        for name, model in definitions.items():
            tracemalloc.start()
            started = time.perf_counter()
            coordinates = model.fit_transform(scaled_profiles)
            reduced_cluster = KMeans(n_clusters=cluster_count, n_init=20,
                                     random_state=RANDOM_STATE).fit_predict(coordinates)
            elapsed = time.perf_counter() - started
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            information = float(np.sum(model.explained_variance_ratio_))
            model_path = run_dir / "models" / f"{name}.joblib"
            joblib.dump({"scaler": profile_scaler, "model": model}, model_path)
            plot_path = run_dir / "plots" / f"{name}.svg"
            plot_path.write_text(svg_plot(profiles["user_ids"], coordinates, reduced_cluster,
                                          f"{name} 用户画像二维表示"), encoding="utf-8")
            result_path = run_dir / "reductions" / f"{name}.json"
            parameters = {"target_dimensions": self.target_dimensions,
                          "actual_dimensions": int(coordinates.shape[1]),
                          "random_state": RANDOM_STATE}
            payload = {
                "algorithm": name,
                "result_version": hashlib.sha256(
                    f"{self.dataset.report['data_version']}:{PROFILE_VERSION}:{name}:{parameters}".encode()).hexdigest()[:16],
                "parameters": parameters,
                "original_dimensions": int(scaled_profiles.shape[1]),
                "actual_dimensions": int(coordinates.shape[1]),
                "information_retained": round(information, 6),
                "coordinates": {str(user_id): [round(float(value), 8) for value in row]
                                for user_id, row in zip(profiles["user_ids"], coordinates)},
                "cluster_comparison": {
                    "algorithm": "kmeans", "clusters": cluster_count,
                    "original_silhouette": clustering_metrics(scaled_profiles, original_cluster)["silhouette"],
                    "reduced_silhouette": clustering_metrics(coordinates, reduced_cluster)["silhouette"],
                    "adjusted_rand_index": round(float(adjusted_rand_score(original_cluster, reduced_cluster)), 6),
                },
                "original_cluster_assignments": {
                    str(user_id): int(label)
                    for user_id, label in zip(profiles["user_ids"], original_cluster)
                },
                "reduced_cluster_assignments": {
                    str(user_id): int(label)
                    for user_id, label in zip(profiles["user_ids"], reduced_cluster)
                },
                "resource_usage": {"runtime_seconds": round(elapsed, 6),
                                   "peak_memory_bytes": int(peak_bytes)},
                "model_path": str(model_path.relative_to(run_dir)),
                "plot_path": str(plot_path.relative_to(run_dir)),
                "profile_version": PROFILE_VERSION,
                "training_cutoff": self.dataset.report["T1"],
            }
            result_path.write_text(json.dumps(json_ready(payload), ensure_ascii=False, indent=2), encoding="utf-8")
            results[name] = {key: value for key, value in payload.items()
                             if key not in {"coordinates", "original_cluster_assignments",
                                            "reduced_cluster_assignments"}}
            results[name]["result_path"] = str(result_path.relative_to(run_dir))
        return {"algorithms": results,
                "comparison_basis": "同一训练期画像、同一 K-Means 参数，对照原始标准化画像与降维坐标"}

    @staticmethod
    def markdown(registry):
        lines = ["# MovieLens 迭代二分析报告", "", f"任务：{registry['task_id']}",
                 f"分析版本：{registry['analysis_version']}",
                 f"数据版本：{registry['input']['data_version']}",
                 f"T1：{registry['input']['T1']}；T2：{registry['input']['T2']}", "",
                 "## 时间切分", "", "| 阶段 | 记录 | 正类 | 负类 |", "| --- | ---: | ---: | ---: |"]
        for name, values in registry["split"].items():
            lines.append(f"| {name} | {values['records']} | {values['positive']} | {values['negative']} |")
        lines += ["", "## 分类", "", "| 算法 | 验证 F1 | 测试 F1 | 测试 AUC |", "| --- | ---: | ---: | ---: |"]
        for name, item in registry["classification"]["algorithms"].items():
            lines.append(f"| {name} | {item['validation_metrics']['f1']} | {item['test_metrics']['f1']} | {item['test_metrics']['roc_auc']} |")
        lines += ["", "## 聚类", "", "| 算法 | 群体数 | 噪声比例 | Silhouette |", "| --- | ---: | ---: | ---: |"]
        for name, item in registry["clustering"]["algorithms"].items():
            metrics = item["metrics"]
            lines.append(f"| {name} | {metrics['clusters']} | {metrics['noise_ratio']} | {metrics['silhouette']} |")
        lines += ["", "## 降维", "", "| 算法 | 原始维数 | 实际维数 | 信息保留 | ARI |", "| --- | ---: | ---: | ---: | ---: |"]
        for name, item in registry["reduction"]["algorithms"].items():
            lines.append(f"| {name} | {item['original_dimensions']} | {item['actual_dimensions']} | {item['information_retained']} | {item['cluster_comparison']['adjusted_rand_index']} |")
        lines += ["", "## 局限", ""] + [f"- {item}" for item in registry["limitations"]]
        return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="训练 MovieLens 迭代二分类、聚类和降维产物")
    parser.add_argument("--iteration1-run", required=True, help="迭代一成功任务目录")
    parser.add_argument("--output", default="analysis-runs")
    parser.add_argument("--min-history", type=int, default=2)
    parser.add_argument("--clusters", type=int, default=3)
    parser.add_argument("--dbscan-eps", type=float, default=1.5)
    parser.add_argument("--dbscan-min-samples", type=int, default=2)
    parser.add_argument("--target-dimensions", type=int, default=2)
    args = parser.parse_args()
    trainer = AnalysisTrainer(args.iteration1_run, args.output, args.min_history, args.clusters,
                              args.dbscan_eps, args.dbscan_min_samples, args.target_dimensions)
    report = trainer.run(on_progress=lambda phase: print(phase, flush=True))
    print(json.dumps({"task_id": report["task_id"], "analysis_version": report["analysis_version"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
