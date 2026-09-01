import ccxt.async_support as ccxt
import asyncio
import os
import sys
import json
import time
import math
import logging
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.request
import urllib.error

# Khắc phục hiển thị tiếng Việt trên Windows Console
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("okx-macro-btc-eth")

# Tải cấu hình từ tệp .env nếu có (chạy cục bộ)
if os.path.exists(".env"):
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning(f"⚠️ Không thể đọc file .env: {e}")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
PORT = int(os.environ.get("PORT", 8080))

INITIAL_BALANCE = 100.0                         # Vốn khởi tạo $100 USDT
TRADING_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAME = "4h"                                # Khung nến lớn 4H (4 Giờ)
LEVERAGE = 3                                    # Đòn bẩy thấp an toàn 3x
RISK_PER_TRADE_PCT = 0.02                       # Rủi ro tối đa 2.0% vốn mỗi lệnh
TAKER_FEE_PCT = 0.0005                          # Phí sàn 0.05%

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "okx_macro_btc_eth_state.json")

state = {}
exchange = ccxt.okx({'enableRateLimit': True})

def save_state_atomic():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp_file = STATE_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        os.rename(tmp_file, STATE_FILE)
    except Exception as e:
        logger.error(f"🔴 Lỗi lưu atomic state: {e}")

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            logger.info(f"💾 Đã nạp trạng thái Macro Bot: Vốn ${state.get('balance', INITIAL_BALANCE):.2f} USDT | PnL: ${state.get('total_pnl', 0):+.2f} USDT | Số lệnh: {state.get('trades_count', 0)}")
        except Exception as e:
            logger.error(f"🔴 Lỗi đọc state: {e}")
            init_new_state()
    else:
        init_new_state()

