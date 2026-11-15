"""治理任务服务，供 Freud 数据任务插件调用。"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from movielens_plugin.governance.pipeline import (ACTION_LABELS, ISSUE_LABELS, HadoopGovernanceTool,
                                 HadoopPipeline, RULE_VERSION, SCORE_VERSION)


DIMENSIONS = ("Accurate", "Complete", "Unique", "Up-to-date", "Consistent")
DIMENSION_ALIASES = {"Accurate": "准确性", "Complete": "完整性", "Unique": "唯一性",
                     "Up-to-date": "时效性", "Consistent": "一致性"}


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


def default_governance_root(hadoop):
    explicit = os.environ.get('GOVERNANCE_HDFS_ROOT')
    if explicit:
        return explicit
    hadoop_path = shutil.which(hadoop)
    hdfs = str(Path(hadoop_path).with_name('hdfs')) if hadoop_path else 'hdfs'
    try:
        result = subprocess.run([hdfs, 'getconf', '-confKey', 'fs.defaultFS'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip().startswith('file:'):
            return str(Path(tempfile.gettempdir()) / f'freud-hadoop-{os.getuid()}' / 'governance')
    except (OSError, subprocess.TimeoutExpired):
        pass
    return '/freud/governance'


class GovernanceService:
    def __init__(self, source_dir, output_dir, hadoop, streaming_jar, python_command):
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        self.settings = dict(hadoop=hadoop, streaming_jar=streaming_jar,
                             python_command=python_command)

    def run(self, prompt, source_version=None, rule_version=None, score_version=None, *, on_progress=None):
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
        phase = "准备运行"

        def progress(value):
            nonlocal phase
            phase = value
            if on_progress:
                on_progress(value)

        progress(phase)
        try:
            root = default_governance_root(self.settings["hadoop"])
            pipeline = HadoopPipeline(self.source_dir, self.output_dir, hdfs_root=root, **self.settings)
            tool = HadoopGovernanceTool(pipeline, progress, task_id)
            options = {"rule_version": RULE_VERSION, "score_version": SCORE_VERSION}
            if source_version:
                options["source_version"] = source_version
            result = tool.execute(options)
            if not result.success:
                raise ValueError(result.error)
            return {"task_id": task_id, "status": "completed", "report": json.loads(result.data)}
        except Exception as error:
            failed_phase = f"{phase}失败"
            failure_path = self.output_dir / task_id / "failure.json"
            if not failure_path.is_file():
                failure_path.parent.mkdir(parents=True, exist_ok=True)
                failure_path.write_text(json.dumps({"task_id": task_id, "phase": failed_phase,
                                                    "error": str(error)}, ensure_ascii=False), encoding="utf-8")
            raise ValueError(f"{failed_phase}：{error}") from error

    def get(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            return None
        report_path = self.output_dir / task_id / "report.json"
        if report_path.is_file():
            return {"task_id": task_id, "status": "completed",
                    "report": json.loads(report_path.read_text(encoding="utf-8"))}
        return None
