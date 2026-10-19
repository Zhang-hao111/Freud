import hashlib
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib

from analysis.data import IterationOneDataset, digest, event_features
from analysis.tools import AnalysisRegistry, tool_catalog
from analysis.training import AnalysisTrainer
from analysis.web import AnalysisAgent, Handler, grounded_follow_up


def create_iteration1_run(root, short_user=False):
    run_dir = Path(root) / "iteration1-success"
    clean_dir = run_dir / "cleaned"
    clean_dir.mkdir(parents=True)
    users = [f"{user_id}::{'M' if user_id % 2 else 'F'}::{[18, 25, 35, 45][user_id % 4]}::{user_id % 10}::{10000 + user_id}"
             for user_id in range(1, 9)]
    movies = [
        "10::Action One (2000)::Action|Adventure",
        "11::Drama One (2000)::Drama",
        "12::Comedy One (2000)::Comedy",
        "13::Sci-Fi One (2000)::Sci-Fi|Action",
        "14::Romance One (2000)::Romance|Drama",
        "15::Crime One (2000)::Crime|Thriller",
        "16::Animation One (2000)::Animation|Children's",
        "17::Documentary One (2000)::Documentary",
    ]
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    ratings = []
    movie_ids = list(range(10, 18))
    for user_id in range(1, 9):
        for offset in range(10):
            if short_user and user_id == 8 and offset < 4:
                continue
            movie_id = movie_ids[(user_id + offset) % len(movie_ids)]
            rating = 5 if (user_id + offset) % 3 else 2
            timestamp = int((base + timedelta(days=offset, minutes=user_id)).timestamp())
            ratings.append(f"{user_id}::{movie_id}::{rating}::{timestamp}")
    (clean_dir / "users.dat").write_text("\n".join(users) + "\n", encoding="iso-8859-1")
    (clean_dir / "movies.dat").write_text("\n".join(movies) + "\n", encoding="iso-8859-1")
    (clean_dir / "ratings.dat").write_text("\n".join(ratings) + "\n", encoding="iso-8859-1")
    hashes = {name: digest(clean_dir / f"{name}.dat") for name in ("users", "movies", "ratings")}
    data_version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    report = {
        "status": "completed", "task_id": "iteration1-fixture",
        "data_version": data_version, "rule_version": "ml1m-governance-v1",
        "score_version": "ml1m-five-dim-v1", "cleaned_sha256": hashes,
        "T1": "2000-01-06T23:59:59Z", "T2": "2000-01-08T23:59:59Z",
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run_dir


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.iteration1_run = create_iteration1_run(cls.root)
        cls.output_dir = cls.root / "analysis-runs"
        cls.registry_data = AnalysisTrainer(
            cls.iteration1_run, cls.output_dir, min_history=3, clusters=3,
            dbscan_eps=2.5, dbscan_min_samples=2, target_dimensions=2).run(task_id="analysis-fixture")
        cls.registry = AnalysisRegistry(cls.output_dir)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_iteration_one_contract_and_time_split(self):
        dataset = IterationOneDataset(self.iteration1_run)
        self.assertEqual({name: len(rows) for name, rows in dataset.splits.items()},
                         {"train": 48, "validation": 16, "test": 16})
        matrices, targets, state = event_features(dataset)
        self.assertEqual(matrices["train"].shape, (48, 27))
        self.assertEqual(set(targets["validation"]), {0, 1})
        self.assertEqual(state["cutoff"], dataset.report["T1"])
        self.assertEqual(matrices["train"][0, 3], 0.0)
        self.assertAlmostEqual(matrices["train"][8, 3], 0.6931471805599453)
        self.assertEqual(state["users"]["1"]["count"], 6)

    def test_iteration_one_hash_mismatch_fails(self):
        damaged = create_iteration1_run(self.root / "damaged")
        with open(damaged / "cleaned" / "ratings.dat", "a", encoding="iso-8859-1") as target:
            target.write("1::10::5::1\n")
        with self.assertRaisesRegex(ValueError, "哈希不一致"):
            IterationOneDataset(damaged)

    def test_training_registers_six_algorithms_and_artifacts(self):
        report = self.registry_data
        self.assertEqual(set(report["classification"]["algorithms"]),
                         {"logistic_regression", "random_forest"})
        self.assertEqual(set(report["clustering"]["algorithms"]), {"kmeans", "dbscan"})
        self.assertEqual(set(report["reduction"]["algorithms"]), {"pca", "truncated_svd"})
        self.assertEqual(report["split"]["train"]["records"], 48)
        self.assertEqual(report["classification"]["selection_basis"],
                         "验证期 F1 最高；并列时按算法名确定，测试期不参与选择")
        run_dir = self.output_dir / "analysis-fixture"
        for path in ("registry.json", "report.md", "profiles.json", "feature-state.json",
                     "models/logistic_regression.joblib", "models/random_forest.joblib",
                     "models/kmeans.joblib", "models/dbscan.joblib", "models/pca.joblib",
                     "models/truncated_svd.joblib", "plots/pca.svg", "plots/truncated_svd.svg"):
            self.assertTrue((run_dir / path).is_file(), path)
        reduction_bundle = joblib.load(run_dir / "models" / "pca.joblib")
        self.assertEqual(set(reduction_bundle), {"scaler", "model"})

    def test_each_registered_tool_returns_actual_results(self):
        tools = tool_catalog(self.registry)
        classification = json.loads(tools["logistic_regression"].execute(
            {"user_id": 1, "movie_id": 10}).data)
        self.assertEqual(classification["task"], "classification")
        self.assertIn(classification["prediction"], (0, 1))
        self.assertGreaterEqual(classification["positive_probability"], 0)
        clustering = json.loads(tools["dbscan"].execute({"user_ids": [1, 2]}).data)
        self.assertEqual(len(clustering["users"]), 2)
        self.assertIn("noise", clustering["users"][0])
        reduction = json.loads(tools["pca"].execute({"user_ids": [1, 2, 3]}).data)
        self.assertEqual(reduction["actual_dimensions"], 2)
        self.assertEqual(len(reduction["coordinates"]), 3)
        self.assertEqual(len(reduction["cluster_comparison"]["user_assignments"]), 3)
        self.assertIn("peak_memory_bytes", reduction["resource_usage"])
        self.assertEqual(reduction["data_split"]["train"]["records"], 48)

    def test_parameterized_clustering_refits_and_persists_results(self):
        tools = tool_catalog(self.registry)
        kmeans_result = tools["kmeans"].execute({
            "user_ids": [1, 2, 3],
            "parameters": {"n_clusters": 2, "n_init": 10},
        })
        self.assertTrue(kmeans_result.success, kmeans_result.error)
        kmeans = json.loads(kmeans_result.data)
        self.assertEqual(kmeans["parameters"]["n_clusters"], 2)
        self.assertEqual(kmeans["metrics"]["records"], 8)

        dbscan_result = tools["dbscan"].execute({
            "user_ids": [1, 2],
            "parameters": {"eps": 2.0, "min_samples": 2},
        })
        self.assertTrue(dbscan_result.success, dbscan_result.error)
        dbscan = json.loads(dbscan_result.data)
        self.assertEqual(dbscan["parameters"]["eps"], 2.0)

        for result in (kmeans, dbscan):
            query_dir = self.registry.run_dir / "queries" / result["result_version"]
            self.assertTrue((query_dir / "result.json").is_file())
            self.assertTrue(any(query_dir.glob("*.joblib")))
            model_bundle = joblib.load(next(query_dir.glob("*.joblib")))
            self.assertEqual(set(model_bundle), {"scaler", "model"})

    def test_parameterized_reduction_supports_dimensions_threshold_and_dbscan(self):
        tools = tool_catalog(self.registry)
        pca_result = tools["pca"].execute({
            "user_ids": [1, 2, 3],
            "target_dimensions": 3,
            "cluster_algorithm": "dbscan",
            "cluster_parameters": {"eps": 2.0, "min_samples": 2},
        })
        self.assertTrue(pca_result.success, pca_result.error)
        pca = json.loads(pca_result.data)
        self.assertEqual(pca["actual_dimensions"], 3)
        self.assertEqual(pca["cluster_comparison"]["algorithm"], "dbscan")
        self.assertEqual(len(pca["cluster_comparison"]["user_assignments"]), 3)
        self.assertEqual(len(pca["coordinates"][0]["values"]), 3)

        svd_result = tools["truncated_svd"].execute({
            "user_ids": [1, 2],
            "information_threshold": 0.5,
        })
        self.assertTrue(svd_result.success, svd_result.error)
        svd = json.loads(svd_result.data)
        self.assertGreaterEqual(svd["actual_dimensions"], 2)
        self.assertGreaterEqual(svd["information_retained"], 0.5)

        for result in (pca, svd):
            query_dir = self.registry.run_dir / "queries" / result["result_version"]
            self.assertTrue((query_dir / "result.json").is_file())
            self.assertTrue((self.registry.run_dir / result["plot_path"]).is_file())
            self.assertTrue(any(query_dir.glob("*.joblib")))
            model_bundle = joblib.load(next(query_dir.glob("*.joblib")))
            self.assertEqual(set(model_bundle), {"scaler", "model"})

    def test_invalid_parameterized_requests_fail_explicitly(self):
        tools = tool_catalog(self.registry)
        invalid_cluster = tools["kmeans"].execute({
            "user_ids": [1], "parameters": {"n_clusters": 99},
        })
        self.assertFalse(invalid_cluster.success)
        self.assertIn("n_clusters", invalid_cluster.error)

        conflicting_reduction = tools["pca"].execute({
            "user_ids": [1], "target_dimensions": 2, "information_threshold": 0.8,
        })
        self.assertFalse(conflicting_reduction.success)
        self.assertIn("只能指定一个", conflicting_reduction.error)

        invalid_comparison = tools["truncated_svd"].execute({
            "user_ids": [1], "cluster_algorithm": "spectral",
        })
        self.assertFalse(invalid_comparison.success)
        self.assertIn("未实现的对照聚类算法", invalid_comparison.error)

    def test_unknown_id_and_missing_registry_fail_without_placeholder(self):
        result = tool_catalog(self.registry)["random_forest"].execute({"user_id": 999, "movie_id": 10})
        self.assertFalse(result.success)
        self.assertIn("未知 UserID", result.error)
        with self.assertRaisesRegex(FileNotFoundError, "未找到已登记"):
            AnalysisRegistry(self.root / "missing")

    def test_insufficient_history_user_fails_without_group_placeholder(self):
        short_run = create_iteration1_run(self.root / "short-history", short_user=True)
        short_output = self.root / "short-history-analysis"
        AnalysisTrainer(short_run, short_output, min_history=3, clusters=3,
                        dbscan_eps=2.5, dbscan_min_samples=2,
                        target_dimensions=2).run(task_id="short-history")
        result = tool_catalog(AnalysisRegistry(short_output))["kmeans"].execute({"user_ids": [8]})
        self.assertFalse(result.success)
        self.assertIn("训练期历史不足", result.error)

    def test_agent_defaults_to_two_algorithms_and_supports_follow_up(self):
        agent = AnalysisAgent(self.output_dir)
        job = agent.start("请用两种分类算法预测是否会给高分", user_id=1, movie_id=10)
        for _ in range(100):
            completed = agent.get(job["task_id"])
            if completed["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual([item["algorithm"] for item in completed["results"]],
                         ["logistic_regression", "random_forest"])
        answer = grounded_follow_up(completed, "两种算法评价指标有什么差异？")
        self.assertIn("logistic_regression", answer)
        self.assertIn("random_forest", answer)

    def test_agent_rejects_missing_inputs_and_unregistered_algorithm(self):
        agent = AnalysisAgent(self.output_dir)
        with self.assertRaisesRegex(ValueError, "UserID 和 MovieID"):
            agent.start("请做分类预测", user_id=1)
        with self.assertRaisesRegex(ValueError, "未实现算法"):
            agent.start("请做用户聚类", user_ids=[1, 2], algorithm="spectral")

    def test_request_body_must_be_object(self):
        handler = object.__new__(Handler)
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"[]")
        with self.assertRaisesRegex(ValueError, "必须是对象"):
            handler.body_json()


if __name__ == "__main__":
    unittest.main()
