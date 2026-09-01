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
logger = logging.getLogger("okx-dynamic-grid-bot")

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
TRADING_PAIRS = ["LINK/USDT", "SOL/USDT", "DOGE/USDT"] # Danh mục lưới theo dõi
GRID_STEP_PCT = 0.008                           # Khoảng cách mỗi tầng lưới: 0.80%
ALLOCATION_PER_PAIR = 1.0 / len(TRADING_PAIRS)  # Chia đều vốn cho các cặp
TAKER_FEE_PCT = 0.0005                          # Phí giao dịch 0.05%

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "okx_dynamic_grid_state.json")

state = {}
exchange = ccxt.okx({'enableRateLimit': True})

def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔴 Lỗi lưu state: {e}")

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if "total_grid_profit" not in state:
                state["total_grid_profit"] = 0.0
            if "total_trades" not in state:
                state["total_trades"] = 0
            logger.info(f"💾 Đã nạp trạng thái: Vốn ${state.get('balance', INITIAL_BALANCE):.2f} USDT | Tổng Lãi Lưới: +${state.get('total_grid_profit', 0):.4f} USDT")
        except Exception as e:
            logger.error(f"🔴 Lỗi đọc state: {e}")
            init_new_state()
    else:
        init_new_state()

def init_new_state():
    global state
    state = {
        "balance": INITIAL_BALANCE,
        "total_grid_profit": 0.0,
        "total_fees_paid": 0.0,
        "total_trades": 0,
        "pairs_data": {},
        "history_trades": [],
        "last_report_time": 0
    }
    save_state()
    logger.info("🆕 Đã khởi tạo danh mục Dynamic Grid Bot mới với vốn 100.0 USDT.")

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
        html = f"""
        <html>
            <head><title>OKX Dynamic Grid Bot</title></head>
            <body style="font-family: Arial; padding: 20px; background: #0f172a; color: #f8fafc;">
                <h1>🤖 OKX Dynamic Volatility Grid Harvester (Paper Trading)</h1>
                <p>Status: <b style="color: #22c55e;">RUNNING 24/7</b></p>
                <p>Số dư ví: <b>${state.get('balance', 100.0):.2f} USDT</b></p>
                <p>Tổng lãi lưới đã thu hoạch: <b style="color: #22c55e;">+${state.get('total_grid_profit', 0.0):.4f} USDT</b></p>
                <p>Số vòng chốt lời: <b>{state.get('total_trades', 0)} lần</b></p>
            </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

def start_health_check_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"🌐 Health check HTTP server đang chạy trên cổng {PORT}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

async def process_grid_for_symbol(symbol):
    try:
        ticker = await exchange.fetch_ticker(symbol)
        curr_price = float(ticker.get('last', 0.0))
        if curr_price <= 0: return

        pair_alloc = state["balance"] * ALLOCATION_PER_PAIR
        pairs_data = state.setdefault("pairs_data", {})
        
        if symbol not in pairs_data or "anchor_price" not in pairs_data[symbol]:
            pairs_data[symbol] = {
                "anchor_price": curr_price,
                "total_earned": 0.0,
                "trades_count": 0,
                "last_update_ms": int(time.time() * 1000)
            }
            save_state()
            logger.info(f"📍 Khởi tạo mốc neo giá cho {symbol} @ ${curr_price:.4f}")
            return

        anchor = pairs_data[symbol]["anchor_price"]
        step_usd = anchor * GRID_STEP_PCT
        price_diff = curr_price - anchor
        steps_moved = int(price_diff / step_usd)

        if abs(steps_moved) >= 1:
            order_val = pair_alloc * 0.20 # Mỗi tầng lưới giao dịch 20% vốn của cặp
            gross_profit = abs(steps_moved) * order_val * GRID_STEP_PCT
            fee = order_val * TAKER_FEE_PCT * 2
            net_profit = gross_profit - fee

            state["balance"] += net_profit
            state["total_grid_profit"] += net_profit
            state["total_fees_paid"] += fee
            state["total_trades"] += abs(steps_moved)
            
            pairs_data[symbol]["total_earned"] += net_profit
            pairs_data[symbol]["trades_count"] += abs(steps_moved)
            pairs_data[symbol]["anchor_price"] = curr_price
            pairs_data[symbol]["last_update_ms"] = int(time.time() * 1000)

            trade_log = {
                "time": datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol,
                "action": "THU_HOACH_LUOI",
                "old_anchor": anchor,
                "new_anchor": curr_price,
                "steps": steps_moved,
                "profit": net_profit,
                "balance_after": state["balance"]
            }
            state.setdefault("history_trades", []).append(trade_log)
            if len(state["history_trades"]) > 100:
                state["history_trades"].pop(0)
            save_state()

            action_type = "📈 BÁN CHỐT LỜI TẦNG CAO" if steps_moved > 0 else "📉 GOM ĐÁY & CHỐT LỜI NHỊP HỒI"
            
            msg = (
                f"🌾 <b>[OKX DYNAMIC GRID - THU HOẠCH LÃI LƯỚI]</b>\n\n"
                f"🪙 <b>Cặp Coin:</b> {symbol}\n"
                f"⚡ <b>Hành động:</b> {action_type} ({abs(steps_moved)} tầng)\n"
                f"💵 <b>Giá cũ:</b> ${anchor:.4f} ──► <b>Giá mới:</b> ${curr_price:.4f}\n"
                f"💰 <b>Lãi ròng tầng này:</b> 🟢 +{net_profit:.4f} USDT\n"
                f"💸 <b>Phí sàn đã trừ:</b> -{fee:.4f} USDT\n"
                f"─────────────────────────\n"
                f"📈 <b>Tổng lãi lưới tích lũy:</b> 🟢 +{state['total_grid_profit']:.4f} USDT ({state['total_trades']} lần chốt)\n"
                f"🏦 <b>Số dư tài khoản:</b> <b>{state['balance']:.2f} USDT</b>"
            )
            logger.info(f"🌾 Chốt lời lưới {symbol}: +{net_profit:.4f} USDT | Số dư: {state['balance']:.2f} USDT")
            send_telegram_message(msg)

    except Exception as e:
        logger.error(f"🔴 Lỗi xử lý grid cho {symbol}: {e}")

async def send_periodic_recap():
    now_vn = datetime.now(timezone(timedelta(hours=7)))
    if now_vn.hour in [7, 15, 23] and (time.time() - state.get("last_report_time", 0)) > 3000:
        state["last_report_time"] = time.time()
        save_state()
        
        msg = (
            f"📊 <b>[OKX DYNAMIC GRID - BÁO CÁO ĐỊNH KỲ]</b>\n\n"
            f"⏰ <b>Thời gian:</b> {now_vn.strftime('%H:%M %d/%m/%Y')} (VN)\n"
            f"💰 <b>Vốn khởi tạo:</b> $100.00 USDT\n"
            f"🏦 <b>Số dư hiện tại:</b> <b>{state['balance']:.2f} USDT</b>\n"
            f"📈 <b>Tổng lãi tích lũy:</b> 🟢 <b>+{state['total_grid_profit']:.4f} USDT</b> ({(state['balance']-INITIAL_BALANCE):+.2f}%)\n"
            f"⚡ <b>Tổng số vòng chốt lời:</b> {state['total_trades']} lần\n"
            f"💸 <b>Tổng phí sàn OKX:</b> -{state['total_fees_paid']:.4f} USDT\n"
            f"─────────────────────────\n"
            f"🛡️ <i>Chiến lược Lưới Động vận hành 100% tự động, biến sóng Sideway thành dòng tiền!</i>"
        )
        send_telegram_message(msg)

async def main():
    load_state()
    start_health_check_server()
    
    start_msg = (
        f"🚀 <b>[OKX DYNAMIC GRID HARVESTER ĐÃ KHỞI ĐỘNG]</b>\n\n"
        f"💰 <b>Vốn khởi tạo:</b> ${INITIAL_BALANCE:.2f} USDT\n"
        f"🎯 <b>Chiến lược:</b> Lưới Biến Động Vi Mô (Dynamic ATR Grid Scalping)\n"
        f"📊 <b>Danh mục theo dõi:</b> {', '.join(TRADING_PAIRS)}\n"
        f"📏 <b>Khoảng cách lưới:</b> 0.80% / tầng vi mô\n"
        f"🛡️ <b>Mục tiêu:</b> Biến sóng Sideway giằng co thành dòng tiền lãi tươi đều đặn!\n\n"
        f"🤖 <i>Bot đã kết nối OKX Public API & sẵn sàng thu hoạch!</i>"
    )
    send_telegram_message(start_msg)
    
    while True:
        try:
            for symbol in TRADING_PAIRS:
                await process_grid_for_symbol(symbol)
                await asyncio.sleep(1)
            await send_periodic_recap()
        except Exception as e:
            logger.error(f"🔴 Lỗi vòng lặp chính: {e}")
        await asyncio.sleep(15)

if __name__ == "__main__":
    asyncio.run(main())
