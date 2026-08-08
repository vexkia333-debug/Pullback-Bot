import ccxt
import urllib.request
import json
import time
import datetime
import math
import logging
import sys
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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
logger = logging.getLogger("okx-paper-ema-macd-bot")

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

OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("🔴 Lỗi: Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong tệp .env!")
    sys.exit(1)

if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
    logger.error("🔴 Lỗi: Chưa cấu hình đầy đủ OKX_API_KEY, OKX_SECRET_KEY hoặc OKX_PASSPHRASE!")
    sys.exit(1)

# Đã lọc bỏ 2 coin nhiễu ETH & SOL, giữ lại 6 coin hiệu suất cao nhất
SYMBOLS = [
    "LINK-USDT-SWAP", "TRX-USDT-SWAP", "XRP-USDT-SWAP", 
    "AVAX-USDT-SWAP", "DOGE-USDT-SWAP", "ARB-USDT-SWAP"
]
INTERVAL = "15m"
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "okx_ema_macd_portfolio.json")

# Quản trị rủi ro & Tham số chiến thuật EMA 10/30 Crossover + MACD Filter
RISK_PERCENT = 2.0                          # Rủi ro 2% tài khoản mỗi lệnh
INITIAL_BALANCE = 100.0                     # Vốn giả lập ban đầu 100 USDT
SL_PCT = 0.007                              # Cắt lỗ cố định 0.7% (Đã tối ưu né bẫy giật râu 15m)
TP1_RATIO = 1.5                             # TP1 = 1.5R (1.05%)
TP2_RATIO = 3.0                             # TP2 = 3.0R (2.10%)
TAKER_FEE_RATE = 0.0005                     # Phí sàn OKX Taker 0.05% mỗi chiều

exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET_KEY,
    'password': OKX_PASSPHRASE,
    'enableRateLimit': True,
})
exchange.set_sandbox_mode(False) 

def format_price(symbol, price):
    try:
        market = exchange.market(symbol)
        precision = market['precision']['price']
        if isinstance(precision, int):
            return f"{price:.{precision}f}"
        else:
            return f"{price:.8f}".rstrip('0').rstrip('.')
    except Exception:
        return f"{price:.6f}"

portfolio = {}

def save_portfolio():
    try:
        os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=4, ensure_ascii=False)
        logger.debug("💾 Đã cập nhật danh mục giả lập EMA 10/30.")
    except Exception as e:
        logger.error(f"🔴 Lỗi ghi file portfolio JSON: {e}")

def init_new_portfolio():
    global portfolio
    portfolio = {
        "balance": INITIAL_BALANCE,
        "total_fees_paid": 0.0,
        "positions": {},
        "last_signal_times": {},
        "trades_history": []
    }
    for sym in SYMBOLS:
        portfolio["positions"][sym] = None
        portfolio["last_signal_times"][sym] = 0
    save_portfolio()
    logger.info(f"✨ Khởi tạo danh mục giả lập EMA 10/30 mới với số dư: {INITIAL_BALANCE} USDT")

def load_portfolio():
    global portfolio
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
            if "total_fees_paid" not in portfolio:
                portfolio["total_fees_paid"] = 0.0
            if "last_signal_times" not in portfolio:
                portfolio["last_signal_times"] = {}
            for sym in SYMBOLS:
                if sym not in portfolio["positions"]:
                    portfolio["positions"][sym] = None
                if sym not in portfolio["last_signal_times"]:
                    portfolio["last_signal_times"][sym] = 0
            logger.info(f"💾 Đã nạp danh mục giả lập EMA 10/30. Số dư hiện tại: {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT")
        except Exception as e:
            logger.error(f"🔴 Lỗi đọc file portfolio JSON: {e}")
            init_new_portfolio()
    else:
        init_new_portfolio()

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        req = urllib.request.Request(
            url, 
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode())
            if res_data.get("ok"):
                logger.info("🟢 Đã gửi tin nhắn báo cáo tới Telegram!")
                return res_data.get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"🔴 Lỗi gửi tin nhắn Telegram: {e}")
    return None

# ==========================================================
# INDICATORS
# ==========================================================
def calculate_ema(prices: list, length: int) -> list:
    ema = [0.0] * len(prices)
    if len(prices) < length: return ema
    sma = sum(prices[:length]) / length
    ema[length - 1] = sma
    alpha = 2.0 / (length + 1)
    for i in range(length, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1 - alpha)
    return ema

