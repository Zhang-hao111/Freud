"""把治理任务暴露成 agent 工具 — 只提供提交与查询，执行仍在后台线程。

为什么是两个轻量工具而不是一个同步工具：整个 pipeline 要串行跑多个
Hadoop 作业（分钟级），同步调用会阻塞 agent 主循环；完整报告 JSON 含
逐日分布，超出工具输出的合理体积。因此这里返回 task_id 和报告摘要，
完整报告落盘后由 LLM 用 read_file 按需读取。
"""

import json
import re
import shutil

from agent.types import BaseTool, ToolResult
from governance.jobs import JobManager, report_digest


TASK_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class SubmitGovernanceJobTool(BaseTool):
    name = "submit_governance_job"
    description = ("在 Hadoop 上提交 MovieLens 1M 数据清洗与五维质量评估任务"
                   "（准确性/完整性/唯一性/时效性/一致性），后台异步执行，立即返回 task_id。"
                   "任务耗时数分钟，不要重复提交，用 get_governance_job 轮询进度。")
    parameters = {"type": "object", "properties": {
        "source_version": {"type": "string",
                           "description": "可选，16 位十六进制原始数据版本校验值，不匹配则拒绝执行"}},
        "additionalProperties": False}

    def __init__(self, manager):
        self.manager = manager

    def execute(self, args):
        if not isinstance(args, dict) or set(args) - {"source_version"}:
            return ToolResult(False, error="存在未支持的配置项，仅支持 source_version")
        try:
            job = self.manager.submit(source_version=args.get("source_version"))
        except ValueError as error:
            return ToolResult(False, error=str(error))
        return ToolResult(True, data=json.dumps(
            {"task_id": job["task_id"], "status": job["status"], "phase": job["phase"],
             "next": "用 get_governance_job 查询进度"}, ensure_ascii=False))


class GetGovernanceJobTool(BaseTool):
    name = "get_governance_job"
    description = ("查询治理任务状态。运行中返回当前阶段；完成后返回报告摘要"
                   "（五维评分前后对比、处置计数、T1/T2、局限性说明），"
                   "完整报告在 report_path 指向的 report.md，可用 read_file 读取。")
    parameters = {"type": "object", "properties": {
        "task_id": {"type": "string", "description": "submit_governance_job 返回的任务 ID"}},
        "required": ["task_id"], "additionalProperties": False}

    def __init__(self, manager):
        self.manager = manager

    def execute(self, args):
        task_id = args.get("task_id") if isinstance(args, dict) else None
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            return ToolResult(False, error="缺少或格式错误的 task_id")
        job = self.manager.get(task_id)
        if job is None:
            return ToolResult(False, error=f"任务不存在：{task_id}")
        payload = {"task_id": job.get("task_id", task_id), "status": job["status"], "phase": job["phase"]}
        if job["status"] == "failed":
            payload["error"] = job.get("error", "")
        elif job["status"] == "completed":
            payload["report"] = report_digest(job["report"])
            payload["report_path"] = str(self.manager.output_dir / task_id / "report.md")
        return ToolResult(True, data=json.dumps(payload, ensure_ascii=False))


def register_governance_tools(registry, source_dir, output_dir, **settings):
    """Hadoop 环境可用时注册治理工具，返回已注册的工具名；环境缺失则返回空列表。

    settings 透传 HadoopPipeline 的连接参数（hadoop/streaming_jar/hdfs_root/python_command），
    未提供的键回退到同名环境变量。
    """
    import os
    hadoop = settings.get("hadoop", os.environ.get("HADOOP_CMD", "hadoop"))
    streaming_jar = settings.get("streaming_jar", os.environ.get("HADOOP_STREAMING_JAR"))
    if not shutil.which(hadoop) or not streaming_jar:
        return []
    manager = JobManager(source_dir, output_dir, **settings)
    registry.register(SubmitGovernanceJobTool(manager))
    registry.register(GetGovernanceJobTool(manager))
    return [SubmitGovernanceJobTool.name, GetGovernanceJobTool.name]
