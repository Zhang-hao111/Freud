"""Small web interface and grounded agent for Hadoop governance jobs."""

import argparse
import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent.config import load_config
from agent.llm import OpenAIProvider
from agent.types import SystemMessage, UserMessage
from governance.pipeline import HadoopGovernanceTool, HadoopPipeline, RULE_VERSION, SCORE_VERSION


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "ml-1m" / "ml-1m"
DEFAULT_OUTPUT = ROOT / "governance-runs"
DIMENSIONS = ("Accurate", "Complete", "Unique", "Up-to-date", "Consistent")
DIMENSION_ALIASES = {"Accurate": "准确性", "Complete": "完整性", "Unique": "唯一性",
                     "Up-to-date": "时效性", "Consistent": "一致性"}
ISSUE_LABELS = {
    "field_count": "字段数错误", "missing": "必填项缺失", "format": "格式不规范",
    "domain": "类型或值域错误", "timestamp": "时间范围错误", "reference": "关联实体不存在",
    "duplicate": "完全重复", "conflict": "同一业务键内容冲突",
}
ACTION_LABELS = {"repair": "修复", "deduplicate": "去重", "quarantine": "隔离"}

GROUNDING_PROMPT = (
    "你是 MovieLens 数据治理评估的解释助手。请只依据下方任务报告中的事实回答用户问题："
    "报告里没有的数据必须回答“报告中未包含该信息”，禁止编造或自行推算数字。"
    "回答使用中文，简洁分点，可直接引用报告中的具体数字。任务报告 JSON：\n"
)


def report_digest(report):
    """抽取报告要点作为回答依据；不含逐日分布等大体量字段。"""
    digest = {key: report[key] for key in
              ("task_id", "source_version", "data_version", "rule_version", "score_version",
               "T1", "T2", "actions", "explanation", "method", "limitations",
               "disposition_examples") if key in report}
    digest["before"] = {key: report["before"].get(key) for key in ("total", "tables", "issues", "scores")}
    digest["after"] = {key: report["after"].get(key) for key in ("total", "tables", "issues", "scores")}
    return digest


class LLMExplainer:
    """大模型追问解释层：报告 JSON 是唯一事实源，模型不可用时返回 None 回退模板回答。"""

    def __init__(self, config):
        self.provider = None
        if config.get("api_key"):
            self.provider = OpenAIProvider(api_key=config["api_key"], model=config["model"],
                                           base_url=config["api_base"])
            self.provider.client = self.provider.client.with_options(timeout=30.0)
            self.provider.MAX_RETRIES = 2

    def answer(self, report, question):
        if not self.provider or not isinstance(question, str) or not question.strip():
            return None
        messages = [SystemMessage(content=GROUNDING_PROMPT + json.dumps(report_digest(report), ensure_ascii=False)),
                    UserMessage(content=question)]
        try:
            response = self.provider.chat(messages, tools=[], include_done=False, include_take_note=False)
        except Exception as error:
            print(f"  ⚠ LLM 追问失败，回退模板回答: {error}")
            return None
        return response.message.content if response.message else None


