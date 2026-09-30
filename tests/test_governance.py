import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from governance.pipeline import HadoopGovernanceTool, HadoopPipeline, RULE_VERSION, SCORE_VERSION, cutoff
from governance.web import LLMExplainer, Handler, explain, report_digest
from governance.jobs import JobManager
from governance.tools import GetGovernanceJobTool, SubmitGovernanceJobTool, register_governance_tools
from agent.registry import ToolRegistry
from agent.types import AssistantMessage, LLMResponse


WORKER = Path(__file__).resolve().parent.parent / "governance" / "worker.py"


class GovernanceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "users.dat").write_bytes(b"1::M::25::12::00501\n2::F::18::1::10001\n")
        (self.directory / "movies.dat").write_bytes(b"10::Film (2000)::Drama\n")

    def worker(self, mode, content, input_file=None):
        environment = os.environ.copy()
        if input_file:
            environment["mapreduce_map_input_file"] = input_file
        result = subprocess.run([sys.executable, str(WORKER), mode], input=content,
                                capture_output=True, cwd=self.directory, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout

    def test_hadoop_workers_inspect_clean_and_score(self):
        raw = b"1::10::5::1014854400\n1::10::5::1014854400\n1::10::9::1014854401\n3::10::4::1014854402\n"
        mapped = self.worker("audit-map", raw, "/input/ratings.dat")
        audited = self.worker("audit-reduce", b"".join(sorted(mapped.splitlines(keepends=True))))
        records = [json.loads(line) for line in audited.splitlines()]
        self.assertEqual(sum("duplicate" in row["issues"] for row in records), 1)
        self.assertEqual(sum("domain" in row["issues"] for row in records), 1)
        self.assertEqual(sum("reference" in row["issues"] for row in records), 1)
        cleaned = [json.loads(line) for line in self.worker("clean-map", audited).splitlines()]
        self.assertEqual([row["action"] for row in cleaned].count("keep"), 1)
        self.assertEqual([row["action"] for row in cleaned].count("deduplicate"), 1)
        self.assertEqual([row["action"] for row in cleaned].count("quarantine"), 2)
        scored = self.worker("score-map", audited)
        combined = self.worker("score-combine", scored)
        report = json.loads(self.worker("score-reduce", combined))
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["scores"]["Unique"], 75.0)
        self.assertEqual(report["scores"]["Accurate"], 75.0)

    def test_conflicting_business_key_is_quarantined(self):
        raw = b"10::Film (2000)::Drama\n10::Other (2000)::Comedy\n"
        mapped = self.worker("audit-map", raw, "/input/movies.dat")
        audited = self.worker("audit-reduce", b"".join(sorted(mapped.splitlines(keepends=True))))
        cleaned = [json.loads(line) for line in self.worker("clean-map", audited).splitlines()]
        self.assertEqual([row["action"] for row in cleaned], ["quarantine", "quarantine"])

    def test_duplicate_survivor_is_deterministic_and_prefers_canonical_record(self):
        raw = b"10::  Film (2000)  ::Drama\n10::Film (2000)::Drama\n"
        mapped = self.worker("audit-map", raw, "/input/movies.dat").splitlines(keepends=True)
        forward = self.worker("audit-reduce", b"".join(mapped))
        reversed_output = self.worker("audit-reduce", b"".join(reversed(mapped)))
        self.assertEqual(forward, reversed_output)
        cleaned = [json.loads(line) for line in self.worker("clean-map", forward).splitlines()]
        self.assertEqual([row["action"] for row in cleaned], ["keep", "deduplicate"])
        self.assertEqual(cleaned[0]["raw"], "10::Film (2000)::Drama")

    def test_split_clean_keeps_deduplicates_out_of_quarantine_file(self):
        source = self.directory / "clean-final.jsonl"
        records = [
            {"table": "ratings", "value": "1::10::5::1", "action": "keep"},
            {"table": "ratings", "value": "1::10::5::1", "action": "deduplicate"},
            {"table": "ratings", "value": "1::10::9::2", "action": "quarantine"},
        ]
        source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        destination = self.directory / "cleaned"

        actions = HadoopPipeline.split_clean(None, source, destination)

        quarantined = [json.loads(line) for line in (destination / "quarantine.jsonl").read_text().splitlines()]
        self.assertEqual(actions, {"keep": 1, "deduplicate": 1, "quarantine": 1})
        self.assertEqual([record["action"] for record in quarantined], ["quarantine"])

    def test_missing_hadoop_fails_without_placeholder_result(self):
        class MissingPipeline:
            def run(self, *_):
                raise RuntimeError("Hadoop unavailable")

        result = HadoopGovernanceTool(MissingPipeline()).execute({})
        self.assertFalse(result.success)
        self.assertIn("Hadoop unavailable", result.error)

    def test_time_boundary_requires_data(self):
        self.assertIsNone(cutoff({}, 0.8))
        self.assertEqual(cutoff({"2001-01-01": 8, "2001-01-02": 2}, 0.8), "2001-01-01T23:59:59Z")

    def test_follow_up_uses_only_report_values(self):
        report = self.report()
        self.assertIn("75 → 100", explain(report, "Unique 分数为何变化？"))

    def test_follow_up_distinguishes_remaining_issues_volume_and_limitations(self):
        report = self.report()
        remaining = explain(report, "清洗后还有哪些问题未解决？")
        self.assertIn("时间范围错误 1", remaining)
        self.assertNotIn("完全重复 2", remaining)
        volume = explain(report, "数据量如何变化？")
        self.assertIn("原始 10 条，清洗后 7 条", volume)
        self.assertNotIn("Accurate：", volume)
        basis = explain(report, "评分依据和局限是什么？")
        self.assertIn("评分依据", basis)
        self.assertIn("评价局限", basis)
        self.assertIn("不能验证现实真实性", basis)
        unverifiable = explain(report, "哪些事实无法验证？")
        self.assertIn("评价局限", unverifiable)
        self.assertNotIn("原始数据版本", unverifiable)
        version = explain(report, "评分版本、数据版本和 T1/T2 是什么？")
        self.assertIn("评分版本 scores-v1", version)
        self.assertNotIn("Accurate：", version)

    def test_request_body_must_be_json_object(self):
        self.assertEqual(self.body_json(b'{"prompt":"clean"}'), {"prompt": "clean"})
        for payload in (b"[]", b"null", b'"text"'):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "必须是对象"):
                    self.body_json(payload)

    def test_report_digest_keeps_facts_and_drops_day_distribution(self):
        report = self.report()
        digest = report_digest(report)
        self.assertIn("T1", digest)
        self.assertIn("scores", digest["before"])
        self.assertIn("rule_version", digest)
        self.assertNotIn("days", digest["before"])

    def test_llm_explainer_grounds_on_report_and_falls_back(self):
        report = self.report()

        class FakeProvider:
            def __init__(self, behavior):
                self.behavior, self.messages = behavior, None

            def chat(self, messages, tools, include_done=True, include_take_note=True):
                self.messages = messages
                if self.behavior == "raise":
                    raise RuntimeError("api down")
                return LLMResponse(message=AssistantMessage(content="依据报告：Unique 75 → 100。"))

        explainer = LLMExplainer.__new__(LLMExplainer)
        explainer.provider = FakeProvider("ok")
        self.assertIn("75 → 100", explainer.answer(report, "Unique 为什么变化？"))
        sent = explainer.provider.messages[0].content
        self.assertIn("rule_version", sent)
        self.assertIn("禁止编造", sent)
        explainer.provider.behavior = "raise"
        self.assertIsNone(explainer.answer(report, "任意问题"))
        explainer.provider.behavior = "ok"
        self.assertIsNone(explainer.answer(report, "   "))
        explainer.provider = None
        self.assertIsNone(explainer.answer(report, "任意问题"))

    @staticmethod
    def body_json(payload):
        handler = object.__new__(Handler)
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        return handler.body_json()

    @staticmethod
    def report():
        dimensions = ("Accurate", "Complete", "Unique", "Up-to-date", "Consistent")
        return {
            "before": {"scores": {name: 75 for name in dimensions},
                       "issues": {"duplicate": 2}, "total": 10},
            "after": {"scores": {name: 100 for name in dimensions},
                      "issues": {"timestamp": 1}, "total": 7},
            "actions": {"repair": 1, "deduplicate": 1, "quarantine": 2},
            "method": {name: f"{name} 规则" for name in dimensions},
            "limitations": ["不能验证现实真实性"],
            "explanation": "清洗后仍有时间范围问题。",
            "source_version": "a" * 16,
            "data_version": "b" * 16,
            "rule_version": "rules-v1",
            "score_version": "scores-v1",
            "T1": "2000-01-01T23:59:59Z",
            "T2": "2000-02-01T23:59:59Z",
            "disposition_examples": [],
        }


