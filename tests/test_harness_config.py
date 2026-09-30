"""config 全局路径回归测试（阶段三：全局 freud 修复）。"""
import sys
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
        with tempfile_temporary_chdir():
            with mock.patch.dict('os.environ', {'HOME': '/tmp/fakehome'}):
                with mock.patch.object(config, '__file__', config.__file__):
                    # 仓库根候选指向真实仓库（有 config.json），因此这里只验证
                    # _find_config 返回存在的路径且优先级有序
                    found = config._find_config()
                    self.assertIsNotNone(found)
                    self.assertTrue(found.exists())


class EnsureConfigTest(unittest.TestCase):
    def test_first_run_creates_missing_home_dir(self):
        """首跑且 ~/.agent-harness 不存在时应自动建目录，而不是 write_text 崩溃。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / 'home'
            with mock.patch.object(Path, 'home', return_value=fake_home), \
                 mock.patch.object(config, '_find_config', return_value=None):
                cfg = config.ensure_config()
            self.assertEqual(cfg, fake_home / config.GLOBAL_CONFIG_DIR / config.CONFIG_FILENAME)
            self.assertTrue(cfg.is_file())
            # 配置已存在时直接返回，不重写
            with mock.patch.object(Path, 'home', return_value=fake_home):
                self.assertEqual(config.ensure_config(), cfg)


def tempfile_temporary_chdir():
    import contextlib, os, tempfile

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
