"""MovieLens 查询和图表任务。"""

import json
from pathlib import Path

from movielens_plugin.task_llm import analysis_evidence
from agent.workflows import TaskField, WorkflowReply, WorkflowTask
from movielens_plugin.analysis.reporting import query_markdown
from movielens_plugin.analysis.service import AnalysisService, grounded_follow_up

from .shared import MARKDOWN_TYPE, REPORT_COMMANDS


class AnalysisTasks:

    def _analysis_job(self, session, task_id):
        job = session.data.get('workflows', {}).get('analysis_jobs', {}).get(task_id)
        if job and Path(job['registry_dir']).resolve().is_relative_to(self.analysis_output.resolve()):
            return job
        return None

    def analysis_report(self, session, task_id, _variant):
        job = self._analysis_job(session, task_id)
        if job:
            report = query_markdown(job, f"/api/workflows/{session.data['id']}", include_report_link=False)
            return report.encode(), MARKDOWN_TYPE
        return None

    def plot(self, session, task_id, algorithm):
        job = self._analysis_job(session, task_id)
        if not job:
            return None
        root = Path(job['registry_dir']).resolve()
        result = next((item for item in job['results'] if item['algorithm'] == algorithm), None)
        if not result or not result.get('plot_path'):
            return None
        path = (root / result['plot_path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path.read_bytes(), 'image/svg+xml; charset=utf-8'

    def analysis(self, session, state, text, request, emit):
        options = request.get('options') or {}
        allowed = {'user_id', 'movie_id', 'user_ids', 'algorithm', 'parameters', 'target_dimensions', 'information_threshold', 'cluster_algorithm', 'cluster_parameters'}
        if not isinstance(options, dict) or set(options) - allowed:
            raise ValueError('分析参数无效')
        training_id = state.get('training_id')
        results = self.analysis_output / training_id if training_id else self.analysis_output
        governance_id = state.get('analysis_governance_id') or state.get('governance_id')
        expected_input = None
        if governance_id:
            governance = self.governance_service.get(governance_id)
            if not governance or governance['status'] != 'completed':
                raise ValueError('本对话引用的清洗结果不可用')
            expected_input = governance['report']
        service = AnalysisService(results, expected_input)
        job = service.run(text, **options, on_progress=lambda phase: emit({'type': 'progress', 'content': phase}))
        state['analysis_job'] = job
        state.setdefault('analysis_jobs', {})[job['task_id']] = job
        return WorkflowReply(analysis_reply(session, job), analysis_evidence(job))

    def followup_analysis(self, session, state, text, _request, _emit):
        job = state.get('analysis_job')
        if not job:
            raise ValueError('本对话没有已完成的迭代二分析。')
        if text.rstrip('。！?？') in (*REPORT_COMMANDS, '查询样例', '查看坐标'):
            return WorkflowReply(analysis_reply(session, job))
        return WorkflowReply(grounded_follow_up(job, text), analysis_evidence(job))

    def install_analysis(self, workflows):
        workflows.register('analysis', WorkflowTask(
            '查询分析结果', '迭代二 · 分类、聚类与降维', '描述分类、聚类或降维问题。',
            self.analysis, self.followup_analysis, '分析结果',
            fields=(TaskField('user_ids', '用户 ID', '用户 ID，多个用逗号分隔', 'integer-list'),
                    TaskField('movie_id', '电影 ID', '电影 ID（分类需要）', 'integer')),
            advanced=True,
            artifacts=(('analysis-report', self.analysis_report), ('plot', self.plot)),
            result_llm=self.result_llm,
        ))


def analysis_reply(session, job):
    answer = query_markdown(job, f"/api/workflows/{session.data['id']}")
    return answer + '\n\n实际参数和完整结果：\n```json\n' + json.dumps(job['results'], ensure_ascii=False, indent=2) + '\n```'