def explain(report, question):
    if not isinstance(question, str):
        return "请输入文本问题。"
    question = question.strip()
    if not question:
        return "请输入关于本次任务的具体问题。"
    before, after = report["before"], report["after"]
    lowered = question.lower()
    names = [name for name in DIMENSIONS
             if name.lower() in lowered or DIMENSION_ALIASES[name] in question]
    remaining_intent = any(word in question for word in
                           ("未解决", "剩余", "仍存在", "还有哪些", "清洗后异常"))
    volume_intent = any(word in question for word in ("数据量", "记录数", "总数", "数量"))
    method_intent = any(word in question for word in ("依据", "方法", "口径")) or (
        "规则" in question and "版本" not in question)
    limitation_intent = any(word in question for word in ("局限", "可信", "未验证", "无法验证"))
    version_intent = any(word in question for word in
                         ("版本", "时间", "T1", "T2", "训练期", "验证期", "测试期"))
    example_intent = any(word in question for word in ("记录", "样例", "例子", "具体", "原因"))
    disposition_intent = any(word in question for word in
                             ("异常", "隔离", "删除", "重复", "去重", "修复")) or (
        "问题" in question and not remaining_intent)
    score_intent = bool(names) or any(word in question for word in ("分数", "提升")) or (
        "评分" in question and not method_intent and not version_intent) or (
        "变化" in question and not volume_intent and not disposition_intent and not remaining_intent)
    sections = []
    if remaining_intent:
        remaining = "、".join(
            f"{ISSUE_LABELS.get(name, name)} {count}"
            for name, count in sorted(after.get("issues", {}).items()))
        sections.append("清洗后规则内仍检出：" + remaining + "。" if remaining else "清洗后规则内未检出异常。")
        if report.get("limitations"):
            sections.append("仍无法自动验证或解决：" + "；".join(report["limitations"]))
    if volume_intent:
        action = report["actions"]
        delta = after["total"] - before["total"]
        sections.append(
            f"原始 {before['total']} 条，清洗后 {after['total']} 条，变化 {delta:+d} 条；"
            f"修复 {action.get('repair', 0)} 条、去重 {action.get('deduplicate', 0)} 条、"
            f"隔离 {action.get('quarantine', 0)} 条。隔离和去重会改变评分分母。")
    if score_intent:
        names = names or DIMENSIONS
        lines = [f"{name}：{before['scores'][name]} → {after['scores'][name]}；依据：{report['method'][name]}。"
                 for name in names]
        if "explanation" in report:
            lines.append(report["explanation"])
        sections.append("\n".join(lines))
    if disposition_intent and not remaining_intent:
        action = report["actions"]
        issues = "、".join(f"{ISSUE_LABELS.get(name, name)} {count}" for name, count in sorted(before["issues"].items())) or "未检出规则内异常"
        answer = (f"清洗前异常标记：{issues}。修复 {action.get('repair', 0)} 条，"
                  f"去重 {action.get('deduplicate', 0)} 条，隔离 {action.get('quarantine', 0)} 条。"
                  "同一记录可能有多个异常标记；隔离并不表示已修复。")
        if example_intent:
            examples = report.get("disposition_examples", [])[:5]
            answer += "\n" + ("\n".join(
                f"{item['table']}：{item['raw']}；{ACTION_LABELS.get(item['action'], item['action'])}；原因："
                + "、".join(ISSUE_LABELS.get(issue, issue) for issue in item["issues"])
                for item in examples) if examples else "报告中没有可展示的处置样例。")
        sections.append(answer)
    if method_intent:
        sections.append("评分依据：\n" + "\n".join(
            f"{name}：{value}" for name, value in report["method"].items()))
    if limitation_intent and not remaining_intent:
        sections.append("评价局限：" + "；".join(report["limitations"]))
    if version_intent:
        sections.append(
            f"原始数据版本 {report['source_version']}，清洗版本 {report['data_version']}，"
            f"规则版本 {report['rule_version']}，评分版本 {report.get('score_version', '未登记')}。"
            f"T1={report['T1']}，T2={report['T2']}；"
            "训练期不晚于 T1，验证期为 T1 之后至 T2，测试期在 T2 之后。")
    return "\n".join(sections) if sections else (
        "我只能依据本次实际报告回答分数、异常处置、规则局限和版本时间问题。请具体询问其中一项。")


