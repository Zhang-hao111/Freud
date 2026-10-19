"""Grounded Agent tools backed only by registered iteration-two artifacts."""

import hashlib
import json
import time
import tracemalloc
from pathlib import Path

import joblib
import numpy as np
from sklearn.cluster import DBSCAN, KMeans
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.metrics import adjusted_rand_score
from sklearn.preprocessing import StandardScaler

from agent.types import BaseTool, ToolResult
from analysis.data import inference_feature
from analysis.training import (RANDOM_STATE, cluster_summaries, clustering_metrics,
                               svg_plot)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class AnalysisRegistry:
    def __init__(self, path):
        requested = Path(path).resolve()
        if (requested / "registry.json").is_file():
            self.run_dir = requested
        elif (requested / "latest.json").is_file():
            latest = load_json(requested / "latest.json")
            self.run_dir = requested / latest["task_id"]
        else:
            raise FileNotFoundError(f"未找到已登记的迭代二结果：{requested}")
        self.registry_path = self.run_dir / "registry.json"
        if not self.registry_path.is_file():
            raise FileNotFoundError(f"迭代二结果尚未生成：{self.registry_path}")
        self.data = load_json(self.registry_path)
        if self.data.get("status") != "completed":
            raise ValueError("迭代二结果未完成")
        self.feature_state = load_json(self.run_dir / "feature-state.json")
        self.profiles = load_json(self.run_dir / "profiles.json")

    def path(self, relative):
        candidate = (self.run_dir / relative).resolve()
        if self.run_dir not in candidate.parents:
            raise ValueError("产物路径越界")
        if not candidate.is_file():
            raise FileNotFoundError(f"登记产物不存在：{relative}")
        return candidate

    def user_status(self, user_id):
        key = str(user_id)
        if key not in self.feature_state["user_metadata"]:
            raise ValueError(f"未知 UserID：{user_id}")
        if user_id in self.profiles["insufficient_user_ids"]:
            raise ValueError(f"UserID {user_id} 训练期历史不足，未生成画像结果")
        if key not in self.profiles["details"]:
            raise ValueError(f"UserID {user_id} 的画像结果不可用")
        return key


class RegisteredTool(BaseTool):
    algorithm = ""

    def __init__(self, registry):
        self.registry = registry if isinstance(registry, AnalysisRegistry) else AnalysisRegistry(registry)

    def fail(self, error):
        return ToolResult(False, error=str(error))


class ClassificationTool(RegisteredTool):
    parameters = {"type": "object", "properties": {
        "user_id": {"type": "integer"}, "movie_id": {"type": "integer"}},
        "required": ["user_id", "movie_id"], "additionalProperties": False}

    def execute(self, args):
        try:
            if set(args) != {"user_id", "movie_id"}:
                raise ValueError("分类工具需要且只接受 user_id 和 movie_id")
            user_id, movie_id = int(args["user_id"]), int(args["movie_id"])
            item = self.registry.data["classification"]["algorithms"][self.algorithm]
            model = joblib.load(self.registry.path(item["model_path"]))
            features = inference_feature(self.registry.feature_state, user_id, movie_id)
            probability = float(model.predict_proba(features)[0, 1])
            payload = {
                "status": "completed", "task": "classification", "algorithm": self.algorithm,
                "user_id": user_id, "movie_id": movie_id,
                "movie_title": self.registry.feature_state["movie_metadata"][str(movie_id)]["title"],
                "prediction": int(probability >= item["parameters"]["threshold"]),
                "positive_probability": round(probability, 6),
                "positive_class": self.registry.data["classification"]["positive_class"],
                "model_version": item["model_version"], "parameters": item["parameters"],
                "validation_metrics": item["validation_metrics"], "test_metrics": item["test_metrics"],
                "feature_version": item["feature_version"], "feature_cutoff": item["training_cutoff"],
                "analysis_version": self.registry.data["analysis_version"],
                "data_split": self.registry.data["split"],
                "time_boundaries": {"T1": self.registry.data["input"]["T1"],
                                    "T2": self.registry.data["input"]["T2"]},
                "limitations": [self.registry.data["limitations"][0], self.registry.data["limitations"][1]],
            }
            return ToolResult(True, data=json.dumps(payload, ensure_ascii=False))
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            return self.fail(error)


class LogisticRegressionTool(ClassificationTool):
    name = "classify_logistic_regression"
    description = "使用已登记的逻辑回归模型预测用户是否会给电影不低于 4 分"
    algorithm = "logistic_regression"


class RandomForestTool(ClassificationTool):
    name = "classify_random_forest"
    description = "使用已登记的随机森林模型预测用户是否会给电影不低于 4 分"
    algorithm = "random_forest"