def calculate_vol_ma(volumes: list, length: int = 20) -> list:
    ma = [0.0] * len(volumes)
    if len(volumes) < length: return ma
    for i in range(length - 1, len(volumes)):
        ma[i] = sum(volumes[i - length + 1 : i + 1]) / length
    return ma

def calculate_macd(prices: list, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = calculate_ema(macd_line, signal)
    histogram = [m - sig for m, sig in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram

# ==========================================================
# TRADING OPERATIONS WITH REAL EXCHANGE FEE DEDUCTION
# ==========================================================
def calculate_contracts(symbol: str, entry: float, sl: float) -> int:
    try:
        balance = portfolio.get("balance", INITIAL_BALANCE)
        risk_amount = balance * (RISK_PERCENT / 100.0)
        market = exchange.market(symbol)
        contract_size = market['contractSize']
        sl_distance = abs(entry - sl)
        if sl_distance == 0: return 1
        contracts = risk_amount / (sl_distance * contract_size)
        lot_size = market.get('lotSize', 1.0)
        if lot_size < 1:
            precision = int(-math.log10(lot_size))
            contracts = round(contracts, precision)
        else:
            contracts = int(contracts - (contracts % lot_size))
        return max(1, contracts)
    except Exception as e:
        logger.error(f"🔴 Lỗi tính số hợp đồng cho {symbol}: {e}")
        return 1

def open_simulated_position(symbol: str, order_type: str, entry: float, sl: float, tp1: float, tp2: float, contracts: int):
    try:
        market = exchange.market(symbol)
        contract_size = market['contractSize']
    except Exception:
        contract_size = 0.01

    entry_val = entry * contracts * contract_size
    entry_fee = entry_val * TAKER_FEE_RATE
    
    # Trừ phí mở lệnh ngay vào tài khoản
    portfolio["balance"] -= entry_fee
    portfolio["total_fees_paid"] += entry_fee

    portfolio["positions"][symbol] = {
        "type": order_type,
        "entry_price": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "contracts": contracts,
        "remaining_contracts": contracts,
        "entry_val": entry_val,
        "entry_fee": entry_fee,
        "is_tp1_hit": False,
        "entry_time": int(time.time() * 1000)
    }
    save_portfolio()
    
    emoji = "🟢" if order_type == "LONG" else "🔴"
    risk_amount = abs(entry - sl) * contracts * contract_size
    sl_dist_pct = (abs(entry - sl) / entry) * 100.0
    
    msg = (
        f"{emoji} <b>[EMA 10/30 - MỞ LỆNH] {order_type} {symbol} ({INTERVAL})</b>\n\n"
        f"🎟️ <b>Khối lượng:</b> {contracts} Hợp đồng\n"
        f"👉 <b>Giá vào lệnh (EMA Cross + MACD):</b> {format_price(symbol, entry)}\n"
        f"🛡️ <b>Stop Loss ({sl_dist_pct:.2f}%):</b> {format_price(symbol, sl)} (Rủi ro: -{risk_amount:.2f} USDT)\n"
        f"🎯 <b>TP1 (1.5R):</b> {format_price(symbol, tp1)} | <b>TP2 (3.0R):</b> {format_price(symbol, tp2)}\n"
        f"💸 <b>Phí mở lệnh OKX (0.05% Taker):</b> -{entry_fee:.4f} USDT\n\n"
        f"📊 <b>Số dư sau trừ phí:</b> <b>{portfolio['balance']:.2f} USDT</b>"
    )
    send_telegram_message(msg)

def close_simulated_trade(symbol: str, order_type: str, entry: float, exit_price: float, contracts: int, gross_pnl: float, exit_fee: float, reason: str):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    net_pnl = gross_pnl - exit_fee
    
    trade_record = {
        "symbol": symbol,
        "type": order_type,
        "entry_price": entry,
        "exit_price": exit_price,
        "contracts": contracts,
        "gross_pnl": round(gross_pnl, 4),
        "exit_fee": round(exit_fee, 4),
        "net_pnl": round(net_pnl, 4),
        "reason": reason,
        "time": now_str
    }
    
    portfolio["trades_history"].append(trade_record)
    portfolio["positions"][symbol] = None
    save_portfolio()
    
    emoji = "🔴" if net_pnl < 0 else "🟢"
    if reason == "STOP_LOSS":
        action_str = "DỪNG LỖ (SL Ban Đầu)"
    elif reason == "SL_BE":
        action_str = "HÒA VỐN (SL về Entry)"
    elif reason == "TAKE_PROFIT_FULL":
        action_str = "CHỐT LỜI CẢ 2 TP (FULL TP2)"
    else:
        action_str = reason
    
    msg = (
        f"{emoji} <b>[EMA 10/30 - ĐÓNG LỆNH] {symbol} ({action_str})</b>\n\n"
        f"🎟️ <b>Loại vị thế:</b> {order_type} ({contracts} contracts)\n"
        f"👉 <b>Entry:</b> {format_price(symbol, entry)} | <b>Exit:</b> {format_price(symbol, exit_price)}\n"
        f"💵 <b>Lợi nhuận gộp:</b> {gross_pnl:+.4f} USDT\n"
        f"💸 <b>Phí đóng lệnh OKX (0.05%):</b> -{exit_fee:.4f} USDT\n"
        f"💰 <b>LỢI NHUẬN RÒNG THỰC TẾ:</b> <b>{net_pnl:+.4f} USDT</b>\n\n"
        f"📊 <b>Số dư tài khoản:</b> <b>{portfolio['balance']:.2f} USDT</b>\n"
        f"💸 <b>Tổng phí sàn tích lũy:</b> {portfolio['total_fees_paid']:.4f} USDT"
    )
    send_telegram_message(msg)

def check_active_positions(symbol: str, current_candle: dict):
    pos = portfolio["positions"].get(symbol)
    if pos is None: return
        
    high = current_candle["high"]
    low = current_candle["low"]
    
    try:
        market = exchange.market(symbol)
        contract_size = market['contractSize']
    except Exception:
        contract_size = 0.01

    contracts = pos["remaining_contracts"]
    
    # LONG POSITION
    if pos["type"] == "LONG":
        if not pos["is_tp1_hit"] and high >= pos["tp1"]:
            tp1_contracts = max(1, contracts // 2)
            pos["remaining_contracts"] -= tp1_contracts
            pos["is_tp1_hit"] = True
            pos["sl"] = pos["entry_price"]
            
            exit_val = pos["tp1"] * tp1_contracts * contract_size
            exit_fee = exit_val * TAKER_FEE_RATE
            gross_pnl = (pos["tp1"] - pos["entry_price"]) * tp1_contracts * contract_size
            net_pnl = gross_pnl - exit_fee
            
            portfolio["balance"] += net_pnl
            portfolio["total_fees_paid"] += exit_fee
            save_portfolio()
            
            send_telegram_message(
                f"🎯 <b>[EMA 10/30 - KHỚP TP1 50%] {symbol}</b>\n\n"
                f"👉 <b>Khớp TP1 giá:</b> {format_price(symbol, pos['tp1'])}\n"
                f"💰 <b>Lãi ròng TP1:</b> +{net_pnl:.4f} USDT (Đã trừ phí sàn -{exit_fee:.4f} USDT)\n"
                f"🛡️ <b>ĐÃ TỰ ĐỘNG DỜI STOP LOSS VỀ ENTRY:</b> {format_price(symbol, pos['entry_price'])}\n"
                f"📊 <b>Số dư tài khoản:</b> {portfolio['balance']:.2f} USDT"
            )
            return

        if pos["is_tp1_hit"]:
            if low <= pos["sl"]:
                rem = pos["remaining_contracts"]
                exit_val = pos["sl"] * rem * contract_size
                exit_fee = exit_val * TAKER_FEE_RATE
                gross_pnl = (pos["sl"] - pos["entry_price"]) * rem * contract_size
                portfolio["balance"] += (gross_pnl - exit_fee)
                portfolio["total_fees_paid"] += exit_fee
                close_simulated_trade(symbol, "LONG", pos["entry_price"], pos["sl"], rem, gross_pnl, exit_fee, "SL_BE")
            elif high >= pos["tp2"]:
                rem = pos["remaining_contracts"]
                exit_val = pos["tp2"] * rem * contract_size
                exit_fee = exit_val * TAKER_FEE_RATE
                gross_pnl = (pos["tp2"] - pos["entry_price"]) * rem * contract_size
                portfolio["balance"] += (gross_pnl - exit_fee)
                portfolio["total_fees_paid"] += exit_fee
                close_simulated_trade(symbol, "LONG", pos["entry_price"], pos["tp2"], rem, gross_pnl, exit_fee, "TAKE_PROFIT_FULL")
        else:
            if low <= pos["sl"]:
                rem = pos["remaining_contracts"]
                exit_val = pos["sl"] * rem * contract_size
                exit_fee = exit_val * TAKER_FEE_RATE
                gross_pnl = (pos["sl"] - pos["entry_price"]) * rem * contract_size
                portfolio["balance"] += (gross_pnl - exit_fee)
                portfolio["total_fees_paid"] += exit_fee
                close_simulated_trade(symbol, "LONG", pos["entry_price"], pos["sl"], rem, gross_pnl, exit_fee, "STOP_LOSS")

    # SHORT POSITION
    elif pos["type"] == "SHORT":
        if not pos["is_tp1_hit"] and low <= pos["tp1"]:
            tp1_contracts = max(1, contracts // 2)
            pos["remaining_contracts"] -= tp1_contracts
            pos["is_tp1_hit"] = True
            pos["sl"] = pos["entry_price"]
            
            exit_val = pos["tp1"] * tp1_contracts * contract_size
            exit_fee = exit_val * TAKER_FEE_RATE
            gross_pnl = (pos["entry_price"] - pos["tp1"]) * tp1_contracts * contract_size
            net_pnl = gross_pnl - exit_fee
            
            portfolio["balance"] += net_pnl
            portfolio["total_fees_paid"] += exit_fee
            save_portfolio()
            
            send_telegram_message(
                f"🎯 <b>[EMA 10/30 - KHỚP TP1 50%] {symbol}</b>\n\n"
                f"👉 <b>Khớp TP1 giá:</b> {format_price(symbol, pos['tp1'])}\n"
                f"💰 <b>Lãi ròng TP1:</b> +{net_pnl:.4f} USDT (Đã trừ phí sàn -{exit_fee:.4f} USDT)\n"
                f"🛡️ <b>ĐÃ TỰ ĐỘNG DỜI STOP LOSS VỀ ENTRY:</b> {format_price(symbol, pos['entry_price'])}\n"
                f"📊 <b>Số dư tài khoản:</b> {portfolio['balance']:.2f} USDT"
            )
            return

        if pos["is_tp1_hit"]:
            if high >= pos["sl"]:
                rem = pos["remaining_contracts"]
                exit_val = pos["sl"] * rem * contract_size
                exit_fee = exit_val * TAKER_FEE_RATE
                gross_pnl = (pos["entry_price"] - pos["sl"]) * rem * contract_size
                portfolio["balance"] += (gross_pnl - exit_fee)
                portfolio["total_fees_paid"] += exit_fee
                close_simulated_trade(symbol, "SHORT", pos["entry_price"], pos["sl"], rem, gross_pnl, exit_fee, "SL_BE")
            elif low <= pos["tp2"]:
                rem = pos["remaining_contracts"]
                exit_val = pos["tp2"] * rem * contract_size
                exit_fee = exit_val * TAKER_FEE_RATE
                gross_pnl = (pos["entry_price"] - pos["tp2"]) * rem * contract_size
                portfolio["balance"] += (gross_pnl - exit_fee)
                portfolio["total_fees_paid"] += exit_fee
                close_simulated_trade(symbol, "SHORT", pos["entry_price"], pos["tp2"], rem, gross_pnl, exit_fee, "TAKE_PROFIT_FULL")
        else:
            if high >= pos["sl"]:
                rem = pos["remaining_contracts"]
                exit_val = pos["sl"] * rem * contract_size
                exit_fee = exit_val * TAKER_FEE_RATE
                gross_pnl = (pos["entry_price"] - pos["sl"]) * rem * contract_size
                portfolio["balance"] += (gross_pnl - exit_fee)
                portfolio["total_fees_paid"] += exit_fee
                close_simulated_trade(symbol, "SHORT", pos["entry_price"], pos["sl"], rem, gross_pnl, exit_fee, "STOP_LOSS")

def fetch_okx_candles(symbol: str, timeframe: str, limit: int = 250) -> list:
    try:
        raw_candles = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        candles = []
        for c in raw_candles:
            candles.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5])
            })
        return candles
    except Exception as e:
        logger.error(f"🔴 Lỗi tải nến {timeframe} cho {symbol}: {e}")
        return []

