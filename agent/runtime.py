"""装配入口共享的服务。插件通过注册提供能力。"""

from agent.core import run_chat_turn
from agent.llm import OpenAIProvider
from agent.registry import create_default_registry
from agent.session import SessionStore
from agent.tool_execution import execute_tool
from agent.tracer import Tracer


class Runtime:
    def __init__(self, config):
        self.config = config
        self._services = {}

    def mount(self, install, **options):
        install(self, **options)

    def provide(self, name, service):
        if name in self._services:
            raise ValueError(f'服务已注册：{name}')
        self._services[name] = service

    def __contains__(self, name):
        return name in self._services

    def __getitem__(self, name):
        return self._services[name]


class Models:
    """同一入口共享正常对话和结果解读使用的连接。"""

    def __init__(self, config, factory=None, mock=False):
        self.config = config
        self.factory = factory
        self.mock = mock
        self.reload()

    def reload(self):
        api_key = self.config.get('api_key')
        self.provider = (OpenAIProvider(api_key, self.config['model'], self.config['api_base'])
                         if api_key and not self.mock else None)

    def chat(self, mock_model=None):
        if self.factory:
            return self.factory()
        if self.mock:
            return mock_model
        if self.provider is None:
            raise ValueError('未设置 API Key，请先配置 config.json 或环境变量。')
        return self.provider


def install_coding(runtime):
    timeout = runtime.config.get('shell_timeout', 30)
    runtime.provide('tools', lambda workspace: create_default_registry(timeout, workspace=workspace))
    runtime.provide('execute_tool', execute_tool)
    runtime.provide('chat_loop', run_chat_turn)
    runtime.provide('tracer', lambda: Tracer(runtime.config['traces_dir']))


def default_runtime(config, store=None, llm_factory=None, mock=False):
    """默认装配。其他入口可以用同一组服务，也可以独立装配。"""
    runtime = Runtime(config)
    runtime.provide('sessions', store if store is not None else SessionStore())
    runtime.provide('models', Models(config, llm_factory, mock))
    runtime.mount(install_coding)
    return runtime


def install_workflows(runtime):
    from agent.workflows import Workflows

    runtime.provide('workflows', Workflows())
