import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from governance.pipeline import HadoopGovernanceTool, cutoff
from governance.web import explain


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
        report = {"before": {"scores": {"Unique": 75}, "issues": {"duplicate": 1}},
                  "after": {"scores": {"Unique": 100}}, "actions": {"deduplicate": 1},
                  "method": {"Unique": "业务键唯一率"}, "limitations": []}
        self.assertIn("75 → 100", explain(report, "Unique 分数为何变化？"))


if __name__ == "__main__":
    unittest.main()
