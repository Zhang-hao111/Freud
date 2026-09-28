"""配置加载 — 从 config.json 加载，环境变量可覆盖。"""

import json
import os
from pathlib import Path

# 配置文件位置（当前目录优先，其次源码仓库根，最后全局 ~/.agent-harness）
CONFIG_FILENAME = "config.json"
GLOBAL_CONFIG_DIR = ".agent-harness"


def _candidate_paths() -> list[Path]:
    """按优先级返回候选配置路径。"""
    return [
        Path.cwd() / CONFIG_FILENAME,
        Path(__file__).resolve().parent.parent / CONFIG_FILENAME,
        Path.home() / GLOBAL_CONFIG_DIR / CONFIG_FILENAME,
    ]


def _find_config() -> Path | None:
    """从候选位置查找 config.json。"""
    for p in _candidate_paths():
        if p.exists():
            return p
    return None


def _default_cfg_path() -> Path:
    """默认配置文件路径（全局 ~/.agent-harness，避免依赖 cwd）。"""
    return Path.home() / GLOBAL_CONFIG_DIR / CONFIG_FILENAME


def load_config() -> dict:
    """加载配置：代码默认值 < config.json < 环境变量。"""
    cfg: dict = {
        "api_key": "",
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com",
        "max_steps": 30,
        "shell_timeout": 30,
        "memory_path": str(Path.home() / ".agent-harness" / "memory.json"),
        "traces_dir": str(Path.home() / ".agent-harness" / "traces"),
        "workspace": os.path.abspath("."),
    }

    # config.json 覆盖
    cfg_path = _find_config()
    if cfg_path:
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.update({k: data[k] for k in cfg if k in data})
        except (json.JSONDecodeError, OSError):
            pass

    # 环境变量覆盖
    env_map = {
        "api_key": ("LLM_API_KEY", "DEEPSEEK_API_KEY"),
        "model": ("LLM_MODEL",),
        "api_base": ("LLM_API_BASE",),
        "max_steps": ("MAX_STEPS",),
        "shell_timeout": ("SHELL_TIMEOUT",),
        "memory_path": ("MEMORY_PATH",),
        "traces_dir": ("TRACES_DIR",),
        "workspace": ("WORKSPACE",),
    }
    for key, vars in env_map.items():
        for v in vars:
            val = os.environ.get(v)
            if val:
                cfg[key] = val
                break

    cfg["max_steps"] = int(cfg["max_steps"])
    cfg["shell_timeout"] = int(cfg["shell_timeout"])

    return cfg


def save_config(updates: dict) -> Path:
    """合并更新到 config.json 并写入磁盘。"""
    cfg_path = _find_config() or _default_cfg_path()

    existing = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.update(updates)
    cfg_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return cfg_path


def ensure_config() -> Path:
    """如 config.json 不存在则创建默认配置。"""
    cfg_path = _find_config()
    if cfg_path:
        return cfg_path
    cfg_path = _default_cfg_path()
    defaults = {
        "api_key": "",
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com",
    }
    cfg_path.write_text(
        json.dumps(defaults, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return cfg_path