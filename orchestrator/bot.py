import yaml
import os
import re
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

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
            
            # 格式校验：BASE/QUOTE
            if re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_upper):
                validated_basket.append(pair_upper)
            else:
                print(f"警告：跳过无效格式的交易对 {pair}")
        
        return validated_basket
        
    except Exception as e:
        print(f"错误：加载篮子文件失败 - {e}")
        return []

class FTClient:
    """Freqtrade HTTP 客户端"""
    
    def __init__(self, base_url: str, user: str, passwd: str):
        """初始化客户端"""
        self.base_url = base_url.rstrip('/')
        self.session = httpx.Client(
            auth=(user, passwd),
            timeout=30.0
        )
    
    def _request(self, method: str, path: str, json: Optional[Dict[Any, Any]] = None) -> Optional[Dict[Any, Any]]:
        """通用请求方法"""
        url = f"{self.base_url}{path}"
        
        try:
            response = self.session.request(method, url, json=json)
            
            # 处理 4xx/5xx 错误
            if response.status_code >= 400:
                error_text = response.text[:200] if response.text else f"HTTP {response.status_code}"
                # 只打印 5xx 服务器错误，4xx 客户端错误（如 404）是预期的
                if response.status_code >= 500:
                    print(f"HTTP 错误 {response.status_code}: {error_text}")
                    raise Exception(f"服务器错误 {response.status_code}: {error_text}")
                return None
            
            # 尝试解析 JSON
            try:
                return response.json()
            except Exception:
                return {"text": response.text}
                
        except Exception as e:
            print(f"请求失败 {method} {url}: {e}")
            raise
    
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
        return self._request("POST", "/api/v1/forceenter", json=data)
    
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
    
    def forceshort(self, pair: str, stake: float) -> Optional[Dict[Any, Any]]:
        """强制开空仓"""
        # 使用 /forceenter 端点，side="short" 表示空仓
        data = {
            "pair": pair,
            "side": "short"
        }
        return self._request("POST", "/api/v1/forceenter", json=data)
    
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
    config = load_config()
    
    # 打印关键字段（不打印 token）
    print("=== 配置加载成功 ===")
    print(f"Chat ID: {config['telegram']['chat_id']}")
    print(f"Topic ID: {config['telegram']['topic_id']}")
    print(f"Admins: {config['telegram']['admins']}")
    print(f"Require Arm: {config['telegram']['require_arm']}")
    print(f"Arm TTL: {config['telegram']['arm_ttl_minutes']} minutes")
    print(f"Long Instance: {config['freqtrade']['long']['base_url']}")
    print(f"Short Instance: {config['freqtrade']['short']['base_url']}")
    print(f"Default Stake: {config['defaults']['stake']}")
    print(f"Default Delay: {config['defaults']['delay_ms']}ms")
    
    # 加载篮子
    basket = load_basket()
    print("\n=== 篮子加载成功 ===")
    print(f"篮子数量: {len(basket)}")
    print(f"篮子内容: {basket}")
    
    # 创建客户端实例
    print("\n=== 客户端测试 ===")
    long_client = FTClient(
        config['freqtrade']['long']['base_url'],
        config['freqtrade']['long']['user'],
        config['freqtrade']['long']['pass']
    )
    short_client = FTClient(
        config['freqtrade']['short']['base_url'],
        config['freqtrade']['short']['user'],
        config['freqtrade']['short']['pass']
    )
    
    print(f"Long 客户端: {long_client.base_url}")
    print(f"Short 客户端: {short_client.base_url}")
    
    print("\n=== 启动 Telegram Bot ===")

# Telegram Bot 功能
print("\n=== 启动 Telegram Bot ===")

