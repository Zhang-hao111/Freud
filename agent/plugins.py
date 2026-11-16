"""发现并装配已安装的 Freud 插件。"""

import logging
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)


def install_plugins(runtime):
    workflows = runtime['workflows']
    for plugin in sorted(entry_points(group='freud.plugins'), key=lambda item: item.name):
        try:
            with workflows.registration():
                runtime.mount(plugin.load())
        except Exception as exc:
            logger.warning('加载插件 %s 失败: %s', plugin.name, exc)
