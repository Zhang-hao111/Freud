"""Web Agent for registered MovieLens iteration-two analysis results."""

import argparse
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from analysis.tools import AnalysisRegistry, tool_catalog


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = ROOT / "analysis-runs"
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
    if any(word in question for word in ("版本", "T1", "T2", "时间", "切分")):
        versions = [f"{item['algorithm']}={item.get('model_version') or item.get('result_version')}"
                    for item in results]
        return (f"分析版本 {results[0]['analysis_version']}；" + "，".join(versions)
                + f"；训练截止 {results[0].get('feature_cutoff') or results[0].get('training_cutoff')}。")
    if any(word in question for word in ("指标", "评价", "效果", "对比", "差异")):
        parts = []
        for item in results:
            metrics = item.get("test_metrics") or item.get("metrics") or item.get("cluster_comparison")
            parts.append(f"{item['algorithm']}：{json.dumps(metrics, ensure_ascii=False)}")
        return "实际登记的评价结果：" + "；".join(parts)
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


class AnalysisAgent:
    def __init__(self, results_path):
        self.results_path = Path(results_path)
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, prompt, user_id=None, movie_id=None, user_ids=None, algorithm=None,
              parameters=None, target_dimensions=None, information_threshold=None,
              cluster_algorithm=None, cluster_parameters=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请输入自然语言分析问题")
        task = infer_task(prompt)
        algorithms = requested_algorithm(prompt, algorithm, task)
        if task == "classification":
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
        task_id = uuid.uuid4().hex
        job = {"task_id": task_id, "status": "running", "phase": "加载已登记结果",
               "prompt": prompt, "task": task, "algorithms": algorithms, "arguments": arguments}
        with self.lock:
            self.jobs[task_id] = job
        threading.Thread(target=self._run, args=(task_id,), daemon=True).start()
        return job.copy()

    def _run(self, task_id):
        try:
            registry = AnalysisRegistry(self.results_path)
            tools = tool_catalog(registry)
            with self.lock:
                request = self.jobs[task_id].copy()
                self.jobs[task_id]["phase"] = "调用分析工具"
            results = []
            for algorithm in request["algorithms"]:
                result = tools[algorithm].execute(request["arguments"])
                if not result.success:
                    raise ValueError(result.error)
                results.append(json.loads(result.data))
            with self.lock:
                self.jobs[task_id].update(status="completed", phase="已完成", results=results,
                                          registry_dir=str(registry.run_dir))
        except Exception as error:
            with self.lock:
                phase = self.jobs[task_id]["phase"]
                self.jobs[task_id].update(status="failed", phase=f"{phase}失败", error=str(error))

    def get(self, task_id):
        with self.lock:
            job = self.jobs.get(task_id)
            return job.copy() if job else None


class Handler(BaseHTTPRequestHandler):
    agent = None

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        content = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("请求内容为空或过大")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            data = self.body_json()
            if path == "/api/jobs":
                job = self.agent.start(
                    data.get("prompt"), data.get("user_id"), data.get("movie_id"),
                    data.get("user_ids"), data.get("algorithm"), data.get("parameters"),
                    data.get("target_dimensions"), data.get("information_threshold"),
                    data.get("cluster_algorithm"), data.get("cluster_parameters"))
                self.send_json(job, 202)
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/ask", path)
            if match:
                job = self.agent.get(match.group(1))
                if not job:
                    self.send_json({"error": "任务不存在"}, 404)
                elif job["status"] != "completed":
                    self.send_json({"error": "任务尚未完成，无法依据结果回答"}, 409)
                else:
                    self.send_json({"answer": grounded_follow_up(job, data.get("question"))})
                return
            self.send_json({"error": "接口不存在"}, 404)
        except (ValueError, TypeError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_file(Path(__file__).with_name("index.html"), "text/html; charset=utf-8")
            return
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(?:/(report|sample|plot/([a-z_]+)))?", path)
        if not match:
            self.send_json({"error": "接口不存在"}, 404)
            return
        job = self.agent.get(match.group(1))
        if not job:
            self.send_json({"error": "任务不存在"}, 404)
            return
        kind, plot_algorithm = match.group(2), match.group(3)
        if not kind:
            self.send_json(job)
            return
        if job["status"] != "completed":
            self.send_json({"error": "任务尚未完成"}, 409)
            return
        registry_dir = Path(job["registry_dir"])
        if kind == "report":
            self.send_file(registry_dir / "report.md", "text/markdown; charset=utf-8")
        elif kind == "sample":
            self.send_json({"task": job["task"], "results": job["results"]})
        else:
            result = next((item for item in job["results"] if item["algorithm"] == plot_algorithm), None)
            if not result or "plot_path" not in result:
                self.send_json({"error": "本次任务没有该图表"}, 404)
            else:
                plot_path = (registry_dir / result["plot_path"]).resolve()
                if registry_dir.resolve() not in plot_path.parents:
                    self.send_json({"error": "图表路径无效"}, 400)
                else:
                    self.send_file(plot_path, "image/svg+xml; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(description="MovieLens 迭代二分析 Agent 网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8865)
    parser.add_argument("--results", default=str(DEFAULT_RESULTS), help="分析任务目录或包含 latest.json 的目录")
    args = parser.parse_args()
    Handler.agent = AnalysisAgent(args.results)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"MovieLens 分析页面：http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
