from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithWeb(build_py):
    def run(self):
        assets = Path(__file__).resolve().parent / 'agent' / 'web_dist'
        if not self.editable_mode and not (assets / 'index.html').is_file():
            raise RuntimeError('打包 wheel 前请在 web 目录运行 pnpm install --frozen-lockfile 和 pnpm build。')
        super().run()


setup(cmdclass={'build_py': BuildWithWeb})