def init_new_state():
    global state
    state = {
        "balance": INITIAL_BALANCE,
        "total_pnl": 0.0,
        "total_fees_paid": 0.0,
        "trades_count": 0,
        "wins": 0,
        "losses": 0,
        "positions": {}, # {symbol: {type, entry, sl, tp1, contracts, is_tp1_hit, ...}}
        "history_trades": [],
        "last_report_time": 0
    }
    save_state_atomic()
    logger.info("🆕 Đã khởi tạo danh mục Macro 4H BTC/ETH Bot mới với vốn 100.0 USDT.")

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            pass
    except Exception as e:
        logger.error(f"🔴 Lỗi gửi Telegram: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        active_pos_html = ""
        for s, p in state.get('positions', {}).items():
            active_pos_html += f"<li><b>{s}</b>: {p.get('type')} @ ${p.get('entry', 0):.2f} (SL: ${p.get('sl', 0):.2f} | TP1: ${p.get('tp1', 0):.2f})</li>"
        if not active_pos_html: active_pos_html = "<li><i>Hiện không có vị thế mở (Đang canh nến 4H)</i></li>"

        win_rate = (state.get('wins', 0) / state.get('trades_count', 1) * 100) if state.get('trades_count', 0) > 0 else 0
        html = f"""
        <html>
            <head><title>OKX Macro 4H BTC/ETH Trend Rider</title></head>
            <body style="font-family: Arial, sans-serif; padding: 25px; background: #0f172a; color: #f8fafc;">
                <h1 style="color: #f59e0b;">👑 OKX Macro 4H BTC/ETH Trend Rider (Paper Trading)</h1>
                <p>Trạng thái: <b style="color: #22c55e;">ĐANG HOẠT ĐỘNG 24/7 (KHUNG 4H SĂN SÓNG LỚN)</b></p>
                <p>Số dư ví: <b>${state.get('balance', 100.0):.2f} USDT</b></p>
                <p>Tổng PnL: <b style="color: {'#22c55e' if state.get('total_pnl', 0) >= 0 else '#ef4444'}; font-size: 1.3em;">${state.get('total_pnl', 0.0):+.2f} USDT</b></p>
                <p>Tỷ lệ Thắng (Win Rate): <b>{win_rate:.1f}%</b> ({state.get('wins', 0)} Thắng / {state.get('losses', 0)} Thua | Tổng {state.get('trades_count', 0)} lệnh)</p>
                <hr style="border-color: #334155;">
                <h3>📊 Vị thế đang mở:</h3>
                <ul>{active_pos_html}</ul>
            </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

def start_health_check_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"🌐 Health check HTTP server đang chạy trên cổng {PORT}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

def calculate_ema(prices, length):
    ema = [0.0] * len(prices)
    if len(prices) < length: return ema
    sma = sum(prices[:length]) / length
    ema[length - 1] = sma
    alpha = 2.0 / (length + 1)
    for i in range(length, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1 - alpha)
    return ema

def calculate_atr(candles, length=14):
    n = len(candles)
    atr = [0.0] * n
    if n <= length: return atr
    tr = [0.0] * n
    for i in range(1, n):
        h, l, pc = candles[i][2], candles[i][3], candles[i-1][4]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr[length] = sum(tr[1:length+1]) / length
    for i in range(length + 1, n):
        atr[i] = (atr[i-1] * (length - 1) + tr[i]) / length
    return atr

async def check_macro_symbol(symbol):
    try:
        candles_4h = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=60)
        ticker = await exchange.fetch_ticker(symbol)
        curr_p = float(ticker.get('last', 0.0))
        if curr_p <= 0 or len(candles_4h) < 30: return

        closes = [c[4] for c in candles_4h]
        vols = [c[5] for c in candles_4h]
        
        ema21 = calculate_ema(closes, 21)
        ema55 = calculate_ema(closes, 55)
        atr = calculate_atr(candles_4h, 14)
        
        vol_ma20 = sum(vols[-21:-1]) / 20.0
        
        positions = state.setdefault("positions", {})
        pos = positions.get(symbol)

        # 1. QUẢN LÝ VỊ THẾ ĐANG MỞ
        if pos:
            # Check TP1 (50% position @ 1.5R)
            if not pos.get("is_tp1_hit", False):
                if pos["type"] == "LONG" and curr_p >= pos["tp1"]:
                    pos["is_tp1_hit"] = True
                    pos["sl"] = pos["entry"] * 1.002 # Dời SL về Entry + Phí hòa vốn
                    fee = (pos["contracts"] * pos["tp1"] * 0.50) * TAKER_FEE_PCT
                    pnl_tp1 = (pos["tp1"] - pos["entry"]) * pos["contracts"] * 0.50 - fee
                    state["balance"] += pnl_tp1
                    state["total_pnl"] += pnl_tp1
                    save_state_atomic()
                    
                    msg = (
                        f"🎯 <b>[MACRO 4H RIDER - CHỐT LỜI TP1 50%]</b>\n\n"
                        f"👑 <b>Cặp:</b> {symbol}\n"
                        f"👉 <b>Khớp TP1 (1.5R):</b> ${pos['tp1']:.2f}\n"
                        f"💰 <b>Lãi ròng TP1:</b> 🟢 <b>+{pnl_tp1:.2f} USDT</b>\n"
                        f"🛡️ <b>Đã dời SL về Entry khóa hòa vốn:</b> ${pos['sl']:.2f}\n"
                        f"🚀 <b>50% còn lại đang gồng sóng chu kỳ 4H theo Trailing Stop!</b>\n"
                        f"🏦 <b>Số dư ví:</b> <b>${state['balance']:.2f} USDT</b>"
                    )
                    send_telegram_message(msg)
                    
                elif pos["type"] == "SHORT" and curr_p <= pos["tp1"]:
                    pos["is_tp1_hit"] = True
                    pos["sl"] = pos["entry"] * 0.998
                    fee = (pos["contracts"] * pos["tp1"] * 0.50) * TAKER_FEE_PCT
                    pnl_tp1 = (pos["entry"] - pos["tp1"]) * pos["contracts"] * 0.50 - fee
                    state["balance"] += pnl_tp1
                    state["total_pnl"] += pnl_tp1
                    save_state_atomic()
                    
                    msg = (
                        f"🎯 <b>[MACRO 4H RIDER - CHỐT LỜI TP1 50%]</b>\n\n"
                        f"👑 <b>Cặp:</b> {symbol}\n"
                        f"👉 <b>Khớp TP1 (1.5R):</b> ${pos['tp1']:.2f}\n"
                        f"💰 <b>Lãi ròng TP1:</b> 🟢 <b>+{pnl_tp1:.2f} USDT</b>\n"
                        f"🛡️ <b>Đã dời SL về Entry khóa hòa vốn:</b> ${pos['sl']:.2f}\n"
                        f"🚀 <b>50% còn lại đang gồng sóng chu kỳ 4H theo Trailing Stop!</b>\n"
                        f"🏦 <b>Số dư ví:</b> <b>${state['balance']:.2f} USDT</b>"
                    )
                    send_telegram_message(msg)

            # Trailing Stop 2.0x ATR 4H cho 50% còn lại sau khi cắn TP1
            if pos.get("is_tp1_hit", False):
                if pos["type"] == "LONG":
                    new_sl = curr_p - 2.0 * atr[-1]
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl
                        save_state_atomic()
                else:
                    new_sl = curr_p + 2.0 * atr[-1]
                    if new_sl < pos["sl"]:
                        pos["sl"] = new_sl
                        save_state_atomic()

            # Check Exit
            closed = False
            pnl_final = 0.0
            if pos.get("is_tp1_hit", False):
                if pos["type"] == "LONG" and curr_p <= pos["sl"]:
                    closed = True
                    fee = (pos["contracts"] * pos["sl"] * 0.50) * TAKER_FEE_PCT
                    pnl_final = (pos["sl"] - pos["entry"]) * pos["contracts"] * 0.50 - fee
                elif pos["type"] == "SHORT" and curr_p >= pos["sl"]:
                    closed = True
                    fee = (pos["contracts"] * pos["sl"] * 0.50) * TAKER_FEE_PCT
                    pnl_final = (pos["entry"] - pos["sl"]) * pos["contracts"] * 0.50 - fee
            else:
                if pos["type"] == "LONG" and curr_p <= pos["sl"]:
                    closed = True
                    fee = (pos["contracts"] * pos["sl"]) * TAKER_FEE_PCT
                    pnl_final = (pos["sl"] - pos["entry"]) * pos["contracts"] - fee
                elif pos["type"] == "SHORT" and curr_p >= pos["sl"]:
                    closed = True
                    fee = (pos["contracts"] * pos["sl"]) * TAKER_FEE_PCT
                    pnl_final = (pos["entry"] - pos["sl"]) * pos["contracts"] - fee

            if closed:
                state["balance"] += pnl_final
                state["total_pnl"] += pnl_final
                state["trades_count"] += 1
                if pnl_final >= 0 or pos.get("is_tp1_hit", False): state["wins"] += 1
                else: state["losses"] += 1
                
                outcome_text = "🟢 CHỐT LÃI SÓNG LỚN 4H THÀNH CÔNG" if (pnl_final >= 0 or pos.get("is_tp1_hit", False)) else "🔴 CẮT LỖ BẢO VỆ 2% VỐN"
                msg = (
                    f"🏁 <b>[MACRO 4H RIDER - ĐÓNG LỆNH]</b>\n\n"
                    f"👑 <b>Cặp:</b> {symbol}\n"
                    f"⚡ <b>Kết quả:</b> {outcome_text}\n"
                    f"👉 <b>Giá Entry:</b> ${pos['entry']:.2f} ──► <b>Giá Exit:</b> ${pos['sl']:.2f}\n"
                    f"💰 <b>PnL phần còn lại:</b> {pnl_final:+.2f} USDT\n"
                    f"─────────────────────────\n"
                    f"🏦 <b>Số dư ví hiện tại:</b> <b>${state['balance']:.2f} USDT</b> (Tổng PnL: ${state['total_pnl']:+.2f} USDT)"
                )
                send_telegram_message(msg)
                del positions[symbol]
                save_state_atomic()

        # 2. QUÉT TÍN HIỆU MỞ LỆNH 4H MỚI (NẾU CHƯA CÓ VỊ THẾ)
        if symbol not in positions:
            last_c = candles_4h[-2] # nến 4H vừa đóng cửa
            
            # Sóng tăng 4H: EMA21 > EMA55 + Nến 4H bứt phá EMA21 + Volume > 1.2x MA20
            is_long = (ema21[-2] > ema55[-2]) and (candles_4h[-3][4] <= ema21[-3]) and (last_c[4] > ema21[-2]) and (last_c[5] > 1.2 * vol_ma20)
            # Sóng giảm 4H: EMA21 < EMA55 + Nến 4H gãy EMA21 + Volume > 1.2x MA20
            is_short = (ema21[-2] < ema55[-2]) and (candles_4h[-3][4] >= ema21[-3]) and (last_c[4] < ema21[-2]) and (last_c[5] > 1.2 * vol_ma20)
            
            if is_long or is_short:
                sl_dist = 1.8 * atr[-2]
                sl = curr_p - sl_dist if is_long else curr_p + sl_dist
                tp1 = curr_p + 1.5 * sl_dist if is_long else curr_p - 1.5 * sl_dist # TP1 @ 1.5R
                
                risk_amt = state["balance"] * RISK_PER_TRADE_PCT
                contracts = risk_amt / sl_dist if sl_dist > 0 else 0
                
                # Format contract precision
                if "BTC" in symbol: contracts = round(contracts, 4)
                elif "ETH" in symbol: contracts = round(contracts, 3)
                
                fee = (contracts * curr_p) * TAKER_FEE_PCT
                state["balance"] -= fee
                state["total_fees_paid"] += fee
                
                positions[symbol] = {
                    "type": "LONG" if is_long else "SHORT",
                    "entry": curr_p,
                    "sl": sl,
                    "tp1": tp1,
                    "contracts": contracts,
                    "is_tp1_hit": False,
                    "open_time": datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
                }
                save_state_atomic()
                
                msg = (
                    f"👑 <b>[MACRO 4H RIDER - MỞ VỊ THẾ CHU KỲ MỚI]</b>\n\n"
                    f"🪙 <b>Cặp:</b> {symbol}\n"
                    f"⚡ <b>Loại lệnh:</b> {'🟢 BUY LONG (Sóng Tăng 4H)' if is_long else '🔴 SELL SHORT (Sóng Giảm 4H)'} ({contracts} HĐ)\n"
                    f"👉 <b>Giá Entry:</b> ${curr_p:.2f}\n"
                    f"🎯 <b>Mục tiêu TP1 (1.5R):</b> ${tp1:.2f}\n"
                    f"🛡️ <b>Mốc Stop Loss:</b> ${sl:.2f} (Rủi ro: -{risk_amt:.2f} USDT = 2.0%)\n"
                    f"🚀 <b>Gồng lãi 50% còn lại theo sóng chu kỳ lớn 4H!</b>"
                )
                send_telegram_message(msg)

    except Exception as e:
        logger.error(f"🔴 Lỗi Macro Bot quét {symbol}: {e}")

async def send_periodic_recap():
    now_vn = datetime.now(timezone(timedelta(hours=7)))
    if now_vn.hour in [7, 15, 23] and (time.time() - state.get("last_report_time", 0)) > 3000:
        state["last_report_time"] = time.time()
        save_state_atomic()
        win_rate = (state.get('wins', 0) / state.get('trades_count', 1) * 100) if state.get('trades_count', 0) > 0 else 0
        
        msg = (
            f"📊 <b>[MACRO 4H BTC/ETH RIDER - BÁO CÁO ĐỊNH KỲ]</b>\n\n"
            f"⏰ <b>Thời gian:</b> {now_vn.strftime('%H:%M %d/%m/%Y')} (VN)\n"
            f"💰 <b>Vốn khởi tạo:</b> $100.00 USDT\n"
            f"🏦 <b>Số dư hiện tại:</b> <b>${state['balance']:.2f} USDT</b> ({state['total_pnl']:+.2f} USDT PnL)\n"
            f"🏆 <b>Hiệu suất:</b> {win_rate:.1f}% Win Rate ({state.get('wins', 0)} Thắng / {state.get('losses', 0)} Thua)\n"
            f"💸 <b>Tổng phí sàn đã nộp:</b> -${state['total_fees_paid']:.4f} USDT (Siêu tiết kiệm phí)\n"
            f"─────────────────────────\n"
            f"🛡️ <i>Chiến lược Macro 4H chuyên săn các con sóng chu kỳ lớn của Bitcoin & Ethereum!</i>"
        )
        send_telegram_message(msg)

async def main():
    load_state()
    start_health_check_server()
    
    start_msg = (
        f"🚀 <b>[OKX MACRO 4H BTC/ETH RIDER ĐÃ KHỞI ĐỘNG]</b>\n\n"
        f"💰 <b>Vốn khởi tạo:</b> ${INITIAL_BALANCE:.2f} USDT\n"
        f"🎯 <b>Chiến lược:</b> Macro Wave 4H (EMA 21/55 Wave + Volume Cá Voi)\n"
        f"👑 <b>Danh mục theo dõi:</b> {', '.join(TRADING_SYMBOLS)}\n"
        f"🛡️ <b>Quản lý vốn:</b> 2.0% Risk / lệnh | TP1 50% @ 1.5R | Gồng lãi 50% theo sóng lớn 4H\n"
        f"⏳ <b>Đặc điểm:</b> Ít lệnh, không bị nhiễu loạn nến nhỏ, phí sàn gần như bằng 0!\n\n"
        f"🤖 <i>Bot đã kết nối OKX Public API & sẵn sàng rình săn sóng lớn!</i>"
    )
    send_telegram_message(start_msg)
    
    while True:
        try:
            for s in TRADING_SYMBOLS:
                await check_macro_symbol(s)
                await asyncio.sleep(2)
            await send_periodic_recap()
        except Exception as e:
            logger.error(f"🔴 Lỗi Macro main loop: {e}")
        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
