"""Tracer — 记录 agent 每一步的决策与执行结果，用于调试和展示。"""

import json
import os
import time
from pathlib import Path
import re
from agent.types import TraceEntry, Action


class Tracer:
    """可观测性记录器，每步记录 action + result + timestamp。"""

    def __init__(self, traces_dir: str, session_id: str | None = None):
        self.traces_dir = traces_dir
        self.session_id = session_id
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
        os.makedirs(self.traces_dir, exist_ok=True, mode=0o700)

        def _serialize(obj):
            if isinstance(obj, Action):
                return {
                    'type': obj.type,
                    'tool': obj.tool,
                    'tool_call_id': obj.tool_call_id,
                }
            if hasattr(obj, '__dict__'):
                return obj.__dict__
            return str(obj)

        filepath = os.path.join(
            self.traces_dir,
            f'trace-{self.session_id or "local"}-{time.time_ns()}.json',
        )
        with os.fdopen(os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
            json.dump(
                [{'step': e.step, 'action': e.action, 'timestamp': e.timestamp, 'result_chars': len(e.result)}
                 for e in self._entries],
                f,
                ensure_ascii=False,
                indent=2,
                default=_serialize,
            )
        self._entries.clear()

    @staticmethod
    def delete_session(traces_dir, session_id):
        if re.fullmatch(r'[a-zA-Z0-9-]+', session_id):
            for path in Path(traces_dir).glob(f'trace-{session_id}-*.json'):
                path.unlink(missing_ok=True)