def check_signals_for_symbol(sym: str):
    candles_15m = fetch_okx_candles(sym, INTERVAL)
    if len(candles_15m) < 220: return
        
    current_candle = candles_15m[-1]
    check_active_positions(sym, current_candle)
    
    pos = portfolio["positions"].get(sym)
    last_signal_time = portfolio.get("last_signal_times", {}).get(sym, 0)
    last_closed_candle = candles_15m[-2]
    
    if last_closed_candle["time"] <= last_signal_time: return
        
    closes = [c["close"] for c in candles_15m]
    vols = [c["volume"] for c in candles_15m]
    
    ema10 = calculate_ema(closes, 10)
    ema30 = calculate_ema(closes, 30)
    vol_ma = calculate_vol_ma(vols, 20)
    macd_line, signal_line, histogram = calculate_macd(closes, 12, 26, 9)
    
    idx = len(candles_15m) - 2
    c_last = last_closed_candle
    
    # -------------------------------------------------------------
    # LOGIC GIAO CẮT EMA 10/30 + VOLUME BREAKOUT + MACD FILTER
    # -------------------------------------------------------------
    if pos is None:
        # LONG: EMA 10 cắt lên EMA 30 + Volume > 1.3x Vol MA + MACD Histogram > 0
        is_long = (ema10[idx] > ema30[idx]) and (ema10[idx-1] <= ema30[idx-1]) and (c_last["volume"] > 1.3 * vol_ma[idx]) and (histogram[idx] > 0)
        
        # SHORT: EMA 10 cắt xuống EMA 30 + Volume > 1.3x Vol MA + MACD Histogram < 0
        is_short = (ema10[idx] < ema30[idx]) and (ema10[idx-1] >= ema30[idx-1]) and (c_last["volume"] > 1.3 * vol_ma[idx]) and (histogram[idx] < 0)
        
        close_price = c_last["close"]
        
        if is_long:
            sl_dist = close_price * SL_PCT # Stop Loss cố định 0.7% né bẫy râu 15m
            sl = close_price - sl_dist
            tp1 = close_price + sl_dist * TP1_RATIO
            tp2 = close_price + sl_dist * TP2_RATIO
            contracts = calculate_contracts(sym, close_price, sl)
            
            portfolio["last_signal_times"][sym] = last_closed_candle["time"]
            open_simulated_position(sym, "LONG", close_price, sl, tp1, tp2, contracts)
                
        elif is_short:
            sl_dist = close_price * SL_PCT
            sl = close_price + sl_dist
            tp1 = close_price - sl_dist * TP1_RATIO
            tp2 = close_price - sl_dist * TP2_RATIO
            contracts = calculate_contracts(sym, close_price, sl)
            
            portfolio["last_signal_times"][sym] = last_closed_candle["time"]
            open_simulated_position(sym, "SHORT", close_price, sl, tp1, tp2, contracts)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        balance_str = f"OK - EMA 10/30 Paper Balance: {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT | Total Fees Paid: {portfolio.get('total_fees_paid', 0.0):.4f} USDT"
        self.wfile.write(balance_str.encode('utf-8'))
        
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", os.environ.get("PORT_EMA", 10004)))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"🌐 Máy chủ Health Check EMA 10/30 chạy trên cổng {port}.")
    server.serve_forever()

