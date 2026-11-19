"""config 全局路径回归测试（阶段三：全局 freud 修复）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config


class ConfigPathTest(unittest.TestCase):
    def test_default_path_uses_home(self):
        """默认配置必须落在全局 ~/.agent-harness，而不是 cwd。"""
        with mock.patch.dict('os.environ', {'HOME': '/tmp/fakehome'}):
            p = config._default_cfg_path()
            self.assertEqual(p, Path('/tmp/fakehome/.agent-harness/config.json'))

    def test_candidates_include_global(self):
        paths = config._candidate_paths()
        self.assertTrue(any('.agent-harness' in str(p) for p in paths))

    def test_global_fallback_found(self):
        """cwd 和仓库根都没有配置时，全局位置可被找到。"""
        with tempfile_temporary_chdir(), tempfile.TemporaryDirectory() as root:
            home = Path(root)
            settings = home / '.agent-harness' / 'config.json'
            settings.parent.mkdir()
            settings.write_text('{}', encoding='utf-8')
            with mock.patch.object(Path, 'home', return_value=home), \
                 mock.patch.object(config, '__file__', home / 'checkout' / 'agent' / 'config.py'):
                self.assertEqual(config._find_config(), settings)


def tempfile_temporary_chdir():
    import contextlib, os

    @contextlib.contextmanager
    def _ctx():
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                yield
            finally:
                os.chdir(old)

    return _ctx()


if __name__ == '__main__':
    unittest.main()
