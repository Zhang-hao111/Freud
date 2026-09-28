"""FileMemory 持久化单元测试。"""
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.memory import FileMemory


class FileMemoryTest(unittest.TestCase):
    def test_write_read_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = f'{tmp}/mem.json'
            m1 = FileMemory(p)
            m1.write('k', 'v')
            self.assertEqual(m1.read('k'), 'v')
            m1.consolidate()
            m2 = FileMemory(p)
            self.assertEqual(m2.read('k'), 'v')

    def test_overwrite_updates_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = FileMemory(f'{tmp}/mem.json')
            m.write('k', 'v1')
            m.write('k', 'v2')
            self.assertEqual(m.read('k'), 'v2')

    def test_bare_filename_consolidate(self):
        """memory_path 无目录前缀时 consolidate 不应崩溃（问题 #5 回归）。"""
        with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
            m = FileMemory('mem.json')
            m.write('a', 'b')
            m.consolidate()
            self.assertTrue((Path(tmp) / 'mem.json').exists())

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = FileMemory(f'{tmp}/nonexistent.json')
            self.assertIsNone(m.read('anything'))


@contextlib.contextmanager
def _cwd(path: str):
    import os
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


if __name__ == '__main__':
    unittest.main()
