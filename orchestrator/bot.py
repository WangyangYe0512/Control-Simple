import yaml
import os
import re
import httpx
import time
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
try:
    from .auto_toggle import schedule_auto_toggle
except Exception:
    # 作为脚本运行时的兼容导入
    from auto_toggle import schedule_auto_toggle

def load_config():
    """加载配置文件"""
    config_file = 'config.yml'
    if not os.path.exists(config_file):
        print(f"错误：配置文件 {config_file} 不存在")
        print("请复制 config.example.yml 为 config.yml 并填写实际配置")
        print("命令：cp config.example.yml config.yml")
        exit(1)
    
    with open(config_file, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def load_basket() -> list[str]:
    """加载篮子并做基本校验"""
    try:
        with open('watchlist.yml', 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        basket = data.get('basket', [])
        if not isinstance(basket, list):
            print("错误：basket 必须是列表")
            return []
        
        # 大写、去重、格式校验
        validated_basket = []
        seen = set()
        
        for pair in basket:
            if not isinstance(pair, str):
                continue
                
            # 转换为大写
            pair_upper = pair.upper()
            
            # 去重
            if pair_upper in seen:
                continue
            seen.add(pair_upper)
            
            # 格式校验：BASE/QUOTE 或 BASE/QUOTE:SETTLE（期货）
            if re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_upper):
                # 现货格式，自动转换为期货格式
                validated_basket.append(f"{pair_upper}:USDT")
            elif re.match(r'^[A-Z0-9]+/[A-Z0-9]+:[A-Z0-9]+$', pair_upper):
                # 期货格式，直接使用
                validated_basket.append(pair_upper)
            else:
                print(f"警告：跳过无效格式的交易对 {pair}")
        
        return validated_basket
        
    except Exception as e:
        print(f"错误：加载篮子文件失败 - {e}")
        return []

def save_basket(basket: list[str]) -> bool:
    """保存篮子到文件"""
    try:
        data = {'basket': basket}
        with open('watchlist.yml', 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        return True
    except Exception as e:
        print(f"错误：保存篮子文件失败 - {e}")
        return False

class FTClient:
    """Freqtrade HTTP 客户端"""
    
    def __init__(self, base_url: str, user: str, passwd: str):
        """初始化客户端"""
        self.base_url = base_url.rstrip('/')
        self.session = httpx.Client(
            auth=(user, passwd),
            timeout=60.0  # 增加超时时间到60秒
        )
    
    def _request(self, method: str, path: str, json: Optional[Dict[Any, Any]] = None) -> Optional[Dict[Any, Any]]:
        """通用请求方法"""
        url = f"{self.base_url}{path}"
        
        try:
            response = self.session.request(method, url, json=json)
            
            # 处理 4xx/5xx 错误
            if response.status_code >= 400:
                error_text = response.text[:200] if response.text else f"HTTP {response.status_code}"
                
                # 解析常见错误并返回友好信息
                if "position for" in error_text and "already open" in error_text:
                    # 持仓已存在错误
                    return {"error": "position_exists", "message": "持仓已存在"}
                elif "No open order for trade_id" in error_text:
                    # 无开放订单错误
                    return {"error": "no_open_order", "message": "无开放订单"}
                elif "Symbol does not exist" in error_text:
                    # 交易对不存在错误
                    return {"error": "symbol_not_found", "message": "交易对不存在或未激活"}
                elif "timed out" in str(error_text):
                    # 超时错误
                    return {"error": "timeout", "message": "请求超时"}
                elif "Insufficient balance" in error_text or "insufficient" in error_text.lower():
                    # 余额不足错误
                    return {"error": "insufficient_balance", "message": "余额不足"}
                elif "Market is closed" in error_text or "market closed" in error_text.lower():
                    # 市场关闭错误
                    return {"error": "market_closed", "message": "市场已关闭"}
                elif "Rate limit" in error_text or "rate limit" in error_text.lower():
                    # 频率限制错误
                    return {"error": "rate_limit", "message": "请求频率过高，请稍后重试"}
                elif "Invalid pair" in error_text or "invalid pair" in error_text.lower():
                    # 无效交易对错误
                    return {"error": "invalid_pair", "message": "无效的交易对"}
                elif "Maintenance" in error_text or "maintenance" in error_text.lower():
                    # 维护中错误
                    return {"error": "maintenance", "message": "系统维护中"}
                
                # 只打印 5xx 服务器错误，4xx 客户端错误（如 404）是预期的
                if response.status_code >= 500:
                    print(f"HTTP 错误 {response.status_code}: {error_text}")
                    return {"error": "server_error", "message": f"服务器错误: {error_text[:100]}"}
                return None
            
            # 尝试解析 JSON
            try:
                return response.json()
            except Exception:
                return {"text": response.text}
                
        except Exception as e:
            print(f"请求失败 {method} {url}: {e}")
            if "timed out" in str(e):
                return {"error": "timeout", "message": "请求超时"}
            return {"error": "connection_error", "message": f"连接错误: {str(e)[:100]}"}
    
    def list_positions(self) -> list:
        """获取当前持仓列表"""
        # 根据 Freqtrade API 文档，/status 端点列出所有开放交易
        result = self._request("GET", "/api/v1/status")
        if result is not None:
            # /status 应该直接返回交易列表
            if isinstance(result, list):
                return result
            # 如果是字典，可能包含在某个字段中
            elif isinstance(result, dict) and "trades" in result:
                return result["trades"] if isinstance(result["trades"], list) else []
        return []
    
    def cancel_open_orders(self) -> bool:
        """取消所有开放订单"""
        # 文档中没有直接的取消所有订单端点，需要逐个取消
        # 先获取当前持仓，然后逐个取消其开放订单
        positions = self.list_positions()
        success = True
        for trade in positions:
            if isinstance(trade, dict) and "trade_id" in trade:
                trade_id = trade["trade_id"]
                result = self._request("DELETE", f"/api/v1/trades/{trade_id}/open-order")
                if result is None:
                    success = False
        return success
    
    def forcebuy(self, pair: str, stake: float) -> Optional[Dict[Any, Any]]:
        """强制开多仓"""
        # 使用 /forceenter 端点，side="long" 表示多仓
        data = {
            "pair": pair,
            "side": "long"
        }
        result = self._request("POST", "/api/v1/forceenter", json=data)
        
        # 如果请求超时但实际可能成功，尝试检查是否真的成功了
        if result is None:
            # 等待一下再检查持仓
            import time
            time.sleep(2)
            # 检查是否已经有这个交易对的持仓
            positions = self.list_positions()
            for pos in positions:
                if isinstance(pos, dict) and pos.get('pair') == pair and not pos.get('is_short', False):
                    # 找到了对应的多仓，说明实际成功了
                    return {"status": "success", "message": "Position found after timeout"}
        
        return result
    
    def forceshort(self, pair: str, stake: float) -> Optional[Dict[Any, Any]]:
        """强制开空仓"""
        # 使用 /forceenter 端点，side="short" 表示空仓
        data = {
            "pair": pair,
            "side": "short"
        }
        result = self._request("POST", "/api/v1/forceenter", json=data)
        
        # 如果请求超时但实际可能成功，尝试检查是否真的成功了
        if result is None:
            # 等待一下再检查持仓
            import time
            time.sleep(2)
            # 检查是否已经有这个交易对的持仓
            positions = self.list_positions()
            for pos in positions:
                if isinstance(pos, dict) and pos.get('pair') == pair and pos.get('is_short', False):
                    # 找到了对应的空仓，说明实际成功了
                    return {"status": "success", "message": "Position found after timeout"}
        
        return result
    
    def forcesell(self, pair: str) -> Optional[Dict[Any, Any]]:
        """强制平多仓"""
        # 需要先找到对应的 trade_id，然后使用 /forceexit
        positions = self.list_positions()
        for trade in positions:
            if (isinstance(trade, dict) and 
                trade.get("pair") == pair and 
                not trade.get("is_short", False)):  # 多仓
                trade_id = trade.get("trade_id")
                if trade_id:
                    data = {"tradeid": trade_id}
                    return self._request("POST", "/api/v1/forceexit", json=data)
        return None
    
    def forcecover(self, pair: str) -> Optional[Dict[Any, Any]]:
        """强制平空仓"""
        # 需要先找到对应的 trade_id，然后使用 /forceexit
        positions = self.list_positions()
        for trade in positions:
            if (isinstance(trade, dict) and 
                trade.get("pair") == pair and 
                trade.get("is_short", False)):  # 空仓
                trade_id = trade.get("trade_id")
                if trade_id:
                    data = {"tradeid": trade_id}
                    return self._request("POST", "/api/v1/forceexit", json=data)
        return None

    def start_trading(self) -> Optional[Dict[Any, Any]]:
        """启动实例交易 (相当于 /start)"""
        return self._request("POST", "/api/v1/start")

    def stop_trading(self) -> Optional[Dict[Any, Any]]:
        """停止实例交易 (相当于 /stop)"""
        return self._request("POST", "/api/v1/stop")
    
    def close_all_positions(self) -> Dict[str, Any]:
        """平仓所有持仓"""
        result = {
            'long_closed': [],
            'short_closed': [],
            'errors': []
        }
        
        try:
            positions = self.list_positions()
            if not positions:
                return result
            
            for trade in positions:
                if not isinstance(trade, dict):
                    continue
                    
                trade_id = trade.get("trade_id")
                pair = trade.get("pair")
                is_short = trade.get("is_short", False)
                
                if not trade_id or not pair:
                    continue
                
                try:
                    data = {"tradeid": trade_id}
                    response = self._request("POST", "/api/v1/forceexit", json=data)
                    
                    if response:
                        if is_short:
                            result['short_closed'].append(f"{pair} (空仓)")
                        else:
                            result['long_closed'].append(f"{pair} (多仓)")
                    else:
                        result['errors'].append(f"平仓失败: {pair}")
                        
                except Exception as e:
                    result['errors'].append(f"平仓 {pair} 时出错: {e}")
                    
        except Exception as e:
            result['errors'].append(f"获取持仓列表失败: {e}")
            
        return result


# 权限控制和武装机制
armed_until = None  # 武装到期时间

def is_admin(user_id: int) -> bool:
    """检查用户是否为管理员"""
    cfg = load_config()  # 每次调用时重新加载配置
    return user_id in cfg['telegram']['admins']

def is_armed() -> bool:
    """检查系统是否已武装"""
    global armed_until
    cfg = load_config()  # 每次调用时重新加载配置
    if not cfg['telegram']['require_arm']:
        return True  # 如果不需要武装，直接返回 True
    
    if armed_until is None:
        return False
    
    return datetime.now() < armed_until

def arm_system() -> timedelta:
    """武装系统，返回剩余时间"""
    global armed_until
    cfg = load_config()  # 每次调用时重新加载配置
    ttl_minutes = cfg['telegram']['arm_ttl_minutes']
    armed_until = datetime.now() + timedelta(minutes=ttl_minutes)
    return timedelta(minutes=ttl_minutes)

def get_remaining_arm_time() -> Optional[timedelta]:
    """获取武装剩余时间"""
    global armed_until
    if armed_until is None:
        return None
    
    remaining = armed_until - datetime.now()
    return remaining if remaining.total_seconds() > 0 else None


if __name__ == "__main__":
    # 加载配置
    cfg = load_config()
    
    # 打印关键字段（不打印 token）
    print("=== 配置加载成功 ===")
    print(f"Chat ID: {cfg['telegram']['chat_id']}")
    print(f"Topic ID: {cfg['telegram']['topic_id']}")
    print(f"Admins: {cfg['telegram']['admins']}")
    print(f"Require Arm: {cfg['telegram']['require_arm']}")
    print(f"Arm TTL: {cfg['telegram']['arm_ttl_minutes']} minutes")
    print(f"Long Instance: {cfg['freqtrade']['long']['base_url']}")
    print(f"Short Instance: {cfg['freqtrade']['short']['base_url']}")
    print(f"Default Stake: {cfg['defaults']['stake']}")
    print(f"Default Delay: {cfg['defaults']['delay_ms']}ms")
    
    # 加载篮子
    basket = load_basket()
    print("\n=== 篮子加载成功 ===")
    print(f"篮子数量: {len(basket)}")
    print(f"篮子内容: {basket}")
    
    # 创建客户端实例
    print("\n=== 客户端测试 ===")
    long_client = FTClient(
        cfg['freqtrade']['long']['base_url'],
        cfg['freqtrade']['long']['user'],
        cfg['freqtrade']['long']['pass']
    )
    short_client = FTClient(
        cfg['freqtrade']['short']['base_url'],
        cfg['freqtrade']['short']['user'],
        cfg['freqtrade']['short']['pass']
    )
    
    print(f"Long 客户端: {long_client.base_url}")
    print(f"Short 客户端: {short_client.base_url}")
    
    print("\n=== 启动 Telegram Bot ===")

# Telegram Bot 功能
print("\n=== 启动 Telegram Bot ===")

# 全局变量
cfg = load_config()
long_client = FTClient(
    cfg['freqtrade']['long']['base_url'],
    cfg['freqtrade']['long']['user'],
    cfg['freqtrade']['long']['pass']
)
short_client = FTClient(
    cfg['freqtrade']['short']['base_url'],
    cfg['freqtrade']['short']['user'],
    cfg['freqtrade']['short']['pass']
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    await update.message.reply_text("🤖 Tiny Orchestrator 已启动！\n使用 /help 查看可用命令。")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    cfg = load_config()
    arm_status = ""
    if cfg['telegram']['require_arm']:
        if is_armed():
            remaining = get_remaining_arm_time()
            if remaining:
                minutes = int(remaining.total_seconds() // 60)
                arm_status = f"\n🔓 **当前状态：已武装** (剩余 {minutes} 分钟)"
            else:
                arm_status = "\n🔒 **当前状态：未武装**"
        else:
            arm_status = "\n🔒 **当前状态：未武装**"
    else:
        arm_status = "\n🔓 **武装机制：已禁用**"
    
    help_text = f"""
🤖 **Tiny Orchestrator 命令列表**

📊 **查看命令：**
• `/basket` - 显示当前篮子与参数
• `/status` - 显示实例状态与最近摘要

⚙️ **篮子管理：**
• `/basket_set <pairs...>` - 设置篮子 (别名: `/bs`)
• `/add <pair>` - 添加交易对 (别名: `/a`)
• `/remove <pair|id>` - 删除交易对 (别名: `/rm`)
• `/clear` - 清空篮子 (别名: `/c`)
• `/stake <amount>` - 设置每笔名义

🚀 **交易命令：**
• `/go_long` - 开多确认卡片
• `/go_short` - 反向开空确认卡片  
• `/flat` - 全平确认卡片

🔐 **安全命令：**
• `/arm <pass>` - 武装系统（如启用）
{arm_status}

---
*仅管理员可在指定 Topic 内使用交易命令*
*交易对格式: BTC/USDT 或 BTC/USDT:USDT*
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def arm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /arm 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查是否为管理员
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("⛔ 无权限：仅管理员可以武装系统")
        return
    
    # 检查是否启用武装机制
    cfg = load_config()
    if not cfg['telegram']['require_arm']:
        await update.message.reply_text("ℹ️ 武装机制已禁用，无需武装即可执行交易命令")
        return
    
    # 检查参数
    if not context.args:
        remaining = get_remaining_arm_time()
        if remaining:
            minutes = int(remaining.total_seconds() // 60)
            await update.message.reply_text(f"🔓 系统已武装，剩余时间：{minutes} 分钟")
        else:
            await update.message.reply_text("🔒 系统未武装\n使用：`/arm <密码>` 来武装系统", parse_mode='Markdown')
        return
    
    # 简单的密码验证（这里可以根据需要增强）
    password = " ".join(context.args)
    if password == "confirm":  # 简单的固定密码，实际使用时可以配置
        ttl = arm_system()
        minutes = int(ttl.total_seconds() // 60)
        await update.message.reply_text(f"✅ 系统已武装 {minutes} 分钟\n可以执行交易命令")
    else:
        await update.message.reply_text("❌ 密码错误")

async def basket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /basket 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    try:
        # 加载配置和篮子
        cfg = load_config()
        basket = load_basket()
        
        # 构建响应消息
        current_time = datetime.now().strftime("%H:%M:%S")
        message = f"📊 **当前篮子配置** (更新时间: {current_time})\n\n"
        
        # 篮子内容
        if basket:
            message += f"🛒 **篮子内容** ({len(basket)} 个交易对):\n"
            for i, pair in enumerate(basket, 1):
                message += f"  {i}. {pair}\n"
        else:
            message += "🛒 **篮子内容**: 空\n"
        
        # 交易参数
        message += "\n⚙️ **交易参数**:\n"
        message += f"  • 每笔名义: `{cfg['defaults']['stake']}` USDT\n"
        message += f"  • 延迟时间: `{cfg['defaults']['delay_ms']}` ms\n"
        message += f"  • 轮询超时: `{cfg['defaults']['poll_timeout_sec']}` 秒\n"
        message += f"  • 轮询间隔: `{cfg['defaults']['poll_interval_sec']}` 秒\n"
        
        # 创建内联键盘
        keyboard = [
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh_basket")],
            [
                InlineKeyboardButton("🚀 开多", callback_data="QUICK_GO_LONG"),
                InlineKeyboardButton("🔴 开空", callback_data="QUICK_GO_SHORT"),
                InlineKeyboardButton("🚫 全平", callback_data="QUICK_FLAT")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        # 解析错误类型并提供友好的错误信息
        error_str = str(e).lower()
        if "permission" in error_str or "access" in error_str:
            await update.message.reply_text("❌ 获取篮子信息失败: 🔐 文件权限不足")
        elif "not found" in error_str or "no such file" in error_str:
            await update.message.reply_text("❌ 获取篮子信息失败: 📁 配置文件不存在")
        elif "yaml" in error_str or "format" in error_str:
            await update.message.reply_text("❌ 获取篮子信息失败: 📝 配置文件格式错误")
        else:
            await update.message.reply_text(f"❌ 获取篮子信息失败: {str(e)[:50]}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /status 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    try:
        # 加载配置
        cfg = load_config()
        
        # 创建客户端
        long_client = FTClient(
            cfg['freqtrade']['long']['base_url'],
            cfg['freqtrade']['long']['user'],
            cfg['freqtrade']['long']['pass']
        )
        short_client = FTClient(
            cfg['freqtrade']['short']['base_url'],
            cfg['freqtrade']['short']['user'],
            cfg['freqtrade']['short']['pass']
        )
        
        # 构建状态消息
        current_time = datetime.now().strftime("%H:%M:%S")
        message = f"📈 **实例状态摘要** (更新时间: {current_time})\n\n"
        
        # 获取多仓实例状态
        try:
            long_positions = long_client.list_positions()
            long_count = len(long_positions) if long_positions else 0
            long_status = "🟢 在线" if long_positions is not None else "🔴 离线"
            
            message += f"🔵 **多仓实例** (`{cfg['freqtrade']['long']['base_url']}`)\n"
            message += f"  • 状态: {long_status}\n"
            message += f"  • 持仓数量: {long_count}\n"
            
            if long_positions and long_count > 0:
                message += "  • 持仓详情:\n"
                for trade in long_positions[:5]:  # 最多显示5个
                    if isinstance(trade, dict):
                        pair = trade.get('pair', 'Unknown')
                        amount = trade.get('amount', 0)
                        profit_pct = trade.get('profit_pct', 0)
                        profit_sign = "+" if profit_pct >= 0 else ""
                        message += f"    - `{pair}`: {amount:.4f} ({profit_sign}{profit_pct:.2f}%)\n"
                if long_count > 5:
                    message += f"    ... 还有 {long_count - 5} 个持仓\n"
            
        except Exception as e:
            # 解析连接错误类型
            error_str = str(e).lower()
            if "timeout" in error_str or "timed out" in error_str:
                message += "🔵 **多仓实例**: ⏰ 连接超时\n"
            elif "connection" in error_str or "connect" in error_str:
                message += "🔵 **多仓实例**: 🔴 连接失败 (网络问题)\n"
            elif "forbidden" in error_str or "401" in error_str:
                message += "🔵 **多仓实例**: 🔐 认证失败 (检查用户名密码)\n"
            elif "not found" in error_str or "404" in error_str:
                message += "🔵 **多仓实例**: 🚫 服务未找到 (检查URL路径)\n"
            else:
                message += f"🔵 **多仓实例**: 🔴 连接失败 ({str(e)[:30]}...)\n"
        
        # 获取空仓实例状态
        try:
            short_positions = short_client.list_positions()
            short_count = len(short_positions) if short_positions else 0
            short_status = "🟢 在线" if short_positions is not None else "🔴 离线"
            
            message += f"\n🔴 **空仓实例** (`{cfg['freqtrade']['short']['base_url']}`)\n"
            message += f"  • 状态: {short_status}\n"
            message += f"  • 持仓数量: {short_count}\n"
            
            if short_positions and short_count > 0:
                message += "  • 持仓详情:\n"
                for trade in short_positions[:5]:  # 最多显示5个
                    if isinstance(trade, dict):
                        pair = trade.get('pair', 'Unknown')
                        amount = trade.get('amount', 0)
                        profit_pct = trade.get('profit_pct', 0)
                        profit_sign = "+" if profit_pct >= 0 else ""
                        message += f"    - `{pair}`: {amount:.4f} ({profit_sign}{profit_pct:.2f}%)\n"
                if short_count > 5:
                    message += f"    ... 还有 {short_count - 5} 个持仓\n"
            
        except Exception as e:
            # 解析连接错误类型
            error_str = str(e).lower()
            if "timeout" in error_str or "timed out" in error_str:
                message += "\n🔴 **空仓实例**: ⏰ 连接超时\n"
            elif "connection" in error_str or "connect" in error_str:
                message += "\n🔴 **空仓实例**: 🔴 连接失败 (网络问题)\n"
            elif "forbidden" in error_str or "401" in error_str:
                message += "\n🔴 **空仓实例**: 🔐 认证失败 (检查用户名密码)\n"
            elif "not found" in error_str or "404" in error_str:
                message += "\n🔴 **空仓实例**: 🚫 服务未找到 (检查URL路径)\n"
            else:
                message += f"\n🔴 **空仓实例**: 🔴 连接失败 ({str(e)[:30]}...)\n"
        
        # 总结
        try:
            total_positions = long_count + short_count
            message += f"\n📊 **总计**: {total_positions} 个活跃持仓"
        except Exception:
            message += "\n📊 **总计**: 无法统计"
        
        # 创建内联键盘
        keyboard = [
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh_status")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        # 解析错误类型并提供友好的错误信息
        error_str = str(e).lower()
        if "permission" in error_str or "access" in error_str:
            await update.message.reply_text("❌ 获取状态信息失败: 🔐 文件权限不足")
        elif "not found" in error_str or "no such file" in error_str:
            await update.message.reply_text("❌ 获取状态信息失败: 📁 配置文件不存在")
        elif "timeout" in error_str or "timed out" in error_str:
            await update.message.reply_text("❌ 获取状态信息失败: ⏰ 连接超时")
        else:
            await update.message.reply_text(f"❌ 获取状态信息失败: {str(e)[:50]}")

async def basket_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /basket_set 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    # 检查参数
    if not context.args:
        await update.message.reply_text("❌ 用法: `/basket_set <pair1> <pair2> ...`\n例如: `/basket_set BTC/USDT ETH/USDT`", parse_mode='Markdown')
        return
    
    try:
        # 解析和验证交易对
        raw_pairs = context.args
        validated_pairs = []
        invalid_pairs = []
        
        for pair in raw_pairs:
            # 转换为大写
            pair_upper = pair.upper()
            
            # 格式校验：BASE/QUOTE 或 BASE/QUOTE:SETTLE（期货）
            if re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_upper):
                # 现货格式，自动转换为期货格式
                validated_pairs.append(f"{pair_upper}:USDT")
            elif re.match(r'^[A-Z0-9]+/[A-Z0-9]+:[A-Z0-9]+$', pair_upper):
                # 期货格式，直接使用
                validated_pairs.append(pair_upper)
            else:
                invalid_pairs.append(pair)
        
        # 去重
        validated_pairs = list(dict.fromkeys(validated_pairs))  # 保持顺序的去重
        
        if invalid_pairs:
            await update.message.reply_text(f"❌ 无效的交易对格式: {', '.join(invalid_pairs)}\n正确格式: BASE/QUOTE (如 BTC/USDT)")
            return
        
        if not validated_pairs:
            await update.message.reply_text("❌ 没有有效的交易对")
            return
        
        # 更新 watchlist.yml 文件
        watchlist_data = {
            'basket': validated_pairs
        }
        
        with open('watchlist.yml', 'w', encoding='utf-8') as f:
            yaml.dump(watchlist_data, f, default_flow_style=False, allow_unicode=True)
        
        # 构建成功消息
        message = "✅ **篮子已更新**\n\n"
        message += f"🛒 **新篮子内容** ({len(validated_pairs)} 个交易对):\n"
        for i, pair in enumerate(validated_pairs, 1):
            message += f"  {i}. `{pair}`\n"
        
        if len(raw_pairs) != len(validated_pairs):
            removed_count = len(raw_pairs) - len(validated_pairs)
            message += f"\n📝 已自动去重和格式化，移除了 {removed_count} 个重复项"
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ 设置篮子失败: {str(e)}")

async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /stake 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    # 检查参数
    if not context.args:
        # 显示当前 stake
        cfg = load_config()
        current_stake = cfg['defaults']['stake']
        await update.message.reply_text(f"💰 **当前每笔名义**: `{current_stake}` USDT\n\n用法: `/stake <amount>`\n例如: `/stake 500`", parse_mode='Markdown')
        return
    
    try:
        # 解析金额
        amount_str = context.args[0]
        
        try:
            amount = float(amount_str)
        except ValueError:
            await update.message.reply_text(f"❌ 无效的金额格式: `{amount_str}`\n请输入数字，例如: `/stake 500`", parse_mode='Markdown')
            return
        
        # 验证金额范围
        if amount <= 0:
            await update.message.reply_text("❌ 金额必须大于 0")
            return
        
        if amount > 10000:  # 设置一个合理的上限
            await update.message.reply_text("❌ 金额过大，最大允许 10000 USDT")
            return
        
        # 读取当前配置
        cfg = load_config()
        old_stake = cfg['defaults']['stake']
        
        # 更新配置
        cfg['defaults']['stake'] = amount
        
        # 写回配置文件
        with open('config.yml', 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        
        # 构建成功消息
        message = "✅ **每笔名义已更新**\n\n"
        message += f"💰 **旧值**: `{old_stake}` USDT\n"
        message += f"💰 **新值**: `{amount}` USDT\n"
        
        # 如果金额变化很大，给出提醒
        if amount > old_stake * 2:
            message += f"\n⚠️ **提醒**: 新金额是原来的 {amount/old_stake:.1f} 倍，请确认"
        elif amount < old_stake * 0.5:
            message += f"\n⚠️ **提醒**: 新金额是原来的 {amount/old_stake:.1f} 倍，请确认"
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ 设置每笔名义失败: {str(e)}")

# 全局变量用于幂等控制
executed_operations = set()  # 记录已执行的操作ID

async def safe_edit_message(query, message: str, parse_mode='Markdown'):
    """安全的消息编辑函数，处理 Markdown 解析错误"""
    try:
        # 清理可能导致 Markdown 解析问题的字符
        safe_message = message.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
        safe_message = safe_message.replace('(', '\\(').replace(')', '\\)').replace('~', '\\~')
        safe_message = safe_message.replace('`', '\\`').replace('>', '\\>').replace('#', '\\#')
        safe_message = safe_message.replace('+', '\\+').replace('-', '\\-').replace('=', '\\=')
        safe_message = safe_message.replace('|', '\\|').replace('{', '\\{').replace('}', '\\}')
        safe_message = safe_message.replace('.', '\\.').replace('!', '\\!')
        
        await query.edit_message_text(safe_message, parse_mode=parse_mode)
    except Exception as e:
        if "can't parse entities" in str(e) or "can't find end of the entity" in str(e):
            # 如果 Markdown 解析失败，使用纯文本
            try:
                # 移除所有 Markdown 标记
                plain_message = message.replace('**', '').replace('*', '').replace('`', '')
                await query.edit_message_text(plain_message, parse_mode=None)
            except Exception:
                # 最后的备选方案：只显示简单消息
                await query.edit_message_text("操作完成", parse_mode=None)
        else:
            raise e

async def go_long_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /go_long 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    try:
        # 加载配置和篮子
        basket = load_basket()
        
        if not basket:
            await update.message.reply_text("❌ 篮子为空，无法执行开多操作")
            return
        
        # 生成操作ID（时间戳+随机数）
        import random
        op_id = f"long_{int(time.time())}_{random.randint(1000, 9999)}"
        
        # 构建确认消息
        message = f"🚀 **开多确认** (ID: {op_id})\n\n"
        message += "📊 **操作详情**:\n"
        message += f"  • 交易对数量: {len(basket)} 个\n"
        message += f"  • 每笔名义: {cfg['defaults']['stake']} USDT\n"
        message += f"  • 延迟间隔: {cfg['defaults']['delay_ms']} ms\n"
        message += f"  • 总金额: {len(basket) * cfg['defaults']['stake']} USDT\n\n"
        
        message += "🛒 **交易对列表**:\n"
        for i, pair in enumerate(basket, 1):
            message += f"  {i}. {pair}\n"
        
        message += "\n⚠️ **确认后将执行开多操作**"
        
        # 创建内联键盘
        keyboard = [
            [
                InlineKeyboardButton("✅ 确认开多", callback_data=f"CONFIRM|GO_LONG|{op_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"CANCEL|GO_LONG|{op_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 创建开多确认失败: {str(e)}")

async def flat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /flat 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    try:
        # 生成操作ID
        import random
        op_id = f"flat_{int(time.time())}_{random.randint(1000, 9999)}"
        
        # 构建确认消息
        message = f"🚫 **全平确认** (ID: {op_id})\n\n"
        message += "📊 **操作详情**:\n"
        message += "  • 取消所有开放订单\n"
        message += "  • 平掉所有多仓持仓\n"
        message += "  • 平掉所有空仓持仓\n\n"
        message += "⚠️ **警告: 此操作将清空所有持仓**\n"
        message += "⚠️ **确认后将执行全平操作**"
        
        # 创建内联键盘
        keyboard = [
            [
                InlineKeyboardButton("✅ 确认全平", callback_data=f"CONFIRM|FLAT|{op_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"CANCEL|FLAT|{op_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 创建全平确认失败: {str(e)}")

async def go_short_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /go_short 命令"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    try:
        # 加载配置和篮子
        cfg = load_config()
        basket = load_basket()
        
        if not basket:
            await update.message.reply_text("❌ 篮子为空，无法执行开空操作")
            return
        
        # 生成操作ID
        import random
        op_id = f"short_{int(time.time())}_{random.randint(1000, 9999)}"
        
        # 构建确认消息
        message = f"🔴 **开空确认** (ID: {op_id})\n\n"
        message += "📊 **操作详情**:\n"
        message += "  • 第一步: 发送平仓信号给多仓账户\n"
        message += "  • 第二步: 逐个开空仓\n"
        message += f"  • 交易对数量: {len(basket)} 个\n"
        message += f"  • 每笔名义: {cfg['defaults']['stake']} USDT\n"
        message += f"  • 延迟间隔: {cfg['defaults']['delay_ms']} ms\n"
        message += f"  • 轮询超时: {cfg['defaults']['poll_timeout_sec']} 秒\n"
        message += f"  • 总金额: {len(basket) * cfg['defaults']['stake']} USDT\n\n"
        
        message += "🛒 **交易对列表**:\n"
        for i, pair in enumerate(basket, 1):
            message += f"  {i}. {pair}\n"
        
        message += "\n⚠️ **确认后将执行反向操作（先平多后开空）**"
        
        # 创建内联键盘
        keyboard = [
            [
                InlineKeyboardButton("✅ 确认开空", callback_data=f"CONFIRM|GO_SHORT|{op_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"CANCEL|GO_SHORT|{op_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 创建开空确认失败: {str(e)}")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /add 命令 - 添加单个交易对到篮子"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    try:
        # 获取命令参数
        if not context.args:
            await update.message.reply_text(
                "❌ 请提供要添加的交易对\n"
                "用法: `/add BTC/USDT` 或 `/add ETH/USDT:USDT`",
                parse_mode='Markdown'
            )
            return
        
        pair_input = context.args[0].upper()
        
        # 验证交易对格式
        if not (re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_input) or 
                re.match(r'^[A-Z0-9]+/[A-Z0-9]+:[A-Z0-9]+$', pair_input)):
            await update.message.reply_text(
                "❌ 无效的交易对格式\n"
                "正确格式: `BTC/USDT` 或 `BTC/USDT:USDT`",
                parse_mode='Markdown'
            )
            return
        
        # 转换为标准格式
        if re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_input):
            pair_standard = f"{pair_input}:USDT"
        else:
            pair_standard = pair_input
        
        # 加载当前篮子
        basket = load_basket()
        
        # 检查是否已存在
        if pair_standard in basket:
            await update.message.reply_text(f"⚠️ 交易对 `{pair_standard}` 已存在于篮子中", parse_mode='Markdown')
            return
        
        # 添加到篮子
        basket.append(pair_standard)
        
        # 保存篮子
        if save_basket(basket):
            await update.message.reply_text(
                f"✅ 成功添加交易对 `{pair_standard}` 到篮子\n"
                f"📊 当前篮子包含 {len(basket)} 个交易对",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ 保存篮子失败")
            
    except Exception as e:
        await update.message.reply_text(f"❌ 添加交易对失败: {str(e)}")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /remove 命令 - 从篮子中删除单个交易对"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    try:
        # 获取命令参数
        if not context.args:
            await update.message.reply_text(
                "❌ 请提供要删除的交易对\n"
                "用法: `/remove BTC/USDT` 或 `/remove 1` (通过ID删除)",
                parse_mode='Markdown'
            )
            return
        
        input_arg = context.args[0]
        
        # 加载当前篮子
        basket = load_basket()
        
        # 检查输入是否为数字（ID）
        if input_arg.isdigit():
            try:
                pair_id = int(input_arg)
                if 1 <= pair_id <= len(basket):
                    # 通过ID删除
                    pair_to_remove = basket[pair_id - 1]
                    basket.pop(pair_id - 1)
                else:
                    await update.message.reply_text(
                        f"⚠️ ID `{pair_id}` 超出范围 (1-{len(basket)})",
                        parse_mode='Markdown'
                    )
                    return
            except ValueError:
                await update.message.reply_text("❌ 无效的ID格式", parse_mode='Markdown')
                return
        else:
            # 通过交易对名称删除
            pair_input = input_arg.upper()
            
            # 转换为标准格式
            if re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_input):
                pair_standard = f"{pair_input}:USDT"
            else:
                pair_standard = pair_input
            
            # 检查是否存在
            if pair_standard not in basket:
                await update.message.reply_text(f"⚠️ 交易对 `{pair_standard}` 不存在于篮子中", parse_mode='Markdown')
                return
            
            # 从篮子中删除
            pair_to_remove = pair_standard
            basket.remove(pair_standard)
        
        # 保存篮子
        if save_basket(basket):
            await update.message.reply_text(
                f"✅ 成功从篮子中删除交易对 `{pair_to_remove}`\n"
                f"📊 当前篮子包含 {len(basket)} 个交易对",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ 保存篮子失败")
            
    except Exception as e:
        await update.message.reply_text(f"❌ 删除交易对失败: {str(e)}")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /clear 命令 - 清空篮子"""
    # 检查是否在目标群组和 Topic
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    # 检查权限
    has_permission, error_msg = check_permission(update.message.from_user.id)
    if not has_permission:
        await update.message.reply_text(error_msg)
        return
    
    try:
        # 清空篮子
        if save_basket([]):
            await update.message.reply_text("✅ 成功清空篮子")
        else:
            await update.message.reply_text("❌ 清空篮子失败")
            
    except Exception as e:
        await update.message.reply_text(f"❌ 清空篮子失败: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联键盘按钮回调"""
    query = update.callback_query
    await query.answer()  # 立即响应回调
    
    # 检查是否在目标群组和 Topic
    cfg = load_config()  # 重新加载配置
    if query.message.chat.id != cfg['telegram']['chat_id']:
        return
    if query.message.message_thread_id != cfg['telegram']['topic_id']:
        return
    
    try:
        if query.data == "refresh_basket":
            # 刷新篮子信息
            cfg = load_config()
            basket = load_basket()
            
            # 添加时间戳以区分内容
            current_time = datetime.now().strftime("%H:%M:%S")
            message = f"📊 **当前篮子配置** (刷新时间: {current_time})\n\n"
            
            if basket:
                message += f"🛒 **篮子内容** ({len(basket)} 个交易对):\n"
                for i, pair in enumerate(basket, 1):
                    message += f"  {i}. {pair}\n"
            else:
                message += "🛒 **篮子内容**: 空\n"
            
            message += "\n⚙️ **交易参数**:\n"
            message += f"  • 每笔名义: `{cfg['defaults']['stake']}` USDT\n"
            message += f"  • 延迟时间: `{cfg['defaults']['delay_ms']}` ms\n"
            message += f"  • 轮询超时: `{cfg['defaults']['poll_timeout_sec']}` 秒\n"
            message += f"  • 轮询间隔: `{cfg['defaults']['poll_interval_sec']}` 秒\n"
            
            # 构建键盘
            keyboard = [
                [InlineKeyboardButton("🔄 刷新", callback_data="refresh_basket")],
                [
                    InlineKeyboardButton("🚀 开多", callback_data="QUICK_GO_LONG"),
                    InlineKeyboardButton("🔴 开空", callback_data="QUICK_GO_SHORT"),
                    InlineKeyboardButton("🚫 全平", callback_data="QUICK_FLAT")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)
            except Exception as e:
                if "Message is not modified" in str(e):
                    # 如果内容相同，显示一个临时提示
                    await query.answer("✅ 内容已是最新", show_alert=False)
                else:
                    raise e
            
        elif query.data == "refresh_status":
            # 刷新状态信息
            cfg = load_config()
            
            long_client = FTClient(
                cfg['freqtrade']['long']['base_url'],
                cfg['freqtrade']['long']['user'],
                cfg['freqtrade']['long']['pass']
            )
            short_client = FTClient(
                cfg['freqtrade']['short']['base_url'],
                cfg['freqtrade']['short']['user'],
                cfg['freqtrade']['short']['pass']
            )
            
            # 添加时间戳以区分内容
            current_time = datetime.now().strftime("%H:%M:%S")
            message = f"📈 **实例状态摘要** (刷新时间: {current_time})\n\n"
            
            # 获取多仓实例状态
            try:
                long_positions = long_client.list_positions()
                long_count = len(long_positions) if long_positions else 0
                long_status = "🟢 在线" if long_positions is not None else "🔴 离线"
                
                message += f"🔵 **多仓实例** (`{cfg['freqtrade']['long']['base_url']}`)\n"
                message += f"  • 状态: {long_status}\n"
                message += f"  • 持仓数量: {long_count}\n"
                
                if long_positions and long_count > 0:
                    message += "  • 持仓详情:\n"
                    for trade in long_positions[:5]:
                        if isinstance(trade, dict):
                            pair = trade.get('pair', 'Unknown')
                            amount = trade.get('amount', 0)
                            profit_pct = trade.get('profit_pct', 0)
                            profit_sign = "+" if profit_pct >= 0 else ""
                            message += f"    - `{pair}`: {amount:.4f} ({profit_sign}{profit_pct:.2f}%)\n"
                    if long_count > 5:
                        message += f"    ... 还有 {long_count - 5} 个持仓\n"
                
            except Exception as e:
                # 解析连接错误类型
                error_str = str(e).lower()
                if "timeout" in error_str or "timed out" in error_str:
                    message += "🔵 **多仓实例**: ⏰ 连接超时\n"
                elif "connection" in error_str or "connect" in error_str:
                    message += "🔵 **多仓实例**: 🔴 连接失败 (网络问题)\n"
                elif "forbidden" in error_str or "401" in error_str:
                    message += "🔵 **多仓实例**: 🔐 认证失败 (检查用户名密码)\n"
                elif "not found" in error_str or "404" in error_str:
                    message += "🔵 **多仓实例**: 🚫 服务未找到 (检查URL路径)\n"
                else:
                    message += f"🔵 **多仓实例**: 🔴 连接失败 ({str(e)[:30]}...)\n"
            
            # 获取空仓实例状态
            try:
                short_positions = short_client.list_positions()
                short_count = len(short_positions) if short_positions else 0
                short_status = "🟢 在线" if short_positions is not None else "🔴 离线"
                
                message += f"\n🔴 **空仓实例** (`{cfg['freqtrade']['short']['base_url']}`)\n"
                message += f"  • 状态: {short_status}\n"
                message += f"  • 持仓数量: {short_count}\n"
                
                if short_positions and short_count > 0:
                    message += "  • 持仓详情:\n"
                    for trade in short_positions[:5]:
                        if isinstance(trade, dict):
                            pair = trade.get('pair', 'Unknown')
                            amount = trade.get('amount', 0)
                            profit_pct = trade.get('profit_pct', 0)
                            profit_sign = "+" if profit_pct >= 0 else ""
                            message += f"    - `{pair}`: {amount:.4f} ({profit_sign}{profit_pct:.2f}%)\n"
                    if short_count > 5:
                        message += f"    ... 还有 {short_count - 5} 个持仓\n"
                
            except Exception as e:
                # 解析连接错误类型
                error_str = str(e).lower()
                if "timeout" in error_str or "timed out" in error_str:
                    message += "\n🔴 **空仓实例**: ⏰ 连接超时\n"
                elif "connection" in error_str or "connect" in error_str:
                    message += "\n🔴 **空仓实例**: 🔴 连接失败 (网络问题)\n"
                elif "forbidden" in error_str or "401" in error_str:
                    message += "\n🔴 **空仓实例**: 🔐 认证失败 (检查用户名密码)\n"
                elif "not found" in error_str or "404" in error_str:
                    message += "\n🔴 **空仓实例**: 🚫 服务未找到 (检查URL路径)\n"
                else:
                    message += f"\n🔴 **空仓实例**: 🔴 连接失败 ({str(e)[:30]}...)\n"
            
            # 总结
            try:
                total_positions = long_count + short_count
                message += f"\n📊 **总计**: {total_positions} 个活跃持仓"
            except Exception:
                message += "\n📊 **总计**: 无法统计"
            
            keyboard = [
                [InlineKeyboardButton("🔄 刷新", callback_data="refresh_status")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)
            except Exception as e:
                if "Message is not modified" in str(e):
                    # 如果内容相同，显示一个临时提示
                    await query.answer("✅ 内容已是最新", show_alert=False)
                else:
                    raise e
            
        # 处理快速操作回调
        elif query.data.startswith("QUICK_"):
            
            # 检查权限
            has_permission, error_msg = check_permission(query.from_user.id)
            if not has_permission:
                await query.answer(error_msg, show_alert=True)
                return
            
            # 检查篮子是否为空
            basket = load_basket()
            if not basket:
                await query.answer("❌ 篮子为空，无法执行操作", show_alert=True)
                return
            
            if query.data == "QUICK_GO_LONG":
                # 快速开多 - 直接调用原有的命令函数
                await query.answer("🚀 正在创建开多确认...", show_alert=False)
                try:
                    # 创建一个模拟的 Update 对象来调用原有函数
                    fake_message = type('FakeMessage', (), {
                        'chat': query.message.chat,
                        'message_thread_id': query.message.message_thread_id,
                        'from_user': query.from_user,
                        'reply_text': query.message.reply_text  # 添加 reply_text 方法
                    })()
                    fake_update = type('FakeUpdate', (), {
                        'message': fake_message
                    })()
                    await go_long_command(fake_update, context)
                except Exception as e:
                    await query.answer(f"❌ 创建确认失败: {str(e)}", show_alert=True)
            elif query.data == "QUICK_GO_SHORT":
                # 快速开空 - 直接调用原有的命令函数
                await query.answer("🔴 正在创建开空确认...", show_alert=False)
                try:
                    fake_message = type('FakeMessage', (), {
                        'chat': query.message.chat,
                        'message_thread_id': query.message.message_thread_id,
                        'from_user': query.from_user,
                        'reply_text': query.message.reply_text  # 添加 reply_text 方法
                    })()
                    fake_update = type('FakeUpdate', (), {
                        'message': fake_message
                    })()
                    await go_short_command(fake_update, context)
                except Exception as e:
                    await query.answer(f"❌ 创建确认失败: {str(e)}", show_alert=True)
            elif query.data == "QUICK_FLAT":
                # 快速全平 - 直接调用原有的命令函数
                await query.answer("🚫 正在创建全平确认...", show_alert=False)
                try:
                    fake_message = type('FakeMessage', (), {
                        'chat': query.message.chat,
                        'message_thread_id': query.message.message_thread_id,
                        'from_user': query.from_user,
                        'reply_text': query.message.reply_text  # 添加 reply_text 方法
                    })()
                    fake_update = type('FakeUpdate', (), {
                        'message': fake_message
                    })()
                    await flat_command(fake_update, context)
                except Exception as e:
                    await query.answer(f"❌ 创建确认失败: {str(e)}", show_alert=True)
            else:
                await query.answer("❌ 未知操作", show_alert=True)
        
        elif query.data == "noop":
            # 无操作按钮，只显示提示
            await query.answer("ℹ️ 使用 /add /remove /clear 命令管理篮子", show_alert=False)
        
        # 处理交易命令回调
        elif query.data.startswith("CONFIRM|") or query.data.startswith("CANCEL|"):
            # 解析回调数据
            parts = query.data.split("|")
            if len(parts) != 3:
                await query.answer("❌ 无效的回调数据", show_alert=True)
                return
            
            action, operation, op_id = parts
            
            # 检查权限
            has_permission, error_msg = check_permission(query.from_user.id)
            if not has_permission:
                await query.answer(error_msg, show_alert=True)
                return
            
            # 检查幂等性
            if action == "CONFIRM" and op_id in executed_operations:
                await query.answer("⚠️ 此操作已执行，请勿重复点击", show_alert=True)
                return
            
            if action == "CANCEL":
                await query.answer("❌ 操作已取消", show_alert=False)
                await query.edit_message_text("❌ **操作已取消**", parse_mode='Markdown')
                return
            
            # 执行确认操作
            if action == "CONFIRM":
                if operation == "GO_LONG":
                    # 记录操作ID，防止重复执行
                    executed_operations.add(op_id)
                    
                    # 开始执行开多操作
                    await execute_go_long(query, op_id)
                elif operation == "FLAT":
                    # 记录操作ID，防止重复执行
                    executed_operations.add(op_id)
                    
                    # 开始执行全平操作
                    await execute_flat(query, op_id)
                elif operation == "GO_SHORT":
                    # 记录操作ID，防止重复执行
                    executed_operations.add(op_id)
                    
                    # 开始执行开空操作
                    await execute_go_short(query, op_id)
            
    except Exception as e:
        await query.edit_message_text(f"❌ 操作失败: {str(e)}")

async def execute_go_long(query, op_id: str):
    """执行开多操作"""
    try:
        # 加载配置和篮子
        cfg = load_config()
        basket = load_basket()
        
        # 创建多仓客户端
        long_client = FTClient(
            cfg['freqtrade']['long']['base_url'],
            cfg['freqtrade']['long']['user'],
            cfg['freqtrade']['long']['pass']
        )
        
        # 更新确认消息为开始状态
        start_message = f"🚀 **开多操作开始** (ID: {op_id})\n\n📊 **执行计划**:\n  • 交易对数量: {len(basket)} 个\n  • 每笔名义: `{cfg['defaults']['stake']}` USDT\n  • 延迟间隔: `{cfg['defaults']['delay_ms']}` ms\n\n⏳ 开始执行..."
        
        await query.edit_message_text(start_message, parse_mode='Markdown')
        
        # 执行开多操作
        results = []
        success_count = 0
        error_count = 0
        
        for i, pair in enumerate(basket, 1):
            try:
                # 执行开多
                result = long_client.forcebuy(pair, cfg['defaults']['stake'])
                
                # 构建当前进度消息
                progress_text = f"🚀 **开多进度** (ID: {op_id})\n\n"
                
                if result is not None:
                    if isinstance(result, dict) and "error" in result:
                        # 处理特定错误类型
                        error_type = result.get("error")
                        error_msg = result.get("message", "未知错误")
                        
                        if error_type == "position_exists":
                            progress_text += f"✅ [{i}/{len(basket)}] `{pair}` → 持仓已存在\n"
                            results.append(f"⚠️ {i}/{len(basket)} {pair} - 持仓已存在")
                            success_count += 1  # 持仓已存在也算成功
                        elif error_type == "symbol_not_found":
                            progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → 交易对不存在\n"
                            results.append(f"❌ {i}/{len(basket)} {pair} - 交易对不存在")
                            error_count += 1
                        elif error_type == "timeout":
                            progress_text += f"⏰ [{i}/{len(basket)}] `{pair}` → 请求超时\n"
                            results.append(f"⏰ {i}/{len(basket)} {pair} - 请求超时")
                            error_count += 1
                        elif error_type == "insufficient_balance":
                            progress_text += f"💰 [{i}/{len(basket)}] `{pair}` → 余额不足\n"
                            results.append(f"💰 {i}/{len(basket)} {pair} - 余额不足")
                            error_count += 1
                        elif error_type == "market_closed":
                            progress_text += f"🏪 [{i}/{len(basket)}] `{pair}` → 市场已关闭\n"
                            results.append(f"🏪 {i}/{len(basket)} {pair} - 市场已关闭")
                            error_count += 1
                        elif error_type == "rate_limit":
                            progress_text += f"🚦 [{i}/{len(basket)}] `{pair}` → 请求频率过高\n"
                            results.append(f"🚦 {i}/{len(basket)} {pair} - 请求频率过高")
                            error_count += 1
                        elif error_type == "invalid_pair":
                            progress_text += f"🚫 [{i}/{len(basket)}] `{pair}` → 无效的交易对\n"
                            results.append(f"🚫 {i}/{len(basket)} {pair} - 无效的交易对")
                            error_count += 1
                        elif error_type == "maintenance":
                            progress_text += f"🔧 [{i}/{len(basket)}] `{pair}` → 系统维护中\n"
                            results.append(f"🔧 {i}/{len(basket)} {pair} - 系统维护中")
                            error_count += 1
                        else:
                            progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → {error_msg}\n"
                            results.append(f"❌ {i}/{len(basket)} {pair} - {error_msg}")
                            error_count += 1
                    else:
                        progress_text += f"✅ [{i}/{len(basket)}] `{pair}` → 开多成功\n"
                        results.append(f"✅ {i}/{len(basket)} {pair} - 开多成功")
                        success_count += 1
                else:
                    progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → 开多失败\n"
                    results.append(f"❌ {i}/{len(basket)} {pair} - 开多失败")
                    error_count += 1
                
                # 添加当前统计
                progress_text += f"\n📊 **当前统计**:\n  • 成功: {success_count} 笔\n  • 失败: {error_count} 笔\n  • 进度: {i}/{len(basket)}"
                
                # 更新确认消息显示进度
                try:
                    await query.edit_message_text(progress_text, parse_mode='Markdown')
                except Exception:
                    # 如果编辑失败，尝试不使用Markdown
                    await query.edit_message_text(progress_text, parse_mode=None)
                
                # 延迟
                if i < len(basket):  # 最后一笔不需要延迟
                    await asyncio.sleep(cfg['defaults']['delay_ms'] / 1000)
                    
            except Exception as e:
                # 构建错误的进度消息
                progress_text = f"🚀 **开多进度** (ID: {op_id})\n\n"
                progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → 错误: {str(e)[:30]}\n"
                results.append(f"❌ {i}/{len(basket)} {pair} - 错误: {str(e)[:50]}")
                error_count += 1
                
                # 添加当前统计
                progress_text += f"\n📊 **当前统计**:\n  • 成功: {success_count} 笔\n  • 失败: {error_count} 笔\n  • 进度: {i}/{len(basket)}"
                
                # 更新确认消息显示进度
                try:
                    await query.edit_message_text(progress_text, parse_mode='Markdown')
                except Exception:
                    await query.edit_message_text(progress_text, parse_mode=None)
        
        # 构建汇总消息
        summary_message = f"🎯 **开多操作完成** (ID: {op_id})\n\n"
        summary_message += "📊 **最终汇总**:\n"
        summary_message += f"  • 成功: {success_count} 笔\n"
        summary_message += f"  • 失败: {error_count} 笔\n"
        summary_message += f"  • 总计: {len(basket)} 笔\n\n"
        
        # 显示详细结果（最多显示前5个）
        summary_message += "📋 **详细结果**:\n"
        for result in results[:5]:
            summary_message += f"  {result}\n"
        
        if len(results) > 5:
            summary_message += f"  ... 还有 {len(results) - 5} 笔\n"
        
        # 添加时间戳
        current_time = datetime.now().strftime("%H:%M:%S")
        summary_message += f"\n⏰ 完成时间: {current_time}"
        
        # 更新确认消息为最终汇总
        try:
            await query.edit_message_text(summary_message, parse_mode='Markdown')
        except Exception as e:
            if "can't parse entities" in str(e):
                # 如果 Markdown 解析失败，尝试不使用 Markdown
                await query.edit_message_text(summary_message, parse_mode=None)
            else:
                await query.edit_message_text(f"❌ 更新汇总失败: {str(e)}")
        
        # 写入审计日志
        audit_log = f"[{datetime.now().isoformat()}] GO_LONG {op_id} - Success: {success_count}, Failed: {error_count}, Total: {len(basket)}\n"
        try:
            os.makedirs('runtime', exist_ok=True)
            with open('runtime/audit.log', 'a', encoding='utf-8') as f:
                f.write(audit_log)
        except Exception as e:
            print(f"写入审计日志失败: {e}")
        
    except Exception as e:
        await safe_edit_message(query, f"❌ **开多执行失败** (ID: {op_id})\n\n错误: {str(e)}")

async def execute_flat(query, op_id: str):
    """执行全平操作"""
    try:
        # 加载配置
        cfg = load_config()
        
        # 创建客户端
        long_client = FTClient(
            cfg['freqtrade']['long']['base_url'],
            cfg['freqtrade']['long']['user'],
            cfg['freqtrade']['long']['pass']
        )
        short_client = FTClient(
            cfg['freqtrade']['short']['base_url'],
            cfg['freqtrade']['short']['user'],
            cfg['freqtrade']['short']['pass']
        )
        
        # 更新确认消息为开始状态
        start_message = f"🚫 **全平操作开始** (ID: {op_id})\n\n📊 **执行计划**:\n  • 取消所有开放订单\n  • 平掉所有多仓持仓\n  • 平掉所有空仓持仓\n\n⏳ 开始执行..."
        
        await query.edit_message_text(start_message, parse_mode='Markdown')
        
        # 执行全平操作
        results = []
        total_success = 0
        total_error = 0
        
        # 1. 取消所有开放订单
        step1_message = f"🚫 **全平进度** (ID: {op_id})\n\n📋 **第一步：取消开放订单**\n⏳ 正在处理..."
        await query.edit_message_text(step1_message, parse_mode='Markdown')
        
        try:
            long_client.cancel_open_orders()
            short_client.cancel_open_orders()
            results.append("✅ 取消开放订单完成")
        except Exception as e:
            # 检查是否是无开放订单的错误
            if "No open order" in str(e) or "no_open_order" in str(e):
                results.append("ℹ️ 无开放订单需要取消")
            else:
                results.append(f"❌ 取消开放订单失败: {str(e)[:50]}")
        
        # 2. 平掉多仓持仓
        step2_message = f"🚫 **全平进度** (ID: {op_id})\n\n📋 **第二步：平掉多仓持仓**\n⏳ 正在处理..."
        await query.edit_message_text(step2_message, parse_mode='Markdown')
        
        try:
            long_positions = long_client.list_positions()
            if long_positions:
                for i, pos in enumerate(long_positions, 1):
                    try:
                        if isinstance(pos, dict) and 'trade_id' in pos:
                            trade_id = pos['trade_id']
                            pair = pos.get('pair', 'Unknown')
                            result = long_client._request("POST", "/api/v1/forceexit", json={"tradeid": trade_id})
                            if result is not None:
                                if isinstance(result, dict) and "error" in result:
                                    error_type = result.get("error")
                                    error_msg = result.get("message", "未知错误")
                                    
                                    if error_type == "no_open_order":
                                        results.append(f"ℹ️ 多仓平仓 {i}: {pair} - 无开放订单")
                                        total_success += 1  # 无订单也算成功
                                    else:
                                        results.append(f"❌ 多仓平仓 {i}: {pair} - {error_msg}")
                                        total_error += 1
                                else:
                                    results.append(f"✅ 多仓平仓 {i}: {pair}")
                                    total_success += 1
                            else:
                                results.append(f"❌ 多仓平仓 {i}: {pair} - 失败")
                                total_error += 1
                            
                            # 延迟
                            if i < len(long_positions):
                                await asyncio.sleep(cfg['defaults']['delay_ms'] / 1000)
                    except Exception as e:
                        results.append(f"❌ 多仓平仓 {i}: 错误 - {str(e)[:50]}")
                        total_error += 1
            else:
                results.append("ℹ️ 无多仓持仓")
        except Exception as e:
            results.append(f"❌ 获取多仓持仓失败: {str(e)[:50]}")
        
        # 3. 平掉空仓持仓
        step3_message = f"🚫 **全平进度** (ID: {op_id})\n\n📋 **第三步：平掉空仓持仓**\n⏳ 正在处理..."
        await query.edit_message_text(step3_message, parse_mode='Markdown')
        
        try:
            short_positions = short_client.list_positions()
            if short_positions:
                for i, pos in enumerate(short_positions, 1):
                    try:
                        if isinstance(pos, dict) and 'trade_id' in pos:
                            trade_id = pos['trade_id']
                            pair = pos.get('pair', 'Unknown')
                            result = short_client._request("POST", "/api/v1/forceexit", json={"tradeid": trade_id})
                            if result is not None:
                                if isinstance(result, dict) and "error" in result:
                                    error_type = result.get("error")
                                    error_msg = result.get("message", "未知错误")
                                    
                                    if error_type == "no_open_order":
                                        results.append(f"ℹ️ 空仓平仓 {i}: {pair} - 无开放订单")
                                        total_success += 1  # 无订单也算成功
                                    else:
                                        results.append(f"❌ 空仓平仓 {i}: {pair} - {error_msg}")
                                        total_error += 1
                                else:
                                    results.append(f"✅ 空仓平仓 {i}: {pair}")
                                    total_success += 1
                            else:
                                results.append(f"❌ 空仓平仓 {i}: {pair} - 失败")
                                total_error += 1
                            
                            # 延迟
                            if i < len(short_positions):
                                await asyncio.sleep(cfg['defaults']['delay_ms'] / 1000)
                    except Exception as e:
                        results.append(f"❌ 空仓平仓 {i}: 错误 - {str(e)[:50]}")
                        total_error += 1
            else:
                results.append("ℹ️ 无空仓持仓")
        except Exception as e:
            results.append(f"❌ 获取空仓持仓失败: {str(e)[:50]}")
        
        # 构建汇总消息
        summary_message = f"🎯 **全平操作完成** (ID: {op_id})\n\n"
        summary_message += "📊 **最终汇总**:\n"
        summary_message += f"  • 成功: {total_success} 笔\n"
        summary_message += f"  • 失败: {total_error} 笔\n"
        summary_message += f"  • 总计: {total_success + total_error} 笔\n\n"
        
        # 显示详细结果（最多显示前8个）
        summary_message += "📋 **详细结果**:\n"
        for result in results[:8]:
            summary_message += f"  {result}\n"
        
        if len(results) > 8:
            summary_message += f"  ... 还有 {len(results) - 8} 项\n"
        
        # 添加时间戳
        current_time = datetime.now().strftime("%H:%M:%S")
        summary_message += f"\n⏰ 完成时间: {current_time}"
        
        # 更新确认消息为最终汇总
        try:
            await query.edit_message_text(summary_message, parse_mode='Markdown')
        except Exception as e:
            if "can't parse entities" in str(e):
                # 如果 Markdown 解析失败，尝试不使用 Markdown
                await query.edit_message_text(summary_message, parse_mode=None)
            else:
                await query.edit_message_text(f"❌ 更新汇总失败: {str(e)}")
        
        # 写入审计日志
        audit_log = f"[{datetime.now().isoformat()}] FLAT {op_id} - Success: {total_success}, Failed: {total_error}, Total: {total_success + total_error}\n"
        try:
            os.makedirs('runtime', exist_ok=True)
            with open('runtime/audit.log', 'a', encoding='utf-8') as f:
                f.write(audit_log)
        except Exception as e:
            print(f"写入审计日志失败: {e}")
        
    except Exception as e:
        await safe_edit_message(query, f"❌ **全平执行失败** (ID: {op_id})\n\n错误: {str(e)}")

async def execute_go_short(query, op_id: str):
    """执行开空操作（先平多后开空）"""
    try:
        # 加载配置和篮子
        cfg = load_config()
        basket = load_basket()
        
        # 创建客户端
        long_client = FTClient(
            cfg['freqtrade']['long']['base_url'],
            cfg['freqtrade']['long']['user'],
            cfg['freqtrade']['long']['pass']
        )
        short_client = FTClient(
            cfg['freqtrade']['short']['base_url'],
            cfg['freqtrade']['short']['user'],
            cfg['freqtrade']['short']['pass']
        )
        
        # 更新确认消息为开始状态
        start_message = f"🔴 **开空操作开始** (ID: {op_id})\n\n📊 **执行计划**:\n  • 交易对数量: {len(basket)} 个\n  • 每笔名义: `{cfg['defaults']['stake']}` USDT\n  • 延迟间隔: `{cfg['defaults']['delay_ms']}` ms\n\n⏳ 开始执行..."
        
        await query.edit_message_text(start_message, parse_mode='Markdown')
        
        results = []
        total_success = 0
        total_error = 0
        
        # 第一步：发送平仓信号给多仓账户
        step1_message = f"🔴 **开空进度** (ID: {op_id})\n\n📋 **第一步：发送平仓信号**\n⏳ 正在处理..."
        await query.edit_message_text(step1_message, parse_mode='Markdown')
        
        try:
            # 取消开放订单
            long_client.cancel_open_orders()
            results.append("✅ 取消多仓开放订单完成")
            
            # 获取多仓持仓并发送平仓信号
            long_positions = long_client.list_positions()
            if long_positions:
                # 逐个发送平仓信号
                for i, pos in enumerate(long_positions, 1):
                    try:
                        if isinstance(pos, dict) and 'trade_id' in pos:
                            trade_id = pos['trade_id']
                            pair = pos.get('pair', 'Unknown')
                            result = long_client._request("POST", "/api/v1/forceexit", json={"tradeid": trade_id})
                            
                            # 构建当前进度消息
                            progress_text = f"🔴 **开空进度** (ID: {op_id})\n\n📋 **第一步：发送平仓信号**\n"
                            
                            if result is not None:
                                if isinstance(result, dict) and "error" in result:
                                    error_type = result.get("error")
                                    error_msg = result.get("message", "未知错误")
                                    
                                    if error_type == "no_open_order":
                                        progress_text += f"ℹ️ [{i}/{len(long_positions)}] `{pair}` → 无开放订单\n"
                                        results.append(f"ℹ️ 平仓信号 {i}: {pair} - 无开放订单")
                                        total_success += 1  # 无订单也算成功
                                    else:
                                        progress_text += f"❌ [{i}/{len(long_positions)}] `{pair}` → {error_msg}\n"
                                        results.append(f"❌ 平仓信号 {i}: {pair} - {error_msg}")
                                        total_error += 1
                                else:
                                    progress_text += f"✅ [{i}/{len(long_positions)}] `{pair}` → 平仓信号发送成功\n"
                                    results.append(f"✅ 平仓信号 {i}: {pair}")
                                    total_success += 1
                            else:
                                progress_text += f"❌ [{i}/{len(long_positions)}] `{pair}` → 平仓信号发送失败\n"
                                results.append(f"❌ 平仓信号 {i}: {pair} - 失败")
                                total_error += 1
                            
                            # 添加当前统计
                            progress_text += f"\n📊 **当前统计**:\n  • 成功: {total_success} 笔\n  • 失败: {total_error} 笔\n  • 进度: {i}/{len(long_positions)}"
                            
                            # 更新进度消息
                            try:
                                await query.edit_message_text(progress_text, parse_mode='Markdown')
                            except Exception:
                                await query.message.reply_text(progress_text, parse_mode='Markdown')
                            
                            # 延迟
                            if i < len(long_positions):
                                await asyncio.sleep(cfg['defaults']['delay_ms'] / 1000)
                    except Exception as e:
                        # 构建错误的进度消息
                        progress_text = f"🔴 **开空进度** (ID: {op_id})\n\n📋 **第一步：发送平仓信号**\n"
                        progress_text += f"❌ [{i}/{len(long_positions)}] `{pair}` → 错误: {str(e)[:30]}\n"
                        results.append(f"❌ 平仓信号 {i}: 错误 - {str(e)[:50]}")
                        total_error += 1
                        
                        # 添加当前统计
                        progress_text += f"\n📊 **当前统计**:\n  • 成功: {total_success} 笔\n  • 失败: {total_error} 笔\n  • 进度: {i}/{len(long_positions)}"
                        
                        # 更新进度消息
                        try:
                            await query.edit_message_text(progress_text, parse_mode='Markdown')
                        except Exception:
                            await query.message.reply_text(progress_text, parse_mode='Markdown')
                
                results.append("✅ 多仓平仓信号发送完成")
                    
            else:
                results.append("ℹ️ 无多仓持仓")
                
        except Exception as e:
            results.append(f"❌ 发送平仓信号失败: {str(e)[:50]}")
        
        # 第二步：逐个开空仓
        step2_message = f"🔴 **开空进度** (ID: {op_id})\n\n📋 **第二步：开空仓**\n⏳ 正在处理..."
        await query.edit_message_text(step2_message, parse_mode='Markdown')
        
        for i, pair in enumerate(basket, 1):
            try:
                # 执行开空
                result = short_client.forceshort(pair, cfg['defaults']['stake'])
                
                # 构建当前进度消息
                progress_text = f"🔴 **开空进度** (ID: {op_id})\n\n📋 **第二步：开空仓**\n"
                
                if result is not None:
                    if isinstance(result, dict) and "error" in result:
                        # 处理特定错误类型
                        error_type = result.get("error")
                        error_msg = result.get("message", "未知错误")
                        
                        if error_type == "position_exists":
                            progress_text += f"⚠️ [{i}/{len(basket)}] `{pair}` → 持仓已存在\n"
                            results.append(f"⚠️ 开空仓 {i}/{len(basket)}: {pair} - 持仓已存在")
                            total_success += 1  # 持仓已存在也算成功
                        elif error_type == "symbol_not_found":
                            progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → 交易对不存在\n"
                            results.append(f"❌ 开空仓 {i}/{len(basket)}: {pair} - 交易对不存在")
                            total_error += 1
                        elif error_type == "timeout":
                            progress_text += f"⏰ [{i}/{len(basket)}] `{pair}` → 请求超时\n"
                            results.append(f"⏰ 开空仓 {i}/{len(basket)}: {pair} - 请求超时")
                            total_error += 1
                        elif error_type == "insufficient_balance":
                            progress_text += f"💰 [{i}/{len(basket)}] `{pair}` → 余额不足\n"
                            results.append(f"💰 开空仓 {i}/{len(basket)}: {pair} - 余额不足")
                            total_error += 1
                        elif error_type == "market_closed":
                            progress_text += f"🏪 [{i}/{len(basket)}] `{pair}` → 市场已关闭\n"
                            results.append(f"🏪 开空仓 {i}/{len(basket)}: {pair} - 市场已关闭")
                            total_error += 1
                        elif error_type == "rate_limit":
                            progress_text += f"🚦 [{i}/{len(basket)}] `{pair}` → 请求频率过高\n"
                            results.append(f"🚦 开空仓 {i}/{len(basket)}: {pair} - 请求频率过高")
                            total_error += 1
                        elif error_type == "invalid_pair":
                            progress_text += f"🚫 [{i}/{len(basket)}] `{pair}` → 无效的交易对\n"
                            results.append(f"🚫 开空仓 {i}/{len(basket)}: {pair} - 无效的交易对")
                            total_error += 1
                        elif error_type == "maintenance":
                            progress_text += f"🔧 [{i}/{len(basket)}] `{pair}` → 系统维护中\n"
                            results.append(f"🔧 开空仓 {i}/{len(basket)}: {pair} - 系统维护中")
                            total_error += 1
                        else:
                            progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → {error_msg}\n"
                            results.append(f"❌ 开空仓 {i}/{len(basket)}: {pair} - {error_msg}")
                            total_error += 1
                    else:
                        progress_text += f"✅ [{i}/{len(basket)}] `{pair}` → 开空成功\n"
                        results.append(f"✅ 开空仓 {i}/{len(basket)}: {pair}")
                        total_success += 1
                else:
                    progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → 开空失败\n"
                    results.append(f"❌ 开空仓 {i}/{len(basket)}: {pair} - 失败")
                    total_error += 1
                
                # 添加当前统计
                progress_text += f"\n📊 **当前统计**:\n  • 成功: {total_success} 笔\n  • 失败: {total_error} 笔\n  • 进度: {i}/{len(basket)}"
                
                # 更新进度消息
                try:
                    await query.edit_message_text(progress_text, parse_mode='Markdown')
                except Exception:
                    await query.message.reply_text(progress_text, parse_mode='Markdown')
                
                # 延迟
                if i < len(basket):  # 最后一笔不需要延迟
                    await asyncio.sleep(cfg['defaults']['delay_ms'] / 1000)
                    
            except Exception as e:
                # 构建错误的进度消息
                progress_text = f"🔴 **开空进度** (ID: {op_id})\n\n📋 **第二步：开空仓**\n"
                progress_text += f"❌ [{i}/{len(basket)}] `{pair}` → 错误: {str(e)[:30]}\n"
                results.append(f"❌ 开空仓 {i}/{len(basket)}: {pair} - 错误: {str(e)[:50]}")
                total_error += 1
                
                # 添加当前统计
                progress_text += f"\n📊 **当前统计**:\n  • 成功: {total_success} 笔\n  • 失败: {total_error} 笔\n  • 进度: {i}/{len(basket)}"
                
                # 更新确认消息显示进度
                try:
                    await query.edit_message_text(progress_text, parse_mode='Markdown')
                except Exception:
                    await query.edit_message_text(progress_text, parse_mode=None)
        
        # 构建汇总消息
        summary_message = f"🎯 **开空操作完成** (ID: {op_id})\n\n"
        summary_message += "📊 **最终汇总**:\n"
        summary_message += f"  • 成功: {total_success} 笔\n"
        summary_message += f"  • 失败: {total_error} 笔\n"
        summary_message += f"  • 总计: {total_success + total_error} 笔\n\n"
        
        # 显示详细结果（最多显示前8个）
        summary_message += "📋 **详细结果**:\n"
        for result in results[:8]:
            summary_message += f"  {result}\n"
        
        if len(results) > 8:
            summary_message += f"  ... 还有 {len(results) - 8} 项\n"
        
        # 添加时间戳
        current_time = datetime.now().strftime("%H:%M:%S")
        summary_message += f"\n⏰ 完成时间: {current_time}"
        
        # 更新确认消息为最终汇总
        try:
            await query.edit_message_text(summary_message, parse_mode='Markdown')
        except Exception as e:
            if "can't parse entities" in str(e):
                # 如果 Markdown 解析失败，尝试不使用 Markdown
                await query.edit_message_text(summary_message, parse_mode=None)
            else:
                await query.edit_message_text(f"❌ 更新汇总失败: {str(e)}")
        
        # 写入审计日志
        audit_log = f"[{datetime.now().isoformat()}] GO_SHORT {op_id} - Success: {total_success}, Failed: {total_error}, Total: {total_success + total_error}\n"
        try:
            os.makedirs('runtime', exist_ok=True)
            with open('runtime/audit.log', 'a', encoding='utf-8') as f:
                f.write(audit_log)
        except Exception as e:
            print(f"写入审计日志失败: {e}")
        
    except Exception as e:
        await safe_edit_message(query, f"❌ **开空执行失败** (ID: {op_id})\n\n错误: {str(e)}")

# 已删除 quick_* 函数，直接复用原有的命令函数

def check_permission(user_id: int) -> tuple[bool, str]:
    """检查用户权限和武装状态"""
    # 检查管理员权限
    if not is_admin(user_id):
        return False, "⛔ 无权限：仅管理员可以执行交易命令"
    
    # 检查武装状态
    if not is_armed():
        return False, "🔒 系统未武装，请先使用 `/arm <密码>` 武装系统"
    
    return True, ""

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有消息，过滤 chat/topic"""
    # 检查是否在目标群组
    cfg = load_config()
    if update.message.chat.id != cfg['telegram']['chat_id']:
        return  # 忽略非目标群组
    
    # 检查是否在目标 Topic
    if update.message.message_thread_id != cfg['telegram']['topic_id']:
        return  # 忽略非目标 Topic
    
    # 在目标 Topic 内，回复 pong
    await update.message.reply_text("pong")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """错误处理"""
    print(f"Telegram Bot 错误: {context.error}")

def run_telegram_bot():
    """启动 Telegram Bot"""
    # 创建应用
    cfg = load_config()
    application = Application.builder().token(cfg['telegram']['token']).build()
    
    # 添加处理器
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("arm", arm_command))
    application.add_handler(CommandHandler("basket", basket_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("basket_set", basket_set_command))
    application.add_handler(CommandHandler("bs", basket_set_command))
    application.add_handler(CommandHandler("stake", stake_command))
    application.add_handler(CommandHandler("go_long", go_long_command))
    application.add_handler(CommandHandler("flat", flat_command))
    application.add_handler(CommandHandler("go_short", go_short_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("a", add_command))  # 别名
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("rm", remove_command))  # 别名
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("c", clear_command))  # 别名
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # 添加错误处理
    application.add_error_handler(error_handler)
    
    # 启动 Bot
    print("🤖 启动 Telegram Bot...")
    print(f"   目标群组: {cfg['telegram']['chat_id']}")
    print(f"   目标 Topic: {cfg['telegram']['topic_id']}")
    print(f"   管理员: {cfg['telegram']['admins']}")
    
    # 启动自动切换后台任务（解耦至 auto_toggle 模块）
    schedule_auto_toggle(
        application,
        get_config=load_config,
        start_long=lambda: long_client.start_trading(),
        stop_long=lambda: long_client.stop_trading(),
        start_short=lambda: short_client.start_trading(),
        stop_short=lambda: short_client.stop_trading(),
        close_long_positions=lambda: long_client.close_all_positions(),
        close_short_positions=lambda: short_client.close_all_positions(),
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)

# 启动 Bot
run_telegram_bot()
