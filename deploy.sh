#!/bin/bash

# Control-Simple Telegram Bot 部署脚本
# 适用于 Ubuntu/Debian 系统

set -e  # 遇到错误时退出

echo "🚀 开始部署 Control-Simple Telegram Bot..."

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查是否为 root 用户
if [[ $EUID -eq 0 ]]; then
   echo -e "${RED}错误: 请不要使用 root 用户运行此脚本${NC}"
   exit 1
fi

# 检查 Python 版本
echo -e "${YELLOW}检查 Python 版本...${NC}"
python3 --version

# 更新系统包
echo -e "${YELLOW}更新系统包...${NC}"
sudo apt update

# 安装必要的系统包
echo -e "${YELLOW}安装系统依赖...${NC}"
sudo apt install -y python3 python3-pip python3-venv curl wget git

# 检查项目目录
if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}错误: 未找到 requirements.txt 文件${NC}"
    echo "请确保在项目根目录运行此脚本"
    exit 1
fi

# 创建虚拟环境
echo -e "${YELLOW}创建虚拟环境...${NC}"
if [ -d ".venv" ]; then
    echo "虚拟环境已存在，跳过创建"
else
    python3 -m venv .venv
fi

# 激活虚拟环境
echo -e "${YELLOW}激活虚拟环境...${NC}"
source .venv/bin/activate

# 升级 pip
echo -e "${YELLOW}升级 pip...${NC}"
pip install --upgrade pip

# 安装 Python 依赖
echo -e "${YELLOW}安装 Python 依赖...${NC}"
pip install -r requirements.txt

# 检查配置文件
echo -e "${YELLOW}检查配置文件...${NC}"
if [ ! -f "orchestrator/config.yml" ]; then
    echo -e "${RED}警告: 未找到 config.yml 文件${NC}"
    echo "请手动创建 orchestrator/config.yml 文件"
else
    echo -e "${GREEN}✓ 找到 config.yml 文件${NC}"
fi

if [ ! -f "orchestrator/watchlist.yml" ]; then
    echo -e "${RED}警告: 未找到 watchlist.yml 文件${NC}"
    echo "请手动创建 orchestrator/watchlist.yml 文件"
else
    echo -e "${GREEN}✓ 找到 watchlist.yml 文件${NC}"
fi

# 设置文件权限
echo -e "${YELLOW}设置文件权限...${NC}"
chmod +x orchestrator/bot.py
if [ -f "orchestrator/config.yml" ]; then
    chmod 600 orchestrator/config.yml
fi

# 测试导入
echo -e "${YELLOW}测试模块导入...${NC}"
cd orchestrator
python -c "import bot; print('✓ Bot 模块导入成功')" || {
    echo -e "${RED}✗ Bot 模块导入失败${NC}"
    exit 1
}
cd ..

# 创建 systemd 服务文件
echo -e "${YELLOW}创建 systemd 服务...${NC}"
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

sudo tee /etc/systemd/system/telegram-bot.service > /dev/null <<EOF
[Unit]
Description=Telegram Bot for Freqtrade Control
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR/orchestrator
Environment=PATH=$CURRENT_DIR/.venv/bin
ExecStart=$CURRENT_DIR/.venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 重新加载 systemd
sudo systemctl daemon-reload

echo -e "${GREEN}✅ 部署完成！${NC}"
echo ""
echo -e "${YELLOW}下一步操作：${NC}"
echo "1. 编辑配置文件: nano orchestrator/config.yml"
echo "2. 启动服务: sudo systemctl start telegram-bot"
echo "3. 查看状态: sudo systemctl status telegram-bot"
echo "4. 查看日志: sudo journalctl -u telegram-bot -f"
echo "5. 设置开机自启: sudo systemctl enable telegram-bot"
echo ""
echo -e "${YELLOW}手动运行测试：${NC}"
echo "source .venv/bin/activate"
echo "cd orchestrator"
echo "python bot.py"
