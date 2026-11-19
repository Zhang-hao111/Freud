"""MovieLens 数据治理任务及报告。"""

from movielens_plugin.task_llm import governance_evidence
from agent.workflows import WorkflowReply, WorkflowTask
from movielens_plugin.governance.pipeline import HadoopPipeline
from movielens_plugin.governance.service import explain

from .shared import MARKDOWN_TYPE, REPORT_COMMANDS

class GovernanceTasks:

    def governance_report(self, session, task_id, _variant):
        if task_id not in session.data.get('workflows', {}).get('governance_ids', []):
            return None
        job = self.governance_service.get(task_id)
        if not job or job['status'] != 'completed':
            return None
        clean = self.governance_output / task_id / 'cleaned'
        return HadoopPipeline.markdown(job['report'], clean).encode(), MARKDOWN_TYPE

    def governance(self, session, state, text, request, emit):
        options = request.get('options') or {}
        if not isinstance(options, dict) or set(options) - {'source_version', 'rule_version', 'score_version'}:
            raise ValueError('治理配置无效')
        job = self.governance_service.run(text, **options,
                                          on_progress=lambda phase: emit({'type': 'progress', 'content': phase}))
        state.setdefault('governance_ids', []).append(job['task_id'])
        state['governance_id'] = job['task_id']
        state['analysis_governance_id'] = job['task_id']
        return WorkflowReply(governance_reply(self, session, job), governance_evidence(job['report']))

    def followup_governance(self, session, state, text, _request, _emit):
        job = self.governance_service.get(state.get('governance_id', ''))
        if not job or job['status'] != 'completed':
            raise ValueError('本对话没有已完成的迭代一任务。')
        if text.rstrip('。！?？') in REPORT_COMMANDS or '清洗后数据样例' in text:
            return WorkflowReply(governance_reply(self, session, job))
        evidence = None if any(word in text for word in ('样例', '例子', '原始记录')) else governance_evidence(job['report'])
        return WorkflowReply(explain(job['report'], text), evidence)

    def install_governance(self, workflows):
        workflows.register('governance', WorkflowTask(
            '清洗与质量评估', '迭代一 · Hadoop 数据治理', '描述清洗和质量评估需求。',
            self.governance, self.followup_governance, '迭代一结果',
            artifacts=(('governance-report', self.governance_report),),
            result_llm=self.result_llm,
        ))


def governance_reply(context, session, job):
    task_id = job['task_id']
    report = HadoopPipeline.markdown(job['report'], context.governance_output / task_id / 'cleaned')
    return report + f"\n[下载治理评估报告](/api/workflows/{session.data['id']}/governance-report/{task_id})"
