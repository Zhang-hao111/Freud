"""MovieLens 模型训练及报告。"""

import re

from movielens_plugin.task_llm import training_evidence
from agent.workflows import TaskField, WorkflowReply, WorkflowTask
from movielens_plugin.analysis.reporting import training_details, training_overview
from movielens_plugin.analysis.tools import load_json
from movielens_plugin.analysis.training import AnalysisTrainer

from .shared import MARKDOWN_TYPE, REPORT_COMMANDS, TRAINING_COMMANDS


class TrainingTasks:

    def training_report(self, session, task_id, _variant):
        if task_id not in session.data.get('workflows', {}).get('training_ids', []):
            return None
        path = self.analysis_output / task_id / 'registry.json'
        return (AnalysisTrainer.markdown(load_json(path)).encode(), MARKDOWN_TYPE) if path.is_file() else None

    def training(self, session, state, text, request, emit):
        task_id = request.get('iteration1_id') or state.get('governance_id')
        if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{32}', task_id):
            raise ValueError('请提供已完成的迭代一任务 ID，或先在此对话中运行迭代一。')
        options = request.get('options') or {}
        allowed = {'min_history', 'clusters', 'dbscan_eps', 'dbscan_min_samples', 'target_dimensions'}
        if not isinstance(options, dict) or set(options) - allowed:
            raise ValueError('训练参数无效')
        trainer = AnalysisTrainer(self.governance_output / task_id, self.analysis_output, **options)
        registry = trainer.run(on_progress=lambda phase: emit({'type': 'progress', 'content': phase}))
        state['training_id'] = registry['task_id']
        state['analysis_governance_id'] = task_id
        state.setdefault('training_ids', []).append(registry['task_id'])
        return WorkflowReply(training_reply(session, registry), training_evidence(registry))

    def followup_training(self, session, state, text, _request, _emit):
        task_id = state.get('training_id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', task_id):
            raise ValueError('本对话没有已完成的迭代二训练。')
        report = self.analysis_output / task_id / 'registry.json'
        if not report.is_file():
            raise ValueError('训练报告不存在。')
        registry = load_json(report)
        show_report = text.rstrip('。！?？') in (*TRAINING_COMMANDS, *REPORT_COMMANDS)
        reply = training_reply(session, registry, None if show_report else text)
        return WorkflowReply(reply, None if show_report else training_evidence(registry))

    def install_training(self, workflows):
        workflows.register('training', WorkflowTask(
            '训练分析模型', '迭代二 · 使用清洗后的数据', '留空时使用本对话最近的清洗结果。',
            self.training, self.followup_training, '训练结果',
            fields=(TaskField('iteration1_id', '迭代一任务 ID', '迭代一任务 ID（留空使用当前对话）', 'text', 'request'),),
            advanced=True,
            commands=TRAINING_COMMANDS,
            artifacts=(('training-report', self.training_report),),
            result_llm=self.result_llm,
        ))


def training_reply(session, registry, question=None):
    details = training_details(registry)
    link = f"[下载训练报告](/api/workflows/{session.data['id']}/training-report/{registry['task_id']})"
    if question and not any(word in question for word in ('完整', '报告', '总结')):
        sections = {
            '分类对照': ('分类', '选型', 'f1', 'auc'),
            '聚类对照': ('聚类', '噪声', 'dbscan'),
            '降维对照': ('降维', '保留', '耗时', '资源', 'pca', 'svd'),
            '时间切分': ('切分', '时间', 't1', 't2'),
            '实际参数与模型版本': ('版本', '参数'),
            '局限': ('局限', '限制', '可信'),
        }
        matches = [part for part in details.split('\n#### ')[1:]
                   if any(word in question.lower() for word in sections[part.splitlines()[0]])]
        if matches:
            return '#### ' + '\n#### '.join(matches) + '\n\n' + link
    return training_overview(registry) + '\n\n' + link + '\n\n<!-- freud-training-details -->\n' + details
