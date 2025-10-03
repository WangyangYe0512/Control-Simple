import os
import time
import threading
from typing import Callable, Optional, Dict, Any
import httpx
from datetime import datetime

# Baseline state persisted locally
BASELINE_FILE = os.path.join('runtime', 'auto_baseline.txt')
PEAK_FILE = os.path.join('runtime', 'auto_peak.txt')
CURRENT_DIRECTION_FILE = os.path.join('runtime', 'auto_direction.txt')

def _log(message: str):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {message}"
    try:
        print(message)
    except Exception:
        pass
    try:
        os.makedirs('runtime', exist_ok=True)
        with open('runtime/audit.log', 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_baseline() -> Optional[float]:
    try:
        if not os.path.exists(BASELINE_FILE):
            return None
        with open(BASELINE_FILE, 'r', encoding='utf-8') as f:
            return float(f.read().strip())
    except Exception:
        return None


def _write_baseline(value: float):
    try:
        os.makedirs('runtime', exist_ok=True)
        with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
            f.write(str(value))
    except Exception as e:
        print(f"写入基准失败: {e}")

def _read_peak() -> Optional[float]:
    try:
        if not os.path.exists(PEAK_FILE):
            return None
        with open(PEAK_FILE, 'r', encoding='utf-8') as f:
            return float(f.read().strip())
    except Exception:
        return None

def _write_peak(value: float):
    try:
        os.makedirs('runtime', exist_ok=True)
        with open(PEAK_FILE, 'w', encoding='utf-8') as f:
            f.write(str(value))
    except Exception as e:
        print(f"写入最高点失败: {e}")

def _read_direction() -> Optional[str]:
    try:
        if not os.path.exists(CURRENT_DIRECTION_FILE):
            return None
        with open(CURRENT_DIRECTION_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return None

def _write_direction(direction: str):
    try:
        os.makedirs('runtime', exist_ok=True)
        with open(CURRENT_DIRECTION_FILE, 'w', encoding='utf-8') as f:
            f.write(direction)
    except Exception as e:
        print(f"写入方向失败: {e}")


def _auto_toggle_loop(
    get_config: Callable[[], Dict[str, Any]],
    start_long: Callable[[], None],
    stop_long: Callable[[], None],
    start_short: Callable[[], None],
    stop_short: Callable[[], None],
):
    _log("[auto] background thread started")
    while True:
        try:
            cfg = get_config() or {}
            ext = cfg.get('external_status', {}) or {}

            url = (ext.get('url') or '').rstrip('/')
            if not url:
                _log("[auto] external_status.url not set, sleep 60s")
                time.sleep(60)
                continue
            # 使用 Freqtrade 官方 /api/v1/status，基于当前持仓计算总盈利
            fetch_url = f"{url}/api/v1/status"

            interval_sec = int(ext.get('interval_sec', 30))
            threshold = float(ext.get('threshold', 400.0))
            user = ext.get('user')
            passwd = ext.get('pass')
            auth = (user, passwd) if user and passwd else None

            # 获取外部状态
            data = None
            resp = None
            try:
                resp = httpx.get(fetch_url, auth=auth, timeout=15.0)
                ct = (resp.headers.get('content-type') or '').lower()
                if 'application/json' in ct:
                    data = resp.json()
                else:
                    txt = resp.text or ''
                    if txt.strip().startswith('{') or txt.strip().startswith('['):
                        import json as _json
                        data = _json.loads(txt)
            except Exception as e:
                _log(f"[auto] fetch failed: {e}")
                time.sleep(interval_sec)
                continue
            if data is None:
                preview = (resp.text or "")[:200] if resp is not None else ""
                _log(f"[auto] no JSON from {fetch_url}, preview={preview!r}")
                time.sleep(interval_sec)
                continue

            # 提取 PnL
            pnl = None
            if isinstance(data, dict) or isinstance(data, list):
                # 统一拿到持仓列表
                if isinstance(data, dict):
                    trades = data.get('trades') if isinstance(data.get('trades'), list) else []
                else:
                    trades = data

                # 计算总盈利（当前持仓的总 profit_abs）。若缺失 profit_abs，尝试用 stake_amount*profit_pct/100 估算
                total_profit = 0.0
                if isinstance(trades, list):
                    for t in trades:
                        if not isinstance(t, dict):
                            continue
                        pa = t.get('profit_abs')
                        if isinstance(pa, (int, float)):
                            total_profit += float(pa)
                            continue
                        # 估算
                        pct = t.get('profit_pct')
                        stake_amt = t.get('stake_amount') or t.get('stake_amount_fiat') or t.get('amount')
                        try:
                            if pct is not None and stake_amt is not None:
                                # profit_pct 多为百分比数值，如 1.23 表示 1.23%
                                total_profit += float(stake_amt) * float(pct) / 100.0
                        except Exception:
                            pass
                pnl = total_profit

            if pnl is None:
                if isinstance(data, dict):
                    _log(f"[auto] pnl not found, top-level keys={list(data.keys())}")
                else:
                    preview = (resp.text or "")[:200] if 'resp' in locals() else ""
                    _log(f"[auto] response not JSON. preview={preview!r}")
                _log("[auto] pnl not found in response, sleep")
                time.sleep(interval_sec)
                continue

            try:
                pnl_value = float(pnl)
            except Exception:
                _log(f"[auto] pnl not numeric: {pnl}")
                time.sleep(interval_sec)
                continue

            baseline = _read_baseline()
            current_direction = _read_direction()
            peak = _read_peak()
            
            if baseline is None:
                _write_baseline(pnl_value)
                _write_peak(pnl_value)
                _write_direction('none')
                _log(f"[auto] init baseline -> {pnl_value:.2f}, peak -> {pnl_value:.2f}")
                time.sleep(interval_sec)
                continue

            # 检查是否需要更新最高点
            if current_direction and current_direction != 'none':
                # 如果当前有方向，检查是否继续向有利方向移动
                if current_direction == 'long' and pnl_value > baseline:
                    # 做多方向，PnL 继续增长，更新最高点
                    if peak is None or pnl_value > peak:
                        _write_peak(pnl_value)
                        _log(f"[auto] update peak -> {pnl_value:.2f} (long direction)")
                        # 发送最高点更新通知
                        try:
                            tg = cfg.get('telegram', {})
                            token = tg.get('token')
                            chat_id = tg.get('chat_id')
                            topic_id = tg.get('topic_id')
                            if token and chat_id:
                                text = f"📈 最高点更新 (做多方向)\n📊 新最高点: `{pnl_value:.2f}`"
                                api_url = f"https://api.telegram.org/bot{token}/sendMessage"
                                payload = {
                                    'chat_id': chat_id,
                                    'text': text,
                                    'parse_mode': 'Markdown',
                                }
                                if topic_id is not None:
                                    payload['message_thread_id'] = topic_id
                                httpx.post(api_url, json=payload, timeout=10.0)
                        except Exception as e:
                            _log(f"[auto] peak update telegram error: {e}")
                elif current_direction == 'short' and pnl_value < baseline:
                    # 做空方向，PnL 继续下降，更新最高点（做空的最高点是最低值）
                    if peak is None or pnl_value < peak:
                        _write_peak(pnl_value)
                        _log(f"[auto] update peak -> {pnl_value:.2f} (short direction)")
                        # 发送最高点更新通知
                        try:
                            tg = cfg.get('telegram', {})
                            token = tg.get('token')
                            chat_id = tg.get('chat_id')
                            topic_id = tg.get('topic_id')
                            if token and chat_id:
                                text = f"📉 最高点更新 (做空方向)\n📊 新最高点: `{pnl_value:.2f}`"
                                api_url = f"https://api.telegram.org/bot{token}/sendMessage"
                                payload = {
                                    'chat_id': chat_id,
                                    'text': text,
                                    'parse_mode': 'Markdown',
                                }
                                if topic_id is not None:
                                    payload['message_thread_id'] = topic_id
                                httpx.post(api_url, json=payload, timeout=10.0)
                        except Exception as e:
                            _log(f"[auto] peak update telegram error: {e}")

            # 检查是否需要反向切换
            direction = None
            if current_direction and current_direction != 'none' and peak is not None:
                # 从最高点回调 500 才反向
                if current_direction == 'long' and pnl_value <= peak - 500:
                    direction = 'short'  # 做多回调，切换到做空
                elif current_direction == 'short' and pnl_value >= peak + 500:
                    direction = 'long'  # 做空回调，切换到做多
            else:
                # 初始触发条件（没有方向或最高点时）
                delta = pnl_value - baseline
                if delta <= -threshold:
                    direction = 'long'
                elif delta >= threshold:
                    direction = 'short'
            
            _log(f"[auto] pnl={pnl_value:.2f} baseline={baseline:.2f} peak={peak:.2f if peak else 'None'} direction={current_direction} new_direction={direction}")

            if direction:
                if direction == 'long':
                    try:
                        result = stop_short()
                        _log(f"[auto] stop_short result: {result}")
                    except Exception as e:
                        _log(f"[auto] stop_short error: {e}")
                    try:
                        result = start_long()
                        _log(f"[auto] start_long result: {result}")
                    except Exception as e:
                        _log(f"[auto] start_long error: {e}")
                else:
                    try:
                        result = stop_long()
                        _log(f"[auto] stop_long result: {result}")
                    except Exception as e:
                        _log(f"[auto] stop_long error: {e}")
                    try:
                        result = start_short()
                        _log(f"[auto] start_short result: {result}")
                    except Exception as e:
                        _log(f"[auto] start_short error: {e}")

                # 直接调用 Telegram Bot API，避免依赖 PTB 事件循环
                try:
                    tg = cfg.get('telegram', {})
                    token = tg.get('token')
                    chat_id = tg.get('chat_id')
                    topic_id = tg.get('topic_id')
                    if token and chat_id:
                        text = (
                            f"⚙️ 自动切换触发\n"
                            f"📐 基准: `{baseline:.2f}` → `{pnl_value:.2f}` (Δ {delta:+.2f})\n"
                            f"🧭 开启方向: {'🚀 做多' if direction=='long' else '🔴 做空'}\n"
                            f"🔵 多实例: {'启动' if direction=='long' else '停止'}\n"
                            f"🔴 空实例: {'停止' if direction=='long' else '启动'}"
                        )
                        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
                        payload = {
                            'chat_id': chat_id,
                            'text': text,
                            'parse_mode': 'Markdown',
                        }
                        if topic_id is not None:
                            payload['message_thread_id'] = topic_id
                        r = httpx.post(api_url, json=payload, timeout=10.0)
                        _log(f"[auto] telegram sent status={r.status_code}")
                except Exception as e:
                    _log(f"[auto] telegram error: {e}")

                # 更新基准和方向
                _write_baseline(pnl_value)
                _write_direction(direction)
                _write_peak(pnl_value)  # 新方向的新最高点
                _log(f"[auto] update baseline -> {pnl_value:.2f} (direction={direction})")

            time.sleep(interval_sec)
        except Exception as e:
            _log(f"[auto] loop error: {e}")
            time.sleep(30)


def schedule_auto_toggle(
    application,
    get_config: Callable[[], Dict[str, Any]],
    start_long: Callable[[], None],
    stop_long: Callable[[], None],
    start_short: Callable[[], None],
    stop_short: Callable[[], None],
):
    # 使用守护线程运行同步轮询，完全独立于 PTB 的事件循环
    th = threading.Thread(
        target=_auto_toggle_loop,
        args=(get_config, start_long, stop_long, start_short, stop_short),
        daemon=True,
    )
    th.start()


