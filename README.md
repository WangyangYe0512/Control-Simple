# Control-Simple Telegram Bot

一个用于管理 Freqtrade 实例的 Telegram 机器人。

## 🚀 快速部署

### 1. 环境准备

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Python 3.11+ 和 pip
sudo apt install python3 python3-pip python3-venv -y

# 检查 Python 版本
python3 --version
```

### 2. 项目设置

```bash
# 克隆或上传项目到服务器
cd /path/to/your/project

# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置文件

确保以下文件存在并正确配置：

#### `orchestrator/config.yml`
```yaml
telegram:
  token: "YOUR_BOT_TOKEN"
  chat_id: -1002700964642
  topic_id: 6253
  admins: [6886636661, 7581225816]
  require_arm: false
  arm_ttl_minutes: 15

freqtrade:
  long:
    base_url: "http://18.143.200.143:8083/"
    user: "freqtrade"
    pass: "your_password"
  short:
    base_url: "http://18.142.146.98:8083/"
    user: "freqtrade"
    pass: "your_password"

defaults:
  stake: 500.0
  delay_ms: 300
  poll_timeout_sec: 60
  poll_interval_sec: 2
```

#### `orchestrator/watchlist.yml`
```yaml
basket: ["SOL/USDT:USDT", "DOGE/USDT:USDT"]
```

### 4. 运行 Bot

```bash
# 激活虚拟环境
source .venv/bin/activate

# 进入 orchestrator 目录
cd orchestrator

# 运行 bot
python bot.py
```

### 5. 后台运行 (推荐)

#### 使用 screen
```bash
# 安装 screen
sudo apt install screen -y

# 创建新的 screen 会话
screen -S telegram-bot

# 在 screen 中运行 bot
source .venv/bin/activate
cd orchestrator
python bot.py

# 按 Ctrl+A 然后按 D 来分离会话
# 使用 screen -r telegram-bot 重新连接
```

#### 使用 systemd 服务 (推荐)
```bash
# 创建服务文件
sudo nano /etc/systemd/system/telegram-bot.service
```

服务文件内容：
```ini
[Unit]
Description=Telegram Bot for Freqtrade Control
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/your/project/orchestrator
Environment=PATH=/path/to/your/project/.venv/bin
ExecStart=/path/to/your/project/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用服务：
```bash
# 重新加载 systemd
sudo systemctl daemon-reload

# 启用服务
sudo systemctl enable telegram-bot

# 启动服务
sudo systemctl start telegram-bot

# 查看状态
sudo systemctl status telegram-bot

# 查看日志
sudo journalctl -u telegram-bot -f
```

## 🔧 故障排除

### 常见问题

1. **权限问题**
```bash
# 确保文件权限正确
chmod +x orchestrator/bot.py
chmod 600 orchestrator/config.yml
```

2. **Python 版本问题**
```bash
# 检查 Python 版本
python3 --version

# 如果版本太低，安装 Python 3.11+
sudo apt install python3.11 python3.11-venv python3.11-pip
```

3. **网络连接问题**
```bash
# 测试网络连接
curl -I http://18.143.200.143:8083/
curl -I http://18.142.146.98:8083/
```

4. **依赖包问题**
```bash
# 重新安装依赖
pip install --upgrade pip
pip install -r requirements.txt --force-reinstall
```

### 日志查看

```bash
# 查看实时日志
tail -f /var/log/telegram-bot.log

# 查看 systemd 日志
sudo journalctl -u telegram-bot -f
```

## 📋 命令列表

- `/start` - 启动机器人
- `/help` - 显示帮助信息
- `/basket` - 显示当前篮子配置
- `/bs <pairs...>` - 设置篮子 (快捷命令)
- `/basket_set <pairs...>` - 设置篮子
- `/status` - 显示实例状态
- `/stake <amount>` - 设置每笔名义
- `/go_long` - 开多确认
- `/go_short` - 开空确认
- `/flat` - 全平确认

## 🔐 安全说明

- 确保 `config.yml` 文件权限设置为 600
- 定期更新 bot token
- 监控 bot 日志
- 使用防火墙限制访问
