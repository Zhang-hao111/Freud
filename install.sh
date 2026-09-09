#!/usr/bin/env bash
# coding-agent 一键安装脚本
# 用法: curl -fsSL <url> | bash
# 或: bash <(curl -fsSL <url>)

set -euo pipefail

REPO_URL="https://github.com/your-username/Lab-coding-agent"
INSTALL_DIR="${HOME}/.coding-agent"
BIN_DIR="${HOME}/.local/bin"

echo "==> 安装 coding-agent ..."

# 1. 检查 uv
if ! command -v uv &>/dev/null; then
    echo "   正在安装 uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # 重新加载 PATH
    export PATH="${HOME}/.local/bin:${PATH}"
fi

# 2. 克隆仓库
if [ -d "$INSTALL_DIR" ]; then
    echo "   更新已有安装 ..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "   克隆仓库到 $INSTALL_DIR ..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# 3. uv tool install
echo "   安装 CLI 命令 ..."
uv tool install --reinstall "$INSTALL_DIR" 2>/dev/null || uv tool install "$INSTALL_DIR"

# 4. 配置 .env
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "=========================================="
    echo "  首次安装完成！"
    echo "  请编辑 $INSTALL_DIR/.env"
    echo "  填入你的 API Key 后即可使用。"
    echo "=========================================="
fi

# 5. 检查 PATH
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    echo ""
    echo "  提示: 将 ${BIN_DIR} 添加到 PATH 以直接运行 coding-agent:"
    echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    echo "  source ~/.bashrc"
fi

echo ""
echo "  安装成功！运行以下命令开始使用:"
echo "  coding-agent --file <任务描述.md>"