class ClusteringTool(RegisteredTool):
    parameters = {"type": "object", "properties": {
        "user_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
        "parameters": {"type": "object"}},
        "required": ["user_ids"], "additionalProperties": False}

    def execute(self, args):
        try:
            if set(args) - {"user_ids", "parameters"} or not isinstance(args.get("user_ids"), list) or not args["user_ids"]:
                raise ValueError("聚类工具需要非空 user_ids 列表")
            user_ids = [int(value) for value in args["user_ids"]]
            keys = [self.registry.user_status(user_id) for user_id in user_ids]
            item = self.registry.data["clustering"]["algorithms"][self.algorithm]
            requested = args.get("parameters")
            result = (load_json(self.registry.path(item["result_path"])) if requested is None
                      else self._run_parameterized(requested))
            users = []
            for user_id, key in zip(user_ids, keys):
                label = result["assignments"][key]
                users.append({"user_id": user_id, "cluster": label, "noise": label == -1,
                              "profile": self.registry.profiles["details"][key],
                              "group_summary": result["summaries"][str(label)]})
            payload = {
                "status": "completed", "task": "clustering", "algorithm": self.algorithm,
                "users": users, "result_version": result["result_version"],
                "parameters": result["parameters"], "metrics": result["metrics"],
                "profile_version": result["profile_version"],
                "training_cutoff": result["training_cutoff"],
                "analysis_version": self.registry.data["analysis_version"],
                "data_split": self.registry.data["split"],
                "time_boundaries": {"T1": self.registry.data["input"]["T1"],
                                    "T2": self.registry.data["input"]["T2"]},
                "limitations": [self.registry.data["limitations"][2], self.registry.data["limitations"][3]],
            }
            return ToolResult(True, data=json.dumps(payload, ensure_ascii=False))
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            return self.fail(error)

    def _run_parameterized(self, parameters):
        if not isinstance(parameters, dict):
            raise ValueError("聚类参数必须是对象")
        allowed = {"n_clusters", "n_init"} if self.algorithm == "kmeans" else {"eps", "min_samples"}
        if set(parameters) - allowed:
            raise ValueError(f"{self.algorithm} 不支持参数：{sorted(set(parameters) - allowed)}")
        user_ids = self.registry.profiles["user_ids"]
        matrix = np.asarray([self.registry.profiles["vectors"][str(user_id)] for user_id in user_ids])
        scaler = StandardScaler()
        scaled = scaler.fit_transform(matrix)
        if self.algorithm == "kmeans":
            actual = {"n_clusters": int(parameters.get("n_clusters", 3)),
                      "n_init": int(parameters.get("n_init", 20)), "random_state": RANDOM_STATE}
            if not 2 <= actual["n_clusters"] < len(user_ids):
                raise ValueError("n_clusters 必须至少为 2 且小于画像用户数")
            model = KMeans(**actual)
        else:
            actual = {"eps": float(parameters.get("eps", 1.5)),
                      "min_samples": int(parameters.get("min_samples", 2))}
            if actual["eps"] <= 0 or actual["min_samples"] < 2:
                raise ValueError("DBSCAN 要求 eps>0 且 min_samples>=2")
            model = DBSCAN(**actual)
        started = time.perf_counter()
        labels = model.fit_predict(scaled)
        version = hashlib.sha256(json.dumps({"algorithm": self.algorithm, "parameters": actual,
                                             "analysis": self.registry.data["analysis_version"]},
                                            sort_keys=True).encode()).hexdigest()[:16]
        query_dir = self.registry.run_dir / "queries" / version
        query_dir.mkdir(parents=True, exist_ok=True)
        model_path = query_dir / f"{self.algorithm}.joblib"
        joblib.dump({"scaler": scaler, "model": model}, model_path)
        payload = {
            "algorithm": self.algorithm, "result_version": version, "parameters": actual,
            "metrics": clustering_metrics(scaled, labels),
            "assignments": {str(user_id): int(label) for user_id, label in zip(user_ids, labels)},
            "summaries": cluster_summaries(labels, self.registry.profiles),
            "model_path": str(model_path.relative_to(self.registry.run_dir)),
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "profile_version": self.registry.data["profiles"]["version"],
            "training_cutoff": self.registry.data["input"]["T1"],
        }
        (query_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload


class KMeansTool(ClusteringTool):
    name = "cluster_kmeans"
    description = "返回已登记的 K-Means 用户群体、摘要、参数和评价指标"
    algorithm = "kmeans"


class DBSCANTool(ClusteringTool):
    name = "cluster_dbscan"
    description = "返回已登记的 DBSCAN 用户群体、噪声标记、参数和评价指标"
    algorithm = "dbscan"


class ReductionTool(RegisteredTool):
    parameters = {"type": "object", "properties": {
        "user_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
        "target_dimensions": {"type": "integer", "minimum": 2},
        "information_threshold": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        "cluster_algorithm": {"type": "string", "enum": ["kmeans", "dbscan"]},
        "cluster_parameters": {"type": "object"}},
        "required": ["user_ids"], "additionalProperties": False}

    def execute(self, args):
        try:
            allowed = {"user_ids", "target_dimensions", "information_threshold",
                       "cluster_algorithm", "cluster_parameters"}
            if set(args) - allowed or not isinstance(args.get("user_ids"), list) or not args["user_ids"]:
                raise ValueError("降维工具需要非空 user_ids 列表")
            user_ids = [int(value) for value in args["user_ids"]]
            keys = [self.registry.user_status(user_id) for user_id in user_ids]
            item = self.registry.data["reduction"]["algorithms"][self.algorithm]
            custom = any(name in args for name in allowed - {"user_ids"})
            result = (self._run_parameterized(args) if custom else
                      load_json(self.registry.path(item["result_path"])))
            comparison = dict(result["cluster_comparison"])
            comparison["user_assignments"] = [
                {"user_id": user_id,
                 "original_cluster": result["original_cluster_assignments"][key],
                 "reduced_cluster": result["reduced_cluster_assignments"][key]}
                for user_id, key in zip(user_ids, keys)
            ]
            payload = {
                "status": "completed", "task": "reduction", "algorithm": self.algorithm,
                "coordinates": [{"user_id": user_id, "values": result["coordinates"][key]}
                                for user_id, key in zip(user_ids, keys)],
                "result_version": result["result_version"], "parameters": result["parameters"],
                "original_dimensions": result["original_dimensions"],
                "actual_dimensions": result["actual_dimensions"],
                "information_retained": result["information_retained"],
                "cluster_comparison": comparison,
                "resource_usage": result["resource_usage"],
                "profile_version": result["profile_version"],
                "training_cutoff": result["training_cutoff"],
                "plot_path": result["plot_path"],
                "analysis_version": self.registry.data["analysis_version"],
                "data_split": self.registry.data["split"],
                "time_boundaries": {"T1": self.registry.data["input"]["T1"],
                                    "T2": self.registry.data["input"]["T2"]},
                "limitations": [self.registry.data["limitations"][4]],
            }
            return ToolResult(True, data=json.dumps(payload, ensure_ascii=False))
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            return self.fail(error)

    def _run_parameterized(self, args):
        if "target_dimensions" in args and "information_threshold" in args:
            raise ValueError("target_dimensions 与 information_threshold 只能指定一个")
        user_ids = self.registry.profiles["user_ids"]
        matrix = np.asarray([self.registry.profiles["vectors"][str(user_id)] for user_id in user_ids])
        scaler = StandardScaler()
        scaled = scaler.fit_transform(matrix)
        maximum = min(scaled.shape[1], len(user_ids) - 1)
        target = int(args.get("target_dimensions", 2))
        threshold = args.get("information_threshold")
        if threshold is not None:
            threshold = float(threshold)
            if not 0 < threshold <= 1:
                raise ValueError("information_threshold 必须在 (0,1] 范围")
            probe = (PCA(n_components=maximum, random_state=RANDOM_STATE) if self.algorithm == "pca"
                     else TruncatedSVD(n_components=maximum, random_state=RANDOM_STATE))
            probe.fit(scaled)
            cumulative = np.cumsum(probe.explained_variance_ratio_)
            target = max(2, int(np.searchsorted(cumulative, threshold) + 1))
        if not 2 <= target <= maximum:
            raise ValueError(f"实际降维维数必须在 2 到 {maximum} 之间")
        reducer = (PCA(n_components=target, random_state=RANDOM_STATE) if self.algorithm == "pca"
                   else TruncatedSVD(n_components=target, random_state=RANDOM_STATE))
        cluster_algorithm = args.get("cluster_algorithm", "kmeans")
        cluster_parameters = args.get("cluster_parameters") or {}
        if not isinstance(cluster_parameters, dict):
            raise ValueError("cluster_parameters 必须是对象")
        tracemalloc.start()
        started = time.perf_counter()
        coordinates = reducer.fit_transform(scaled)
        original_labels, reduced_labels, actual_cluster = self._cluster_pair(
            scaled, coordinates, cluster_algorithm, cluster_parameters)
        elapsed = time.perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        parameters = {"target_dimensions": args.get("target_dimensions"),
                      "information_threshold": threshold, "actual_dimensions": target,
                      "random_state": RANDOM_STATE, "cluster_algorithm": cluster_algorithm,
                      "cluster_parameters": actual_cluster}
        version = hashlib.sha256(json.dumps({"algorithm": self.algorithm, "parameters": parameters,
                                             "analysis": self.registry.data["analysis_version"]},
                                            sort_keys=True).encode()).hexdigest()[:16]
        query_dir = self.registry.run_dir / "queries" / version
        query_dir.mkdir(parents=True, exist_ok=True)
        model_path = query_dir / f"{self.algorithm}.joblib"
        plot_path = query_dir / f"{self.algorithm}.svg"
        joblib.dump({"scaler": scaler, "model": reducer}, model_path)
        plot_path.write_text(svg_plot(user_ids, coordinates, reduced_labels,
                                      f"{self.algorithm} 参数化用户画像"), encoding="utf-8")
        comparison = {
            "algorithm": cluster_algorithm, "parameters": actual_cluster,
            "original_metrics": clustering_metrics(scaled, original_labels),
            "reduced_metrics": clustering_metrics(coordinates, reduced_labels),
            "adjusted_rand_index": round(float(adjusted_rand_score(original_labels, reduced_labels)), 6),
        }
        payload = {
            "algorithm": self.algorithm, "result_version": version, "parameters": parameters,
            "original_dimensions": int(scaled.shape[1]), "actual_dimensions": target,
            "information_retained": round(float(np.sum(reducer.explained_variance_ratio_)), 6),
            "coordinates": {str(user_id): [round(float(value), 8) for value in row]
                            for user_id, row in zip(user_ids, coordinates)},
            "cluster_comparison": comparison,
            "original_cluster_assignments": {
                str(user_id): int(label) for user_id, label in zip(user_ids, original_labels)
            },
            "reduced_cluster_assignments": {
                str(user_id): int(label) for user_id, label in zip(user_ids, reduced_labels)
            },
            "resource_usage": {"runtime_seconds": round(elapsed, 6), "peak_memory_bytes": int(peak_bytes)},
            "model_path": str(model_path.relative_to(self.registry.run_dir)),
            "plot_path": str(plot_path.relative_to(self.registry.run_dir)),
            "profile_version": self.registry.data["profiles"]["version"],
            "training_cutoff": self.registry.data["input"]["T1"],
        }
        (query_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    @staticmethod
    def _cluster_pair(original, reduced, algorithm, parameters):
        if algorithm == "kmeans":
            allowed = {"n_clusters", "n_init"}
            if set(parameters) - allowed:
                raise ValueError(f"K-Means 不支持参数：{sorted(set(parameters) - allowed)}")
            actual = {"n_clusters": int(parameters.get("n_clusters", 3)),
                      "n_init": int(parameters.get("n_init", 20)), "random_state": RANDOM_STATE}
            if not 2 <= actual["n_clusters"] < len(original):
                raise ValueError("n_clusters 必须至少为 2 且小于画像用户数")
            original_labels = KMeans(**actual).fit_predict(original)
            reduced_labels = KMeans(**actual).fit_predict(reduced)
        elif algorithm == "dbscan":
            allowed = {"eps", "min_samples"}
            if set(parameters) - allowed:
                raise ValueError(f"DBSCAN 不支持参数：{sorted(set(parameters) - allowed)}")
            actual = {"eps": float(parameters.get("eps", 1.5)),
                      "min_samples": int(parameters.get("min_samples", 2))}
            if actual["eps"] <= 0 or actual["min_samples"] < 2:
                raise ValueError("DBSCAN 要求 eps>0 且 min_samples>=2")
            original_labels = DBSCAN(**actual).fit_predict(original)
            reduced_labels = DBSCAN(**actual).fit_predict(reduced)
        else:
            raise ValueError(f"未实现的对照聚类算法：{algorithm}")
        return original_labels, reduced_labels, actual


class PCATool(ReductionTool):
    name = "reduce_pca"
    description = "返回已登记的 PCA 坐标、信息保留、聚类对照和资源开销"
    algorithm = "pca"


class TruncatedSVDTool(ReductionTool):
    name = "reduce_truncated_svd"
    description = "返回已登记的 Truncated SVD 坐标、信息保留、聚类对照和资源开销"
    algorithm = "truncated_svd"


def tool_catalog(registry):
    return {
        "logistic_regression": LogisticRegressionTool(registry),
        "random_forest": RandomForestTool(registry),
        "kmeans": KMeansTool(registry),
        "dbscan": DBSCANTool(registry),
        "pca": PCATool(registry),
        "truncated_svd": TruncatedSVDTool(registry),
    }
