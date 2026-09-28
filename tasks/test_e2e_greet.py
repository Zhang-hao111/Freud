import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from e2e_greet import greet


class TestGreet(unittest.TestCase):
    def test_world(self):
        self.assertEqual(greet("世界"), "你好, 世界")

    def test_other_name(self):
        self.assertEqual(greet("Python"), "你好, Python")

    def test_empty(self):
        self.assertEqual(greet(""), "你好, ")


if __name__ == "__main__":
    unittest.main()
