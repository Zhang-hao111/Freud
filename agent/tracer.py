"""Tracer — 记录 agent 每一步的决策与执行结果，用于调试和展示。"""

import json
import os
import time
from agent.types import TraceEntry, Action


class Tracer:
    """可观测性记录器，每步记录 action + result + timestamp。"""

    def __init__(self, traces_dir: str):
        self.traces_dir = traces_dir
        self._entries: list[TraceEntry] = []

    def record(self, step: int, action: Action, result: str, feedback: str | None = None):
        entry = TraceEntry(
            step=step,
            action=action,
            result=result,
            timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            feedback=feedback,
        )
        self._entries.append(entry)

    def get_trace(self) -> list[TraceEntry]:
        return list(self._entries)

    def flush(self):
        if not self._entries:
            return
        os.makedirs(self.traces_dir, exist_ok=True)

        def _serialize(obj):
            if isinstance(obj, Action):
                return {
                    'type': obj.type,
                    'tool': obj.tool,
                    'args': obj.args,
                    'answer': obj.answer,
                    'note_key': obj.note_key,
                    'note_value': obj.note_value,
                }
            if hasattr(obj, '__dict__'):
                return obj.__dict__
            return str(obj)

        filepath = os.path.join(
            self.traces_dir,
            f'trace-{int(time.time())}.json',
        )
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(
                [e.__dict__ for e in self._entries],
                f,
                ensure_ascii=False,
                indent=2,
                default=_serialize,
            )