# 全局变量
config = load_config()
long_client = FTClient(
    config['freqtrade']['long']['base_url'],
    config['freqtrade']['long']['user'],
    config['freqtrade']['long']['pass']
)
short_client = FTClient(
    config['freqtrade']['short']['base_url'],
    config['freqtrade']['short']['user'],
    config['freqtrade']['short']['pass']
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    await update.message.reply_text("🤖 Tiny Orchestrator 已启动！\n使用 /help 查看可用命令。")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    arm_status = ""
    if config['telegram']['require_arm']:
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

⚙️ **设置命令：**
• `/basket_set <pairs...>` - 设置篮子
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
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def arm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /arm 命令"""
    # 检查是否在目标群组和 Topic
    if update.message.chat.id != config['telegram']['chat_id']:
        return
    if update.message.message_thread_id != config['telegram']['topic_id']:
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
    if update.message.chat.id != config['telegram']['chat_id']:
        return
    if update.message.message_thread_id != config['telegram']['topic_id']:
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
                message += f"  {i}. `{pair}`\n"
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
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh_basket")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 获取篮子信息失败: {str(e)}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /status 命令"""
    # 检查是否在目标群组和 Topic
    if update.message.chat.id != config['telegram']['chat_id']:
        return
    if update.message.message_thread_id != config['telegram']['topic_id']:
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
            message += f"🔵 **多仓实例**: 🔴 连接失败 ({str(e)[:50]}...)\n"
        
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
            message += f"\n🔴 **空仓实例**: 🔴 连接失败 ({str(e)[:50]}...)\n"
        
        # 总结
        try:
            total_positions = long_count + short_count
            message += f"\n📊 **总计**: {total_positions} 个活跃持仓"
        except:
            message += "\n📊 **总计**: 无法统计"
        
        # 创建内联键盘
        keyboard = [
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh_status")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 获取状态信息失败: {str(e)}")

async def basket_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /basket_set 命令"""
    # 检查是否在目标群组和 Topic
    if update.message.chat.id != config['telegram']['chat_id']:
        return
    if update.message.message_thread_id != config['telegram']['topic_id']:
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
            
            # 格式校验：BASE/QUOTE
            if re.match(r'^[A-Z0-9]+/[A-Z0-9]+$', pair_upper):
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
    if update.message.chat.id != config['telegram']['chat_id']:
        return
    if update.message.message_thread_id != config['telegram']['topic_id']:
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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理内联键盘按钮回调"""
    query = update.callback_query
    await query.answer()  # 立即响应回调
    
    # 检查是否在目标群组和 Topic
    if query.message.chat.id != config['telegram']['chat_id']:
        return
    if query.message.message_thread_id != config['telegram']['topic_id']:
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
                    message += f"  {i}. `{pair}`\n"
            else:
                message += "🛒 **篮子内容**: 空\n"
            
            message += "\n⚙️ **交易参数**:\n"
            message += f"  • 每笔名义: `{cfg['defaults']['stake']}` USDT\n"
            message += f"  • 延迟时间: `{cfg['defaults']['delay_ms']}` ms\n"
            message += f"  • 轮询超时: `{cfg['defaults']['poll_timeout_sec']}` 秒\n"
            message += f"  • 轮询间隔: `{cfg['defaults']['poll_interval_sec']}` 秒\n"
            
            keyboard = [
                [InlineKeyboardButton("🔄 刷新", callback_data="refresh_basket")]
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
                message += f"🔵 **多仓实例**: 🔴 连接失败 ({str(e)[:50]}...)\n"
            
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
                message += f"\n🔴 **空仓实例**: 🔴 连接失败 ({str(e)[:50]}...)\n"
            
            # 总结
            try:
                total_positions = long_count + short_count
                message += f"\n📊 **总计**: {total_positions} 个活跃持仓"
            except:
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
            
    except Exception as e:
        await query.edit_message_text(f"❌ 刷新失败: {str(e)}")

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
    if update.message.chat.id != config['telegram']['chat_id']:
        return  # 忽略非目标群组
    
    # 检查是否在目标 Topic
    if update.message.message_thread_id != config['telegram']['topic_id']:
        return  # 忽略非目标 Topic
    
    # 在目标 Topic 内，回复 pong
    await update.message.reply_text("pong")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """错误处理"""
    print(f"Telegram Bot 错误: {context.error}")

def run_telegram_bot():
    """启动 Telegram Bot"""
    # 创建应用
    application = Application.builder().token(config['telegram']['token']).build()
    
    # 添加处理器
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("arm", arm_command))
    application.add_handler(CommandHandler("basket", basket_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("basket_set", basket_set_command))
    application.add_handler(CommandHandler("stake", stake_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # 添加错误处理
    application.add_error_handler(error_handler)
    
    # 启动 Bot
    print("🤖 启动 Telegram Bot...")
    print(f"   目标群组: {config['telegram']['chat_id']}")
    print(f"   目标 Topic: {config['telegram']['topic_id']}")
    print(f"   管理员: {config['telegram']['admins']}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# 启动 Bot
run_telegram_bot()