if __name__ == "__main__":
    logger.info("🚀 Khởi động Bot OKX Paper EMA 10/30 Crossover + MACD Filter (6 Coin tuyển chọn)...")
    try:
        exchange.load_markets()
        logger.info("✅ Đã kết nối OKX API.")
    except Exception as e:
        logger.error(f"🔴 Không thể kết nối OKX: {e}")
        sys.exit(1)
        
    init_new_portfolio()
    
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    
    send_telegram_message(
        f"🚀 <b>BOT MÔ PHỎNG EMA 10/30 + MACD FILTER (6 COIN TUYỂN CHỌN) KHỞI CHẠY!</b>\n\n"
        f"📈 <b>Cấu hình chiến thuật nâng cấp:</b>\n"
        f"- Giao cắt EMA 10/30 + Khối lượng Vol > 1.3x Vol MA20 + Động lượng MACD Histogram\n"
        f"- Danh mục 6 coin tuyển chọn: LINK, TRX, XRP, AVAX, DOGE, ARB (Loại bỏ ETH & SOL nhiễu)\n"
        f"- Cắt lỗ SL chuẩn hóa 0.7% | TP1: 1.5R (50%) | TP2: 3.0R (50%)\n"
        f"- <b>TỰ ĐỘNG TRỪ PHÍ SÀN OKX THẬT:</b> 0.05% Taker mở/đóng\n"
        f"💵 <b>Vốn khởi tạo:</b> {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT"
    )
    
    while True:
        try:
            for symbol in SYMBOLS:
                check_signals_for_symbol(symbol)
                time.sleep(1)
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("⏹️ Đang tắt bot...")
            sys.exit(0)
        except Exception as e:
            logger.error(f"🔴 Lỗi trong vòng lặp chính: {e}")
            time.sleep(15)