class FakePipeline:
    """测试替身：不碰 Hadoop，按 HadoopPipeline.run 的契约产出报告并落盘。"""

    def __init__(self, source_dir, output_dir, **settings):
        self.source_dir, self.output_dir, self.settings = source_dir, output_dir, settings

    def run(self, on_progress=None, task_id=None, expected_source_version=None):
        if expected_source_version and expected_source_version != "0123456789abcdef":
            raise ValueError(f"原始数据版本不匹配：请求 {expected_source_version}，实际 0123456789abcdef")
        if on_progress:
            on_progress("上传原始数据")
        report = {
            "task_id": task_id, "rule_version": RULE_VERSION, "score_version": SCORE_VERSION,
            "T1": "2003-01-01T23:59:59Z", "T2": "2003-02-01T23:59:59Z",
            "actions": {"repair": 1, "deduplicate": 0, "quarantine": 0},
            "explanation": "测试说明", "method": {"Accurate": "口径"}, "limitations": ["测试局限"],
            "disposition_examples": [], "days": {"2002-01-01": 1},
            "before": {"total": 3, "tables": {"ratings": 3}, "issues": {"duplicate": 1},
                       "scores": {"Accurate": 66.6667}},
            "after": {"total": 2, "tables": {"ratings": 2}, "issues": {}, "scores": {"Accurate": 100.0}},
        }
        run_dir = self.output_dir / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        (run_dir / "report.md").write_text("# 测试报告\n", encoding="utf-8")
        return report


