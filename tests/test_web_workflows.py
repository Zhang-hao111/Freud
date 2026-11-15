import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from agent.session import SessionStore
import agent.session as session_module
from agent.task_llm import TaskLLM
from agent.types import UserMessage
from agent.web import WebAgent
from agent.movielens import install_movie_lens
from agent.workflows import TaskField, WorkflowReply, WorkflowTask
from governance.service import GovernanceService, default_governance_root
from tests.test_analysis import create_iteration1_run
from tests import test_governance


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gov = self.root / 'governance'
        self.gov.mkdir()
        self.analysis = self.root / 'analysis'
        self.task_id = 'a' * 32
        create_iteration1_run(self.gov).rename(self.gov / self.task_id)
        self.store = SessionStore(self.root / 'sessions')
        self.app = WebAgent({'workspace': str(self.root), 'traces_dir': str(self.root / 'traces'), 'shell_timeout': 5, 'max_steps': 4}, self.store)
        self.app.runtime.mount(install_movie_lens, source=self.root / 'source',
                               governance_output=self.gov, analysis_output=self.analysis)
        self.flows = self.app.conversations.workflows
        self.session = self.store.new(workspace=str(self.root))

    def run_flow(self, mode, text, **kwargs):
        events = []
        self.app.conversations.run_workflow(self.session, text, {'mode': mode, **kwargs}, events.append)
        return events

    def test_local_filesystem_uses_writable_staging_and_hdfs_keeps_default(self):
        with patch.dict('os.environ', {}, clear=True), patch('governance.service.shutil.which', return_value='/opt/hadoop/bin/hadoop'), patch('governance.service.subprocess.run') as run:
            run.return_value = Mock(returncode=0, stdout='file:///\n')
            root = default_governance_root('hadoop')
            self.assertTrue(root.startswith(tempfile.gettempdir() + '/'))
            self.assertNotEqual(root, '/freud/governance')
            self.assertEqual(run.call_args.args[0], ['/opt/hadoop/bin/hdfs', 'getconf', '-confKey', 'fs.defaultFS'])
            run.return_value = Mock(returncode=0, stdout='hdfs://localhost:9000\n')
            self.assertEqual(default_governance_root('hadoop'), '/freud/governance')
        with patch.dict('os.environ', {'GOVERNANCE_HDFS_ROOT': '/custom/root'}), patch('governance.service.subprocess.run') as run:
            self.assertEqual(default_governance_root('hadoop'), '/custom/root')
            run.assert_not_called()

    def test_train_query_followup_and_artifacts_survive_session_reload(self):
        events = self.run_flow('training', '训练', iteration1_id=self.task_id)
        self.assertEqual(events[-1]['type'], 'assistant', events)
        self.assertTrue(any(e['type'] == 'progress' for e in events))
        self.assertIn('选型依据为 验证期 F1', events[-1]['content'])
        self.assertIn('聚类耗时 原始 → 降维', events[-1]['content'])
        self.assertTrue(self.flows.artifact(self.session, 'training-report', self.session.data['workflows']['training_id']))
        events = self.run_flow('analysis', '比较 PCA 和 SVD 降维', options={'user_ids': [1, 2]})
        self.assertEqual(events[-1]['type'], 'assistant', events)
        self.assertIn('/plot/', events[-1]['content'])
        self.assertIn('| UserID | 降维坐标 | 原始群体 | 降维后群体 |', events[-1]['content'])
        self.assertIn('图中每点对应参与实验的一份训练期画像', events[-1]['content'])
        self.session = self.store.list()[0]
        self.assertTrue(self.flows.artifact(self.session, 'plot', self.session.data['workflows']['analysis_job']['task_id'], 'pca'))
        query_id = self.session.data['workflows']['analysis_job']['task_id']
        content, mime = self.flows.artifact(self.session, 'analysis-report', query_id)
        self.assertTrue(mime.startswith('text/markdown'))
        self.assertIn('降维与聚类对照', content.decode())
        self.assertIn('不能换算成归属不变的用户比例', content.decode())
        self.assertIsNone(self.flows.artifact(self.store.new(workspace=str(self.root)), 'analysis-report', query_id))
        events = self.run_flow('followup', '评价指标', target='analysis')
        self.assertEqual(events[-1]['type'], 'assistant')
        self.assertIn('评价结果', events[-1]['content'])
        events = self.run_flow('followup', '查看六种算法', target='analysis')
        overview = events[-1]['content'].split('<!-- freud-training-details -->')[0]
        for algorithm in ('逻辑回归', '随机森林', 'K-Means', 'DBSCAN', 'PCA', 'Truncated SVD'):
            self.assertIn(algorithm, overview)
        self.assertEqual(self.session.data['workflows']['followup_context']['target'], 'training')

    def test_completed_analysis_becomes_followup_context_after_reload(self):
        self.run_flow('training', '训练', iteration1_id=self.task_id)
        self.assertEqual(self.session.data['workflows']['followup_context']['target'], 'training')
        self.run_flow('analysis', '比较 PCA 和 SVD 降维', options={'user_ids': [1, 2]})
        self.session = self.store.list()[0]
        self.assertEqual(self.session.data['workflows']['followup_context']['target'], 'analysis')
        events = []
        self.app.conversations.chat(self.session, '评价指标', events.append, followup=True)
        self.assertIn('评价结果', events[-1]['content'])

    def test_failed_task_does_not_replace_previous_followup_context(self):
        self.session.data['workflows'] = {'followup_context': {'target': 'analysis'}}
        self.session.save()
        self.run_flow('training', '训练', iteration1_id='invalid')
        self.assertEqual(self.session.data['workflows']['followup_context']['target'], 'analysis')

    def test_clear_followup_context_preserves_results(self):
        self.session.data['workflows'] = {'followup_context': {'target': 'analysis'}, 'analysis_job': {'task': 'reduction'}}
        self.session.save()
        self.app.conversations.clear_followup(self.session.data['id'])
        restored = self.app.conversations.session(self.session.data['id'])
        self.assertIsNone(restored.data['workflows']['followup_context'])
        self.assertIn('analysis_job', restored.data['workflows'])

    def test_cleared_followup_context_is_not_reused_from_an_old_snapshot(self):
        self.session.data['workflows'] = {'followup_context': {'target': 'analysis'}}
        self.session.save()
        self.app.conversations.clear_followup(self.session.data['id'])
        events = []
        self.app.conversations.chat(self.session, '现在走普通对话', events.append, followup=True)
        self.assertIn('未设置 API Key', events[-1]['content'])
        self.assertIsNone(self.flows.context(self.session))

    def test_governance_plugin_uses_actual_completed_job(self):
        report = test_governance.GovernanceWorkerTests.report()
        report['task_id'] = self.task_id
        report['before']['tables'] = {'users': 2, 'movies': 2, 'ratings': 6}
        report['after']['tables'] = {'users': 2, 'movies': 2, 'ratings': 3}
        report['disposition_examples'] = [{'table': 'ratings', 'raw': '1::10::6::1',
                                           'action': 'quarantine', 'issues': ['domain']}]
        job = {'task_id': self.task_id, 'status': 'completed', 'phase': '完成', 'report': report}
        with patch.object(GovernanceService, 'run', return_value=job), patch.object(GovernanceService, 'get', return_value=job):
            events = self.run_flow('governance', '清洗 MovieLens')
            download, _ = self.flows.artifact(self.session, 'governance-report', self.task_id)
        body = events[-1]['content']
        self.assertIn('| Unique | 75 | 100 | +25.0000 |', body)
        self.assertIn('修复 1 条、去重 1 条、隔离 2 条', body)
        self.assertIn('记录数与作业文档的标准 MovieLens 1M 不一致', body)
        self.assertIn('**隔离**（类型或值域错误）', body)
        self.assertIn('ratings.dat 首条记录 `1::11::5::', body)
        self.assertIn('清洗后仍检出 时间范围错误 1 条', download.decode())
        self.assertEqual(self.store.list()[0].data['workflows']['governance_id'], self.task_id)

    def test_analysis_rejects_training_with_stale_cutoff(self):
        self.run_flow('training', '训练', iteration1_id=self.task_id)
        report_path = self.gov / self.task_id / 'report.json'
        report = json.loads(report_path.read_text())
        report['T2'] = '2000-01-09T23:59:59Z'
        report_path.write_text(json.dumps(report))
        events = self.run_flow('analysis', '分类预测', options={'user_id': 1, 'movie_id': 10})
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('T1/T2 与当前清洗结果不一致', events[-1]['content'])

    def test_llm_explains_registered_results_and_falls_back_after_reload(self):
        self.session.save([UserMessage(content='与本次分析无关的旧消息')])
        self.run_flow('training', '训练', iteration1_id=self.task_id)
        provider = Mock()
        provider.complete_text.side_effect = ['两种模型可进行对照。', '这是预测概率。', TimeoutError()]
        self.flows.result_llm = TaskLLM(provider)
        events = self.run_flow('analysis', '分类预测', options={'user_id': 1, 'movie_id': 10})
        original = next(event for event in events if event['type'] == 'assistant')
        update = events[-1]
        self.assertIn('高分概率', original['content'])
        self.assertEqual(update['type'], 'assistant_update')
        self.assertIn('**LLM 解读**', update['content'])
        self.assertIn('高分概率', update['content'])
        prompt = json.loads(provider.complete_text.call_args.args[0][-1].content)
        self.assertEqual({item['algorithm'] for item in prompt['evidence']['results']},
                         {'logistic_regression', 'random_forest'})

        self.session = self.store.list()[0]
        self.assertEqual(self.session.messages[-1].content, update['content'])
        events = self.run_flow('followup', '这些概率代表真实评分吗？', target='analysis')
        self.assertIn('这是预测概率。', events[-1]['content'])
        prompt = json.loads(provider.complete_text.call_args.args[0][-1].content)
        self.assertEqual(prompt['recent_questions'], ['分类预测'])

        events = self.run_flow('followup', '评价指标', target='analysis')
        self.assertEqual(events[-1]['type'], 'assistant')
        self.assertIn('模型暂不可用', events[-1]['content'])
        self.assertIn('| 逻辑回归 | 测试期 |', events[-1]['content'])

    def test_reduction_followup_gives_llm_the_metric_limits(self):
        self.run_flow('training', '训练', iteration1_id=self.task_id)
        self.run_flow('analysis', '比较降维结果', options={'user_ids': [1, 2]})
        provider = Mock()
        provider.complete_text.return_value = 'ARI 校正了随机一致性。'
        self.flows.result_llm = TaskLLM(provider)
        events = self.run_flow('followup', 'ARI 是用户归属不变的比例吗？', target='analysis')
        self.assertIn('LLM 解读', events[-1]['content'])
        system, request = provider.complete_text.call_args.args[0]
        self.assertIn('禁止把 ARI 当成百分比', system.content)
        self.assertIn('禁止仅根据簇编号变化', system.content)
        evidence = json.loads(request.content)['evidence']
        self.assertTrue(all('adjusted_rand_index' in result['cluster_comparison']
                            for result in evidence['results']))

    def test_registration_and_bad_task_id(self):
        with self.assertRaises(ValueError):
            self.flows.register('training', self.flows.plugins['training'])
        events = self.run_flow('training', '训练', iteration1_id='../outside')
        self.assertEqual(events[-1]['type'], 'error')

    def test_plugin_supplies_form_followup_and_artifact(self):
        task_id = 'b' * 32

        def run(session, state, _text, request, _emit):
            state['example'] = request['options']['value']
            return WorkflowReply(f"结果 {state['example']}")

        def followup(session, state, _text, _request, _emit):
            return WorkflowReply(f"继续查看 {state['example']}")

        def read(session, requested_id, _variant):
            if requested_id == task_id and 'example' in session.data.get('workflows', {}):
                return b'example', 'text/markdown'

        def install(runtime):
            workflows = runtime['workflows']
            workflows.register('example', WorkflowTask(
                '示例任务', '检查插件装配', '', run, followup,
                fields=(TaskField('value', '值', '输入正整数', 'integer'),),
            ))
            workflows.register_artifact('example-report', read)

        self.app.runtime.mount(install)
        task = next(task for task in self.flows.catalog() if task['id'] == 'example')
        self.assertEqual(task['fields'][0]['name'], 'value')
        self.assertEqual(self.run_flow('example', '运行', options={'value': 7})[-1]['content'], '结果 7')
        events = []
        self.app.conversations.chat(self.session, '继续', events.append, followup=True)
        self.assertEqual(events[-1]['content'], '继续查看 7')
        self.assertEqual(self.flows.artifact(self.session, 'example-report', task_id)[0], b'example')

    def test_deleting_legacy_session_does_not_reimport_it(self):
        legacy = self.root / 'memory.json'
        legacy.write_text('[{"key":"a","value":"b"}]')
        with patch.object(session_module, 'LEGACY_MEMORY', legacy):
            store = SessionStore(self.root / 'legacy-sessions')
            app = WebAgent(self.app.runtime.config, store)
            session_id = store.list()[0].data['id']
            self.assertTrue(app.conversations.delete_session(session_id))
            self.assertEqual(SessionStore(store.dir).list(), [])
        self.assertTrue(legacy.exists())

    def test_delete_running_session_is_blocked(self):
        session_id = self.session.data['id']
        self.app.conversations.active.add(session_id)
        with self.assertRaises(ValueError):
            self.app.conversations.delete_session(session_id)
        self.assertTrue(self.session.path.exists())
        self.app.conversations.active.remove(session_id)
        self.assertTrue(self.app.conversations.delete_session(session_id))
        self.assertFalse(self.session.path.exists())
        self.assertFalse(self.app.conversations.delete_session(session_id))


if __name__ == '__main__':
    unittest.main()
