import yaml
import os
import re

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
    
    print("boot ok")
