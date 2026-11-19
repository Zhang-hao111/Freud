"""装配 MovieLens 治理、训练和分析任务。"""

import os
from pathlib import Path

from movielens_plugin.tasks.analysis import AnalysisTasks
from movielens_plugin.tasks.governance import GovernanceTasks
from movielens_plugin.tasks.training import TrainingTasks
from movielens_plugin.task_llm import TaskLLM
from movielens_plugin.governance.service import GovernanceService


class MovieLens(GovernanceTasks, TrainingTasks, AnalysisTasks):
    def __init__(self, root, provider=None, source=None, governance_output=None, analysis_output=None):
        root = Path(root)
        self.result_llm = TaskLLM(provider) if provider else None
        self.governance_output = Path(governance_output or root / 'governance-runs')
        self.analysis_output = Path(analysis_output or root / 'analysis-runs')
        hadoop = os.environ.get('HADOOP_CMD', 'hadoop')
        self.governance_service = GovernanceService(
            source or root / 'ml-1m' / 'ml-1m', self.governance_output, hadoop,
            os.environ.get('HADOOP_STREAMING_JAR'), os.environ.get('HADOOP_PYTHON', 'python3'),
        )


def install_movie_lens(runtime, source=None, governance_output=None, analysis_output=None):
    root = os.environ.get('MOVIELENS_ROOT', runtime.config.get('launch_dir', runtime.config['workspace']))
    movie_lens = MovieLens(root, runtime['models'].provider, source, governance_output, analysis_output)
    workflows = runtime['workflows']
    movie_lens.install_governance(workflows)
    movie_lens.install_training(workflows)
    movie_lens.install_analysis(workflows)
