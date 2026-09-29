"""Run the versioned MovieLens governance workflow on Hadoop Streaming."""

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path

from agent.types import BaseTool, ToolResult


TABLES = ("users", "movies", "ratings")
RULE_VERSION = "ml1m-governance-v1"
SCORE_VERSION = "ml1m-five-dim-v1"
WORKER = Path(__file__).with_name("worker.py")


def digest(path):
    hash_value = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hash_value.update(block)
    return hash_value.hexdigest()


def cutoff(days, fraction):
    total = sum(days.values())
    if not total:
        return None
    running = 0
    for day, count in sorted(days.items()):
        running += count
        if running >= total * fraction:
            return f"{day}T23:59:59Z"
    return None


class HadoopPipeline:
    def __init__(self, source_dir, output_dir, hadoop="hadoop", streaming_jar=None,
                 hdfs_root="/freud/governance", python_command="python3"):
        self.source_dir = Path(source_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.hadoop = hadoop
        self.streaming_jar = streaming_jar or os.environ.get("HADOOP_STREAMING_JAR")
        self.hdfs_root = hdfs_root.rstrip("/")
        self.python_command = python_command
        if not self.streaming_jar:
            raise ValueError("缺少 Hadoop Streaming jar；设置 HADOOP_STREAMING_JAR")
        if not shutil.which(self.hadoop):
            raise RuntimeError(f"找不到 Hadoop 可执行文件：{self.hadoop}")
        for table in TABLES:
            if not (self.source_dir / f"{table}.dat").is_file():
                raise FileNotFoundError(f"缺少原始数据：{self.source_dir / (table + '.dat')}")

    def command(self, args):
        result = subprocess.run(args, capture_output=True, text=True, errors="replace")
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise RuntimeError(f"Hadoop 命令失败 ({result.returncode}): {' '.join(args[:4])}\n{detail}")
        return result

    def stage(self, local_dir, hdfs_dir):
        self.command([self.hadoop, "fs", "-mkdir", "-p", hdfs_dir])
        for table in TABLES:
            self.command([self.hadoop, "fs", "-put", "-f", str(local_dir / f"{table}.dat"), hdfs_dir])
        return [f"{hdfs_dir}/{table}.dat" for table in TABLES]

    def job(self, name, inputs, output, mapper, reducer, references, combiner=None):
        files = [f"{self.worker_hdfs}#worker.py",
                 *(f"{references}/{table}.dat#{table}.dat" for table in ("users", "movies"))]
        command = [self.hadoop, "jar", self.streaming_jar, "-D", "mapreduce.job.name=" + name,
                   "-D", "mapreduce.job.reduces=" + ("0" if reducer is None else "1"),
                   "-files", ",".join(files)]
        for input_path in inputs:
            command.extend(["-input", input_path])
        command.extend(["-output", output, "-mapper", f"{self.python_command} worker.py {mapper}"])
        if reducer:
            command.extend(["-reducer", f"{self.python_command} worker.py {reducer}"])
        if combiner:
            command.extend(["-combiner", f"{self.python_command} worker.py {combiner}"])
        self.command(command)

    def fetch(self, hdfs_dir, target):
        self.command([self.hadoop, "fs", "-getmerge", hdfs_dir, str(target)])
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"Hadoop 结果为空：{hdfs_dir}")

    def split_clean(self, source, destination, metadata_only=False):
        destination.mkdir(parents=True, exist_ok=True)
        counts = Counter()
        handles = {table: open(destination / f"{table}.dat", "w", encoding="iso-8859-1", newline="\n") for table in TABLES}
        quarantine = None if metadata_only else open(destination / "quarantine.jsonl", "w", encoding="utf-8")
        try:
            with open(source, encoding="utf-8") as records:
                for line in records:
                    record = json.loads(line)
                    if metadata_only and record["table"] == "ratings":
                        continue
                    action = record["action"]
                    counts[action] += 1
                    if action in {"keep", "repair"}:
                        handles[record["table"]].write(record["value"] + "\n")
                    elif quarantine:
                        quarantine.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            for handle in handles.values():
                handle.close()
            if quarantine:
                quarantine.close()
        return dict(counts)

    def score(self, name, audit_path, hdfs_dir, local_dir, references):
        self.job(name, [audit_path], hdfs_dir, "score-map", "score-reduce", references,
                 "score-combine")
        result_path = local_dir / f"{name}.jsonl"
        self.fetch(hdfs_dir, result_path)
        with open(result_path, encoding="utf-8") as source:
            lines = [line for line in source if line.strip()]
        if len(lines) != 1:
            raise RuntimeError(f"评分输出应为一条记录，实际 {len(lines)} 条")
        return json.loads(lines[0])

    def run(self, on_progress=None, task_id=None, expected_source_version=None):
        phase = "准备运行"

        def notify(current_phase):
            nonlocal phase
            phase = current_phase
            if on_progress:
                on_progress(current_phase)

        run_id = task_id or uuid.uuid4().hex
        run_dir = self.output_dir / run_id
        run_dir.mkdir(parents=True)
        hdfs_run = f"{self.hdfs_root}/{run_id}"
        source_hashes = {table: digest(self.source_dir / f"{table}.dat") for table in TABLES}
        source_version = hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()[:16]
        try:
            if expected_source_version and expected_source_version != source_version:
                raise ValueError(f"原始数据版本不匹配：请求 {expected_source_version}，实际 {source_version}")
            notify("上传原始数据")
            worker_dir = hdfs_run + "/bin"
            self.command([self.hadoop, "fs", "-mkdir", "-p", worker_dir])
            self.command([self.hadoop, "fs", "-put", "-f", str(WORKER), worker_dir])
            self.worker_hdfs = worker_dir + "/worker.py"
            raw_paths = self.stage(self.source_dir, hdfs_run + "/raw")
            notify("Hadoop 检查清洗前数据")
            self.job("audit-before", raw_paths, hdfs_run + "/audit-before", "audit-map", "audit-reduce", hdfs_run + "/raw")
            self.fetch(hdfs_run + "/audit-before", run_dir / "audit-before.jsonl")
            notify("Hadoop 计算清洗前五维评分")
            before = self.score("score-before", hdfs_run + "/audit-before", hdfs_run + "/score-before", run_dir, hdfs_run + "/raw")
            notify("Hadoop 清洗并核对跨表关联")
            self.job("clean-initial", [hdfs_run + "/audit-before"], hdfs_run + "/clean-initial",
                     "clean-map", None, hdfs_run + "/raw")
            self.fetch(hdfs_run + "/clean-initial", run_dir / "clean-initial.jsonl")
            references = run_dir / "references"
            self.split_clean(run_dir / "clean-initial.jsonl", references, metadata_only=True)
            self.stage(references, hdfs_run + "/references")
            self.job("clean-final", [hdfs_run + "/audit-before"], hdfs_run + "/clean-final",
                     "clean-map", None, hdfs_run + "/references")
            self.fetch(hdfs_run + "/clean-final", run_dir / "clean-final.jsonl")
            clean_dir = run_dir / "cleaned"
            actions = self.split_clean(run_dir / "clean-final.jsonl", clean_dir)
            if not (clean_dir / "ratings.dat").stat().st_size:
                raise RuntimeError("清洗后没有有效评分，无法确定时间边界")
            notify("Hadoop 复评清洗后数据")
            clean_paths = self.stage(clean_dir, hdfs_run + "/cleaned")
            self.job("audit-after", clean_paths, hdfs_run + "/audit-after", "audit-map", "audit-reduce", hdfs_run + "/cleaned")
            after = self.score("score-after", hdfs_run + "/audit-after", hdfs_run + "/score-after", run_dir, hdfs_run + "/cleaned")
            if before["total"] != after["total"] + actions.get("deduplicate", 0) + actions.get("quarantine", 0):
                raise RuntimeError("清洗记录数无法与去重、隔离和复评结果对账")
            t1, t2 = cutoff(after["days"], 0.8), cutoff(after["days"], 0.9)
            if not t1 or not t2 or t1 >= t2:
                raise RuntimeError("评分时间分布不足，无法生成严格递增的 T1/T2")
            cleaned_hashes = {table: digest(clean_dir / f"{table}.dat") for table in TABLES}
            data_version = hashlib.sha256(json.dumps(cleaned_hashes, sort_keys=True).encode()).hexdigest()[:16]
            changes = []
            for name in before["scores"]:
                old, new = before["scores"][name], after["scores"][name]
                if old is not None and new is not None:
                    changes.append(f"{name} {old}→{new}（{new-old:+.4f} 分）")
            remaining = "、".join(f"{name} {count}" for name, count in sorted(after["issues"].items())) or "规则内未检出异常"
            explanation = ("五维变化：" + "；".join(changes) + "。"
                           f"修复 {actions.get('repair', 0)}、去重 {actions.get('deduplicate', 0)}、"
                           f"隔离 {actions.get('quarantine', 0)} 条。清洗后异常：{remaining}。"
                           "评分分母随隔离和去重改变，不能据此声称被隔离记录已修复。")
            examples = []
            represented = set()
            with open(run_dir / "clean-final.jsonl", encoding="utf-8") as records:
                for line in records:
                    record = json.loads(line)
                    category = (record["action"], tuple(record["issues"]))
                    if record["action"] != "keep" and category not in represented:
                        examples.append({"table": record["table"], "raw": record["raw"],
                                         "value": record["value"], "issues": record["issues"],
                                         "action": record["action"]})
                        represented.add(category)
                        if len(examples) == 12:
                            break
            report = {"status": "completed", "task_id": run_id, "source_version": source_version,
                      "data_version": data_version, "rule_version": RULE_VERSION, "score_version": SCORE_VERSION,
                      "source_sha256": source_hashes, "cleaned_sha256": cleaned_hashes,
                      "hdfs_run": hdfs_run, "T1": t1, "T2": t2,
                      "before": before, "after": after, "actions": actions,
                      "disposition_examples": examples, "explanation": explanation,
                      "limitations": ["准确性仅验证类型、值域和时间范围，不能证实自报人口属性或电影信息真实。",
                                      "时效性以数据集发布日 2003-02-28 为参照，仅评价评分时间是否落在此前一年；不代表当前时效。",
                                      "标题年份缺失、同名电影和异常评分行为无法自动判定，未据此删除记录。",
                                      "隔离与去重减少参与评分的记录，分数提高不等于原始信息已修复。"],
                      "method": {"Accurate": "必填、数值类型与值域、时间范围通过率；不验证现实真实性",
                                 "Complete": "必填字段与字段数量完整率",
                                 "Unique": "业务键无重复或冲突的记录率；评分键为用户/电影/时间",
                                 "Up-to-date": "评分时间在 2002-02-28 至 2003-02-28 的比例，仅评分表参与",
                                 "Consistent": "字段格式、类别、外键与同键内容无冲突的记录率"}}
            (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (run_dir / "report.md").write_text(self.markdown(report), encoding="utf-8")
            notify("已完成")
            return report
        except Exception as error:
            (run_dir / "failure.json").write_text(json.dumps(
                {"task_id": run_id, "phase": phase, "error": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
            raise

    @staticmethod
    def markdown(report):
        lines = ["# MovieLens 1M 数据治理评估报告", "", f"任务：{report['task_id']}",
                 f"原始版本：{report['source_version']}；清洗版本：{report['data_version']}",
                 f"规则：{report['rule_version']}；评分：{report['score_version']}",
                 f"T1：{report['T1']}；T2：{report['T2']}", "",
                 "| 维度 | 清洗前 | 清洗后 | 变化 |", "| --- | ---: | ---: | ---: |"]
        for name in report["method"]:
            before, after = report["before"]["scores"][name], report["after"]["scores"][name]
            lines.append(f"| {name} | {before} | {after} | {round(after-before, 4) if before is not None and after is not None else 'N/A'} |")
        lines += ["", "## 处置", "", f"原始记录：{report['before']['total']}；清洗后：{report['after']['total']}",
                  f"修复：{report['actions'].get('repair', 0)}；去重：{report['actions'].get('deduplicate', 0)}；隔离：{report['actions'].get('quarantine', 0)}",
                  "", "## Agent 解释", "", report["explanation"], "", "## 评分依据", ""]
        lines += [f"- {name}：{method}" for name, method in report["method"].items()]
        lines += ["", "## 局限", ""] + [f"- {item}" for item in report["limitations"]]
        lines += ["", "## 清洗前异常计数", ""] + [f"- {name}：{count}" for name, count in sorted(report["before"]["issues"].items())]
        lines += ["", "## 代表性处置记录", ""]
        lines += [f"- {item['table']}：{item['raw']} → {item['action']}（{', '.join(item['issues'])}）"
                  for item in report["disposition_examples"]]
        return "\n".join(lines) + "\n"


class HadoopGovernanceTool(BaseTool):
    name = "govern_movielens"
    description = "在 Hadoop 上完成 MovieLens 1M 清洗前评分、清洗、清洗后评分和版本化报告"
    parameters = {"type": "object", "properties": {
        "source_version": {"type": "string"}, "rule_version": {"type": "string"},
        "score_version": {"type": "string"}}, "additionalProperties": False}

    def __init__(self, pipeline, on_progress=None, task_id=None):
        self.pipeline = pipeline
        self.on_progress = on_progress
        self.task_id = task_id

    def execute(self, args):
        if set(args) - {"source_version", "rule_version", "score_version"}:
            return ToolResult(False, error="存在未支持的配置项")
        if args.get("rule_version", RULE_VERSION) != RULE_VERSION or args.get("score_version", SCORE_VERSION) != SCORE_VERSION:
            return ToolResult(False, error="当前只支持已登记的默认清洗和评分规则版本")
        try:
            report = self.pipeline.run(self.on_progress, self.task_id, args.get("source_version"))
            return ToolResult(True, data=json.dumps(report, ensure_ascii=False))
        except Exception as error:
            return ToolResult(False, error=str(error))
