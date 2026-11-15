import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.plugins import install_plugins
from agent.runtime import default_runtime, install_workflows
from agent.workflows import WorkflowTask


class PluginDiscoveryTests(unittest.TestCase):
    def test_no_installed_plugins_keeps_workflows_empty(self):
        runtime = default_runtime({'workspace': '.', 'traces_dir': '.', 'shell_timeout': 1}, mock=True)
        runtime.mount(install_workflows)
        with patch('agent.plugins.entry_points', return_value=[]):
            install_plugins(runtime)
        self.assertEqual(runtime['workflows'].catalog(), [])

    def test_discovered_plugin_is_mounted(self):
        runtime = Mock()
        install = Mock()
        entry = Mock(name='entry')
        entry.name = 'example'
        entry.load.return_value = install
        with patch('agent.plugins.entry_points', return_value=[entry]):
            install_plugins(runtime)
        runtime.mount.assert_called_once_with(install)

    def test_uninstalled_task_followup_is_hidden_without_erasing_session(self):
        runtime = default_runtime({}, mock=True)
        runtime.mount(install_workflows)
        workflows = runtime['workflows']
        context = {'target': 'example'}
        session = SimpleNamespace(data={'workflows': {'followup_context': context}})
        self.assertIsNone(workflows.context(session))
        handler = Mock()
        workflows.register('example', WorkflowTask('示例任务', '', '', handler, handler))
        self.assertEqual(workflows.context(session), context)

    def test_plugin_load_failure_names_the_plugin(self):
        entry = Mock(name='entry')
        entry.name = 'example'
        entry.load.side_effect = ValueError('broken')
        with patch('agent.plugins.entry_points', return_value=[entry]):
            with self.assertRaisesRegex(RuntimeError, '加载插件 example 失败'):
                install_plugins(Mock())


if __name__ == '__main__':
    unittest.main()
