"""发现并装配已安装的 Freud 插件。"""

from importlib.metadata import entry_points


def install_plugins(runtime):
    for plugin in sorted(entry_points(group='freud.plugins'), key=lambda item: item.name):
        try:
            runtime.mount(plugin.load())
        except Exception as exc:
            raise RuntimeError(f'加载插件 {plugin.name} 失败: {exc}') from exc
