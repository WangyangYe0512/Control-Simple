import yaml
import os
import re
import httpx
from typing import Optional, Dict, Any

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
    
    # 测试不存在的路径
    print("\n=== 错误处理测试 ===")
    # 创建一个指向本地无效端口的客户端进行测试
    test_client = FTClient("http://127.0.0.1:9999", "test", "test")
    print(f"Test 客户端: {test_client.base_url}")
    
    try:
        result = test_client._request("GET", "/test")
        print(f"测试路径结果: {result}")
    except Exception as e:
        print(f"网络错误测试通过: {type(e).__name__}")
    
    # 测试 list_positions()
    print("\n=== list_positions 测试 ===")
    try:
        positions = test_client.list_positions()
        print(f"404 端点测试结果: {positions}")
    except Exception as e:
        print(f"list_positions 异常: {e}")
    
    # 测试真实实例的 list_positions
    print("\n=== 真实实例测试 ===")
    try:
        long_positions = long_client.list_positions()
        print(f"Long 实例持仓: {len(long_positions)} 个")
        if long_positions:
            print(f"持仓详情: {long_positions[:2]}...")  # 只显示前2个
    except Exception as e:
        print(f"Long 实例异常: {e}")
    
    try:
        short_positions = short_client.list_positions()
        print(f"Short 实例持仓: {len(short_positions)} 个")
        if short_positions:
            print(f"持仓详情: {short_positions[:2]}...")  # 只显示前2个
    except Exception as e:
        print(f"Short 实例异常: {e}")

# 测试交易方法（逐一测试功能）
print("\n=== 交易方法详细测试 ===")

# 测试 1: cancel_open_orders()
print("1. 测试 cancel_open_orders():")
try:
    result1 = long_client.cancel_open_orders()
    print(f"   Long 客户端: {result1}")
    result2 = short_client.cancel_open_orders()
    print(f"   Short 客户端: {result2}")
except Exception as e:
    print(f"   错误: {e}")

# 测试 2: forcebuy() - 实际调用（使用实际可用的交易对）
print("\n2. 测试 forcebuy() [实际调用]:")
try:
    result = long_client.forcebuy("BNB/USDT:USDT", 50)  # 使用实际可用的交易对
    print(f"   Long 客户端 forcebuy 结果: {result}")
except Exception as e:
    print(f"   Long 客户端 forcebuy 错误: {e}")

# 测试 3: forceshort() - 实际调用（使用实际可用的交易对）
print("\n3. 测试 forceshort() [实际调用]:")
try:
    result = short_client.forceshort("ETH/USDT:USDT", 50)  # 使用不同的交易对避免冲突
    print(f"   Short 客户端 forceshort 结果: {result}")
except Exception as e:
    print(f"   Short 客户端 forceshort 错误: {e}")

# 等待一下让订单处理
import time
print("\n   等待 2 秒让订单处理...")
time.sleep(2)

# 重新获取持仓状态
print("\n4. 检查新的持仓状态:")
try:
    new_long_positions = long_client.list_positions()
    print(f"   Long 客户端持仓: {len(new_long_positions)} 个")
    for pos in new_long_positions:
        pair = pos.get('pair', 'N/A')
        is_short = pos.get('is_short', False)
        trade_id = pos.get('trade_id', 'N/A')
        print(f"     - {pair} ({'空仓' if is_short else '多仓'}, ID: {trade_id})")
    
    new_short_positions = short_client.list_positions()
    print(f"   Short 客户端持仓: {len(new_short_positions)} 个")
    for pos in new_short_positions:
        pair = pos.get('pair', 'N/A')
        is_short = pos.get('is_short', False)
        trade_id = pos.get('trade_id', 'N/A')
        print(f"     - {pair} ({'空仓' if is_short else '多仓'}, ID: {trade_id})")
except Exception as e:
    print(f"   获取新持仓失败: {e}")

# 测试 5: forcesell() - 实际调用
print("\n5. 测试 forcesell() [实际调用]:")
try:
    new_long_positions = long_client.list_positions()
    if new_long_positions:
        # 找到多仓
        long_trade = None
        for pos in new_long_positions:
            if not pos.get('is_short', False):
                long_trade = pos
                break
        
        if long_trade:
            test_pair = long_trade.get('pair', 'N/A')
            result = long_client.forcesell(test_pair)
            print(f"   Long 客户端 forcesell({test_pair}) 结果: {result}")
        else:
            print("   当前无多仓持仓")
    else:
        print("   当前无持仓")
except Exception as e:
    print(f"   Long 客户端 forcesell 错误: {e}")

# 测试 6: forcecover() - 实际调用
print("\n6. 测试 forcecover() [实际调用]:")
try:
    new_short_positions = short_client.list_positions()
    if new_short_positions:
        # 找到空仓
        short_trade = None
        for pos in new_short_positions:
            if pos.get('is_short', False):
                short_trade = pos
                break
        
        if short_trade:
            test_pair = short_trade.get('pair', 'N/A')
            result = short_client.forcecover(test_pair)
            print(f"   Short 客户端 forcecover({test_pair}) 结果: {result}")
        else:
            print("   当前无空仓持仓")
    else:
        print("   当前无持仓")
except Exception as e:
    print(f"   Short 客户端 forcecover 错误: {e}")

# 测试错误处理
print("\n7. 测试错误处理:")
try:
    test_client = FTClient("http://127.0.0.1:9999", "test", "test")
    result = test_client.forcebuy("BTC/USDT", 100)
    print(f"   假地址测试: {result}")
except Exception as e:
    print(f"   假地址测试 - 预期错误: {type(e).__name__}")

print("\n=== 交易方法测试完成 ===")
print("boot ok")