class GovernanceAgent:
    def __init__(self, source_dir, output_dir, hadoop, streaming_jar, hdfs_root, python_command, llm=None):
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        self.settings = dict(hadoop=hadoop, streaming_jar=streaming_jar,
                             hdfs_root=hdfs_root, python_command=python_command)
        self.llm = llm
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, prompt, source_version=None, rule_version=None, score_version=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请输入自然语言任务需求")
        if not any(word in prompt.lower() for word in ("清洗", "评估", "评分", "治理", "movielens", "movie lens")):
            raise ValueError("当前 Agent 支持 MovieLens 1M 清洗与五维质量评估，请描述相关需求")
        if re.search(r"自定义|非默认|不用默认|修改.{0,8}规则|修改.{0,8}权重", prompt):
            raise ValueError("当前只登记了默认清洗及评分方案；请提供已登记的规则版本")
        if rule_version not in (None, RULE_VERSION) or score_version not in (None, SCORE_VERSION):
            raise ValueError("请求的清洗或评分规则版本未登记")
        if source_version is not None and (not isinstance(source_version, str) or not re.fullmatch(r"[0-9a-f]{16}", source_version)):
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
            pipeline = HadoopPipeline(self.source_dir, self.output_dir, **self.settings)
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
            with self.lock:
                phase = self.jobs[task_id]["phase"]
                self.jobs[task_id].update(status="failed", phase=f"{phase}失败", error=str(error))
            failure_dir = self.output_dir / task_id
            failure_dir.mkdir(parents=True, exist_ok=True)
            (failure_dir / "failure.json").write_text(json.dumps(
                {"task_id": task_id, "phase": f"{phase}失败", "error": str(error)}, ensure_ascii=False), encoding="utf-8")

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


class Handler(BaseHTTPRequestHandler):
    agent = None

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("请求内容为空或过大")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求 JSON 必须是对象")
        return data

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            data = self.body_json()
            if path == "/api/jobs":
                self.send_json(self.agent.start(data.get("prompt"), data.get("source_version"),
                                                data.get("rule_version"), data.get("score_version")), 202)
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/ask", path)
            if match:
                job = self.agent.get(match.group(1))
                if not job:
                    self.send_json({"error": "任务不存在"}, 404)
                elif job["status"] != "completed":
                    self.send_json({"error": "任务尚未完成，无法依据结果回答"}, 409)
                else:
                    question = data.get("question", "")
                    answer = self.agent.llm.answer(job["report"], question) if self.agent.llm else None
                    mode = "llm" if answer else "template"
                    if not answer:
                        answer = explain(job["report"], question)
                    self.send_json({"answer": answer, "mode": mode})
                return
            self.send_json({"error": "接口不存在"}, 404)
        except (ValueError, TypeError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            content = Path(__file__).with_name("index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(?:/(report|sample))?", path)
        if not match:
            self.send_json({"error": "接口不存在"}, 404)
            return
        job = self.agent.get(match.group(1))
        if not job:
            self.send_json({"error": "任务不存在"}, 404)
            return
        kind = match.group(2)
        if not kind:
            self.send_json(job)
            return
        if job["status"] != "completed":
            self.send_json({"error": "任务尚未完成"}, 409)
            return
        run_dir = self.agent.output_dir / job["report"]["task_id"]
        if kind == "report":
            content = (run_dir / "report.md").read_bytes()
            content_type = "text/markdown; charset=utf-8"
        else:
            with open(run_dir / "cleaned" / "ratings.dat", "rb") as source:
                content = b"".join(source.readline() for _ in range(12)).decode("iso-8859-1").encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    parser = argparse.ArgumentParser(description="MovieLens Hadoop 数据治理 Agent 网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--hadoop", default=os.environ.get("HADOOP_CMD", "hadoop"))
    parser.add_argument("--streaming-jar", default=os.environ.get("HADOOP_STREAMING_JAR"))
    parser.add_argument("--hdfs-root", default=os.environ.get("GOVERNANCE_HDFS_ROOT", "/freud/governance"))
    parser.add_argument("--python-command", default=os.environ.get("HADOOP_PYTHON", "python3"))
    parser.add_argument("--no-llm", action="store_true", help="追问使用确定性模板回答，不调用大模型")
    args = parser.parse_args()
    llm = None
    if not args.no_llm:
        llm = LLMExplainer(load_config())
        if llm.provider:
            print(f"追问解释层：大模型 {llm.provider.model}（失败自动回退模板）")
        else:
            print("追问解释层：未配置 API Key，使用确定性模板回答")
    else:
        print("追问解释层：--no-llm，使用确定性模板回答")
    Handler.agent = GovernanceAgent(args.source, args.output, args.hadoop, args.streaming_jar,
                                    args.hdfs_root, args.python_command, llm=llm)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"MovieLens 治理页面：http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止；任务产物保留在 governance-runs/ 与 Hadoop 目录中。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
