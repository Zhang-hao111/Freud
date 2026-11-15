"""注册 MovieLens 治理、训练、分析和结果问答。"""

import json
import os
import re
from pathlib import Path

from agent.task_llm import analysis_evidence, governance_evidence, training_evidence
from agent.workflows import TaskField, WorkflowReply, WorkflowTask
from analysis.reporting import query_markdown, training_details, training_overview
from analysis.service import AnalysisService, grounded_follow_up
from analysis.tools import load_json
from analysis.training import AnalysisTrainer
from governance.pipeline import HadoopPipeline
from governance.service import GovernanceService, explain

ROOT = Path(__file__).resolve().parent.parent
TRAINING_COMMANDS = ('六种算法', '查看六种算法', '查看六个算法', '训练报告', '查看训练报告', '下载训练报告')
REPORT_COMMANDS = ('完整结果', '查看完整结果', '查看报告', '下载报告', '结果总结')
MARKDOWN_TYPE = 'text/markdown; charset=utf-8'


class MovieLens:
    def __init__(self, source=None, governance_output=None, analysis_output=None):
        self.governance_output = Path(governance_output or ROOT / 'governance-runs')
        self.analysis_output = Path(analysis_output or ROOT / 'analysis-runs')
        hadoop = os.environ.get('HADOOP_CMD', 'hadoop')
        self.governance_service = GovernanceService(
            source or ROOT / 'ml-1m' / 'ml-1m', self.governance_output, hadoop,
            os.environ.get('HADOOP_STREAMING_JAR'), os.environ.get('HADOOP_PYTHON', 'python3'),
        )

    def governance_report(self, session, task_id, _variant):
        if task_id not in session.data.get('workflows', {}).get('governance_ids', []):
            return None
        job = self.governance_service.get(task_id)
        if not job or job['status'] != 'completed':
            return None
        clean = self.governance_output / task_id / 'cleaned'
        return HadoopPipeline.markdown(job['report'], clean).encode(), MARKDOWN_TYPE

    def training_report(self, session, task_id, _variant):
        if task_id not in session.data.get('workflows', {}).get('training_ids', []):
            return None
        path = self.analysis_output / task_id / 'registry.json'
        return (AnalysisTrainer.markdown(load_json(path)).encode(), MARKDOWN_TYPE) if path.is_file() else None

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

    def followup_governance(self, session, state, text, _request, _emit):
        job = self.governance_service.get(state.get('governance_id', ''))
        if not job or job['status'] != 'completed':
            raise ValueError('本对话没有已完成的迭代一任务。')
        if text.rstrip('。！?？') in REPORT_COMMANDS or '清洗后数据样例' in text:
            return WorkflowReply(governance_reply(self, session, job))
        evidence = None if any(word in text for word in ('样例', '例子', '原始记录')) else governance_evidence(job['report'])
        return WorkflowReply(explain(job['report'], text), evidence)

    def followup_analysis(self, session, state, text, _request, _emit):
        job = state.get('analysis_job')
        if not job:
            raise ValueError('本对话没有已完成的迭代二分析。')
        if text.rstrip('。！?？') in (*REPORT_COMMANDS, '查询样例', '查看坐标'):
            return WorkflowReply(analysis_reply(session, job))
        return WorkflowReply(grounded_follow_up(job, text), analysis_evidence(job))

    def install_governance(self, workflows):
        workflows.register('governance', WorkflowTask(
            '清洗与质量评估', '迭代一 · Hadoop 数据治理', '描述清洗和质量评估需求。',
            self.governance, self.followup_governance, '迭代一结果',
        ))
        workflows.register_artifact('governance-report', self.governance_report)

    def install_analysis(self, workflows):
        workflows.register('training', WorkflowTask(
            '训练分析模型', '迭代二 · 使用清洗后的数据', '留空时使用本对话最近的清洗结果。',
            self.training, self.followup_training, '训练结果',
            fields=(TaskField('iteration1_id', '迭代一任务 ID', '迭代一任务 ID（留空使用当前对话）', 'text', 'request'),),
            advanced=True,
        ))
        workflows.register('analysis', WorkflowTask(
            '查询分析结果', '迭代二 · 分类、聚类与降维', '描述分类、聚类或降维问题。',
            self.analysis, self.followup_analysis, '分析结果',
            fields=(TaskField('user_ids', '用户 ID', '用户 ID，多个用逗号分隔', 'integer-list'),
                    TaskField('movie_id', '电影 ID', '电影 ID（分类需要）', 'integer')),
            advanced=True,
        ))
        workflows.register_followup_commands('training', *TRAINING_COMMANDS)
        workflows.register_artifact('training-report', self.training_report)
        workflows.register_artifact('analysis-report', self.analysis_report)
        workflows.register_artifact('plot', self.plot)


def governance_reply(context, session, job):
    task_id = job['task_id']
    report = HadoopPipeline.markdown(job['report'], context.governance_output / task_id / 'cleaned')
    return report + f"\n[下载治理评估报告](/api/workflows/{session.data['id']}/governance-report/{task_id})"


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


def analysis_reply(session, job):
    answer = query_markdown(job, f"/api/workflows/{session.data['id']}")
    return answer + '\n\n实际参数和完整结果：\n```json\n' + json.dumps(job['results'], ensure_ascii=False, indent=2) + '\n```'

def install_movie_lens(runtime, source=None, governance_output=None, analysis_output=None):
    movie_lens = MovieLens(source, governance_output, analysis_output)
    workflows = runtime['workflows']
    movie_lens.install_governance(workflows)
    movie_lens.install_analysis(workflows)