class ExplodingPipeline:
    def __init__(self, source_dir, output_dir, **settings):
        pass

    def run(self, on_progress=None, task_id=None, expected_source_version=None):
        raise RuntimeError("模拟 Hadoop 失败")


class GovernanceAgentToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.manager = JobManager(self.directory / "source", self.directory / "runs",
                                  pipeline_factory=FakePipeline)

    def wait_for(self, manager, task_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = manager.get(task_id)
            if state and state["status"] != "running":
                return state
            time.sleep(0.02)
        self.fail("任务在超时前未结束")

    def test_job_runs_in_background_and_persists_report(self):
        job = self.manager.submit()
        final = self.wait_for(self.manager, job["task_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["phase"], "已完成")
        self.assertTrue((self.directory / "runs" / job["task_id"] / "report.md").is_file())

    def test_get_tool_returns_digest_without_day_distribution(self):
        submitted = self.manager.submit()
        self.wait_for(self.manager, submitted["task_id"])
        result = GetGovernanceJobTool(self.manager).execute({"task_id": submitted["task_id"]})
        self.assertTrue(result.success, result.error)
        payload = json.loads(result.data)
        self.assertEqual(payload["status"], "completed")
        self.assertNotIn("days", payload["report"])
        self.assertIn("scores", payload["report"]["before"])
        self.assertTrue(Path(payload["report_path"]).is_file())

    def test_submit_tool_rejects_bad_source_version(self):
        result = SubmitGovernanceJobTool(self.manager).execute({"source_version": "not-hex"})
        self.assertFalse(result.success)
        self.assertIn("版本格式错误", result.error)

    def test_get_tool_rejects_unknown_task(self):
        result = GetGovernanceJobTool(self.manager).execute({"task_id": "a" * 32})
        self.assertFalse(result.success)
        self.assertIn("任务不存在", result.error)

    def test_failure_is_recorded_on_disk(self):
        manager = JobManager(self.directory / "source2", self.directory / "runs2",
                             pipeline_factory=ExplodingPipeline)
        job = manager.submit()
        state = self.wait_for(manager, job["task_id"])
        self.assertEqual(state["status"], "failed")
        self.assertIn("模拟 Hadoop 失败", state["error"])
        failure = json.loads((self.directory / "runs2" / job["task_id"] / "failure.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(failure["phase"], "准备运行失败")

    def test_registration_skipped_without_hadoop_and_added_with_it(self):
        registry = ToolRegistry()
        self.assertEqual(register_governance_tools(
            registry, self.directory, self.directory,
            hadoop="freud-no-such-hadoop", streaming_jar="stub.jar"), [])
        self.assertEqual(registry.list(), [])
        names = register_governance_tools(registry, self.directory, self.directory,
                                          hadoop=sys.executable, streaming_jar="stub.jar")
        self.assertEqual(names, ["submit_governance_job", "get_governance_job"])
        self.assertEqual({tool.name for tool in registry.list()}, set(names))


if __name__ == "__main__":
    unittest.main()
