"""工作流注册、会话状态和回复事件，供交互入口调用。"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from agent.core import build_chat_system_prompt
from agent.types import AssistantMessage, SystemMessage, UserMessage


@dataclass
class WorkflowReply:
    content: str
    evidence: dict | None = None


@dataclass(frozen=True)
class TaskField:
    name: str
    label: str
    placeholder: str
    kind: Literal['text', 'integer', 'integer-list']
    location: Literal['options', 'request'] = 'options'


@dataclass(frozen=True)
class WorkflowTask:
    label: str
    description: str
    hint: str
    run: Callable
    followup: Callable | None = None
    result_label: str = '结果'
    fields: tuple[TaskField, ...] = ()
    advanced: bool = False


class Workflows:
    def __init__(self, result_llm=None):
        self.result_llm = result_llm
        self.plugins = {}
        self._artifacts = {}
        self._commands = {}

    def register(self, name, task: WorkflowTask):
        if name in self.plugins:
            raise ValueError(f'插件已注册：{name}')
        self.plugins[name] = task

    def catalog(self):
        return [{
            'id': name, 'label': task.label, 'description': task.description,
            'hint': task.hint, 'result_label': task.result_label,
            'fields': [asdict(field) for field in task.fields], 'advanced': task.advanced,
        } for name, task in self.plugins.items()]

    def supports_followup(self, name):
        task = self.plugins.get(name)
        return task is not None and task.followup is not None

    def context(self, session):
        return session.data.get('workflows', {}).get('followup_context')

    def clear_context(self, session):
        session.data.setdefault('workflows', {})['followup_context'] = None
        session.save()

    def register_followup_commands(self, name, *commands):
        if not self.supports_followup(name):
            raise ValueError(f'任务不支持结果追问：{name}')
        for command in commands:
            if command in self._commands:
                raise ValueError(f'追问命令已注册：{command}')
            self._commands[command] = name

    def register_artifact(self, name, reader):
        if name in self._artifacts:
            raise ValueError(f'产物已注册：{name}')
        self._artifacts[name] = reader

    def artifact(self, session, name, task_id, algorithm=None):
        reader = self._artifacts.get(name)
        return reader(session, task_id, algorithm) if reader else None

    def run(self, session, text, request, emit):
        messages = session.messages
        state = session.data.setdefault('workflows', {})
        connected = True
        def send(event):
            nonlocal connected
            if connected:
                try:
                    emit(event)
                except (BrokenPipeError, ConnectionResetError):
                    connected = False
        try:
            if not messages:
                messages.append(SystemMessage(content=build_chat_system_prompt(session.data['workspace'])))
            session.note_title(text)
            messages.append(UserMessage(content=text))
            session.save(messages)
            mode = request.get('mode')
            followup = mode == 'followup'
            target = (self._commands.get(text.rstrip('。！?？'), request.get('target'))
                      if followup else mode)
            plugin = self.plugins.get(target)
            handler = (plugin.followup if followup else plugin.run) if plugin else None
            if handler is None:
                raise ValueError('未知任务类型')
            reply = handler(session, state, text, request, send)
            if not followup or target != request.get('target'):
                state['followup_context'] = ({'target': target, 'message_index': len(messages) - 1}
                                             if plugin.followup else None)
            use_model = self.result_llm is not None and reply.evidence is not None
            if followup and use_model:
                followup_state = state.get('followup_context') or {'target': target}
                state['followup_context'] = followup_state
                start = followup_state.setdefault('message_index', len(messages) - 1)
                recent = [m.content[:500] for m in messages[start:-1] if isinstance(m, UserMessage)][-4:]
                send({'type': 'progress', 'content': '正在根据登记结果回答'})
                try:
                    reply.content = self.result_llm.answer(text, reply.evidence, recent_questions=recent)
                    reply.content = '**LLM 解读**\n\n' + reply.content
                except Exception:
                    reply.content = '模型暂不可用，以下为登记结果。\n\n' + reply.content
            message = AssistantMessage(content=reply.content)
            messages.append(message)
            session.save(messages)
            send({'type': 'assistant', 'content': reply.content, 'workflow_result': True})
            if not followup and use_model:
                try:
                    explanation = self.result_llm.answer(text, reply.evidence, summary=True)
                    interpretation = '**LLM 解读**\n\n' + explanation
                except Exception:
                    interpretation = 'LLM 解读暂不可用。'
                heading, _, body = reply.content.partition('\n\n')
                message.content = f'{heading}\n\n{interpretation}\n\n{body}'
                session.save(messages)
                send({'type': 'assistant_update', 'content': message.content})
        except Exception as exc:
            answer = f'任务未完成：{exc}'
            messages.append(AssistantMessage(content=answer))
            session.save(messages)
            send({'type': 'error', 'content': answer})
