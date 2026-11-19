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
        runtime = default_runtime({}, mock=True)
        runtime.mount(install_workflows)
        install = Mock()
        entry = Mock(name='entry')
        entry.name = 'example'
        entry.load.return_value = install
        with patch('agent.plugins.entry_points', return_value=[entry]):
            install_plugins(runtime)
        install.assert_called_once_with(runtime)

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

    def test_broken_plugin_does_not_prevent_healthy_plugins_from_loading(self):
        runtime = default_runtime({}, mock=True)
        runtime.mount(install_workflows)
        workflows = runtime['workflows']
        handler = Mock()
        task = WorkflowTask('示例任务', '', '', handler, handler, commands=('查看示例',),
                            artifacts=(('example-report', handler),))

        def broken(runtime):
            runtime.provide('plugin-service', 'broken')
            runtime['workflows'].register('broken', task)
            raise ValueError('broken')

        def healthy(runtime):
            runtime.provide('plugin-service', 'healthy')
            runtime['workflows'].register('healthy', task)

        unavailable, bad, good = Mock(), Mock(), Mock()
        unavailable.name = 'a-unavailable'
        unavailable.load.side_effect = ImportError('missing dependency')
        bad.name = 'b-broken'
        bad.load.return_value = broken
        good.name = 'c-healthy'
        good.load.return_value = healthy
        with patch('agent.plugins.entry_points', return_value=[good, bad, unavailable]):
            with self.assertLogs('agent.plugins', level='WARNING') as logs:
                install_plugins(runtime)

        self.assertEqual([entry['id'] for entry in workflows.catalog()], ['healthy'])
        self.assertEqual(runtime['plugin-service'], 'healthy')
        self.assertIn('a-unavailable', logs.output[0])
        self.assertIn('b-broken', logs.output[1])


if __name__ == '__main__':
    unittest.main()
