"""治理任务的异步作业管理 — web 界面与 agent 工具共用的提交/查询层。

执行仍然只在后台线程里进行，报告 JSON 是唯一事实源；这里不引入任何
LLM 决策，保证同一份数据重复运行结果可复现。
"""

import json
import re
import threading
import uuid
from pathlib import Path

from governance.pipeline import HadoopGovernanceTool, HadoopPipeline, RULE_VERSION, SCORE_VERSION


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "ml-1m" / "ml-1m"
DEFAULT_OUTPUT = ROOT / "governance-runs"

SOURCE_VERSION_PATTERN = re.compile(r"[0-9a-f]{16}")


def report_digest(report):
    """抽取报告要点作为回答依据；不含逐日分布等大体量字段。"""
    digest = {key: report[key] for key in
              ("task_id", "source_version", "data_version", "rule_version", "score_version",
               "T1", "T2", "actions", "explanation", "method", "limitations",
               "disposition_examples") if key in report}
    digest["before"] = {key: report["before"].get(key) for key in ("total", "tables", "issues", "scores")}
    digest["after"] = {key: report["after"].get(key) for key in ("total", "tables", "issues", "scores")}
    return digest


class JobManager:
    """后台线程运行 HadoopPipeline，维护任务状态；失败时落盘 failure.json。

    pipeline_factory 仅用于测试注入替身，默认构造真实的 HadoopPipeline。
    """

    def __init__(self, source_dir, output_dir, pipeline_factory=HadoopPipeline, **settings):
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        self.pipeline_factory = pipeline_factory
        self.settings = dict(settings)
        self.jobs = {}
        self.lock = threading.Lock()

    def submit(self, prompt="", source_version=None, rule_version=None, score_version=None):
        if rule_version not in (None, RULE_VERSION) or score_version not in (None, SCORE_VERSION):
            raise ValueError("请求的清洗或评分规则版本未登记")
        if source_version is not None and (not isinstance(source_version, str) or not SOURCE_VERSION_PATTERN.fullmatch(source_version)):
            raise ValueError("原始数据版本格式错误")
        task_id = uuid.uuid4().hex
        job = {"task_id": task_id, "status": "running", "phase": "准备运行", "prompt": prompt,
               "source_version": source_version, "rule_version": RULE_VERSION, "score_version": SCORE_VERSION}
        with self.lock:
            self.jobs[task_id] = job
        threading.Thread(target=self._run, args=(task_id,), daemon=True).start()
        return job.copy()

    def _run(self, task_id):
        def progress(phase):
            with self.lock:
                self.jobs[task_id]["phase"] = phase
        try:
            pipeline = self.pipeline_factory(self.source_dir, self.output_dir, **self.settings)
            tool = HadoopGovernanceTool(pipeline, progress, task_id)
            request = self.get(task_id)
            result = tool.execute({"rule_version": request["rule_version"],
                                   "score_version": request["score_version"],
                                   **({"source_version": request["source_version"]} if request["source_version"] else {})})
            if not result.success:
                raise RuntimeError(result.error)
            report = json.loads(result.data)
            with self.lock:
                self.jobs[task_id].update(status="completed", phase="已完成", report=report)
        except Exception as error:
            # 先落盘 failure.json 再更新状态，避免轮询方读到 failed 却找不到失败记录
            with self.lock:
                phase = self.jobs[task_id]["phase"]
            failure_dir = self.output_dir / task_id
            failure_dir.mkdir(parents=True, exist_ok=True)
            (failure_dir / "failure.json").write_text(json.dumps(
                {"task_id": task_id, "phase": f"{phase}失败", "error": str(error)}, ensure_ascii=False), encoding="utf-8")
            with self.lock:
                self.jobs[task_id].update(status="failed", phase=f"{phase}失败", error=str(error))

    def get(self, task_id):
        with self.lock:
            job = self.jobs.get(task_id)
            if job:
                return job.copy()
        report_path = self.output_dir / task_id / "report.json"
        if report_path.is_file():
            return {"task_id": task_id, "status": "completed", "phase": "已完成",
                    "report": json.loads(report_path.read_text(encoding="utf-8"))}
        failure_path = self.output_dir / task_id / "failure.json"
        if failure_path.is_file():
            return {"status": "failed", **json.loads(failure_path.read_text(encoding="utf-8"))}
        return None
