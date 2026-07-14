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
logger = logging.getLogger("okx-paper-pullback-bot")

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

# Kiểm tra cấu hình bắt buộc
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("🔴 Lỗi: Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong tệp .env!")
    sys.exit(1)

if not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
    logger.error("🔴 Lỗi: Chưa cấu hình đầy đủ OKX_API_KEY, OKX_SECRET_KEY hoặc OKX_PASSPHRASE!")
    sys.exit(1)

# Danh sách sản phẩm Hợp đồng Vĩnh cửu (Perpetual Swap) ký quỹ bằng USDT
SYMBOLS = ["TRX-USDT-SWAP", "XRP-USDT-SWAP", "LTC-USDT-SWAP", "SHIB-USDT-SWAP", "DOGE-USDT-SWAP", "ARB-USDT-SWAP", "SOL-USDT-SWAP"]
INTERVAL = "15m"                            # Khung thời gian quét chính
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "okx_paper_pullback_portfolio.json")

# Quản lý rủi ro giả lập
RISK_PERCENT = 2.0                          # Rủi ro 2% tài khoản mỗi lệnh
INITIAL_BALANCE = 100.0                     # Vốn giả lập ban đầu 100 USDT
SL_PCT = 0.008                              # Cắt lỗ cứng 0.8%
TP_PCT = 0.012                              # Chốt lời cứng 1.2% (R:R = 1.5)

# Khởi tạo API sàn OKX ở chế độ THẬT (Nhưng chỉ dùng để ĐỌC dữ liệu)
exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET_KEY,
    'password': OKX_PASSPHRASE,
    'enableRateLimit': True,
})
exchange.set_sandbox_mode(False) 

# ==========================================================
# KHỞI TẠO VÀ QUẢN LÝ DANH MỤC GIẢ LẬP (PORTFOLIO)
# ==========================================================
portfolio = {}

def save_portfolio():
    try:
        # Tạo thư mục data nếu chưa có
        os.makedirs(os.path.dirname(PORTFOLIO_FILE), exist_ok=True)
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=4, ensure_ascii=False)
        logger.debug("💾 Đã cập nhật danh mục giả lập Pullback.")
    except Exception as e:
        logger.error(f"🔴 Lỗi ghi file portfolio JSON: {e}")

def init_new_portfolio():
    global portfolio
    portfolio = {
        "balance": INITIAL_BALANCE,
        "positions": {},
        "trades_history": []
    }
    for sym in SYMBOLS:
        portfolio["positions"][sym] = None
    save_portfolio()
    logger.info(f"✨ Khởi tạo danh mục giả lập Pullback mới với số dư: {INITIAL_BALANCE} USDT")

def load_portfolio():
    global portfolio
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
            # Đồng bộ các symbol nếu danh sách đổi
            for sym in SYMBOLS:
                if sym not in portfolio["positions"]:
                    portfolio["positions"][sym] = None
            logger.info(f"💾 Đã nạp danh mục giả lập Pullback. Số dư hiện tại: {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT")
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
# PHÂN TÍCH KỸ THUẬT (INDICATORS)
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

def calculate_rsi(prices: list, length: int = 14) -> list:
    n = len(prices)
    rsi = [50.0] * n
    if n <= length: return rsi
    deltas = [prices[i] - prices[i-1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    
    if avg_loss > 0:
        rs = avg_gain / avg_loss
        rsi[length] = 100.0 - (100.0 / (1.0 + rs))
    else:
        rsi[length] = 100.0
        
    for i in range(length + 1, n):
        gain = gains[i-1]
        loss = losses[i-1]
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
        else:
            rsi[i] = 100.0
    return rsi

def calculate_bb(prices: list, n: int = 20, std_mult: float = 2.0) -> tuple:
    l = len(prices)
    basis = [0.0]*l
    upper = [0.0]*l
    lower = [0.0]*l
    if l < n: return basis, upper, lower
    
    for i in range(n-1, l):
        window = prices[i-n+1:i+1]
        basis[i] = sum(window)/n
        dev = math.sqrt(sum((x - basis[i])**2 for x in window)/n)
        upper[i] = basis[i] + std_mult * dev
        lower[i] = basis[i] - std_mult * dev
    return basis, upper, lower

# ==========================================================
# THAO TÁC GIAO DỊCH GIẢ LẬP
# ==========================================================
def calculate_contracts(symbol: str, price: float) -> int:
    """Tính toán số hợp đồng dựa trên rủi ro 2% tài khoản với Stop Loss cố định 0.8%"""
    try:
        balance = portfolio.get("balance", INITIAL_BALANCE)
        risk_amount = balance * (RISK_PERCENT / 100.0)
        
        market = exchange.market(symbol)
        contract_size = market['contractSize']
        
        # SL cố định cách Entry 0.8% -> Khoảng cách giá SL là price * 0.008
        sl_distance = price * SL_PCT
        
        # Formula: Contracts = Risk_Amount / (SL_Distance * Contract_Size)
        contracts = risk_amount / (sl_distance * contract_size)
        
        # Áp dụng độ chính xác của sàn
        lot_size = market.get('lotSize', 1.0)
        precision = 0
        if lot_size < 1:
            precision = int(-math.log10(lot_size))
            contracts = round(contracts, precision)
        else:
            contracts = int(contracts - (contracts % lot_size))
            
        return max(1, contracts)
    except Exception as e:
        logger.error(f"🔴 Lỗi tính toán khối lượng hợp đồng cho {symbol}: {e}")
        return 1

def open_simulated_position(symbol: str, order_type: str, entry: float, sl: float, tp: float, contracts: int):
    """Mở vị thế mô phỏng mới"""
    portfolio["positions"][symbol] = {
        "type": order_type,
        "entry_price": entry,
        "sl": sl,
        "tp": tp,
        "contracts": contracts,
        "entry_time": int(time.time() * 1000) # Lưu dạng ms để đồng bộ
    }
    save_portfolio()
    
    emoji = "🟢" if order_type == "LONG" else "🔴"
    try:
        market = exchange.market(symbol)
        contract_size = market['contractSize']
    except Exception:
        contract_size = 0.01
        
    risk_amount = abs(entry - sl) * contracts * contract_size
    profit_amount = abs(tp - entry) * contracts * contract_size
    
    msg = (
        f"{emoji} <b>[MÔ PHỎNG PULLBACK - MỞ LỆNH] {order_type} {symbol} ({INTERVAL})</b>\n\n"
        f"🎟️ <b>Khối lượng:</b> {contracts} Hợp đồng\n"
        f"👉 <b>Giá vào lệnh:</b> {entry:.4f}\n"
        f"🛡️ <b>Stop Loss (0.8%):</b> {sl:.4f} (Rủi ro: -{risk_amount:.2f} USDT)\n"
        f"🎯 <b>Take Profit (1.2%):</b> {tp:.4f} (Lợi nhuận mục tiêu: +{profit_amount:.2f} USDT)\n\n"
        f"📊 <b>Số dư tài khoản mô phỏng:</b> {portfolio['balance']:.2f} USDT"
    )
    send_telegram_message(msg)

def close_simulated_trade(symbol: str, order_type: str, entry: float, exit_price: float, contracts: int, profit: float, reason: str):
    """Xử lý kết thúc vị thế mô phỏng, ghi chép nhật ký giao dịch và báo Telegram"""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trade_record = {
        "symbol": symbol,
        "type": order_type,
        "entry_price": entry,
        "exit_price": exit_price,
        "contracts": contracts,
        "profit": round(profit, 2),
        "reason": reason,
        "time": now_str
    }
    
    portfolio["trades_history"].append(trade_record)
    portfolio["positions"][symbol] = None
    save_portfolio()
    
    emoji = "🔴" if profit < 0 else "🟢"
    action_str = "DỪNG LỖ (SL)" if reason == "STOP_LOSS" else "CHỐT LỜI (TP)"
    
    msg = (
        f"{emoji} <b>[MÔ PHỎNG PULLBACK - ĐÓNG LỆNH] {symbol} ({action_str})</b>\n\n"
        f"🎟️ <b>Loại vị thế:</b> {order_type}\n"
        f"💵 <b>Khối lượng:</b> {contracts} Hợp đồng\n"
        f"👉 <b>Entry:</b> {entry:.4f} | <b>Exit:</b> {exit_price:.4f}\n"
        f"💰 <b>Kết quả:</b> {'-' if profit < 0 else '+'}{abs(profit):.2f} USDT\n"
        f"📊 <b>Số dư tài khoản mô phỏng:</b> <b>{portfolio['balance']:.2f} USDT</b>"
    )
    send_telegram_message(msg)

def check_active_positions(symbol: str, current_candle: dict):
    """Kiểm tra giá hiện tại để quét dừng lỗ / chốt lời của vị thế đang mở"""
    pos = portfolio["positions"].get(symbol)
    if pos is None:
        return
        
    high = current_candle["high"]
    low = current_candle["low"]
    
    try:
        market = exchange.market(symbol)
        contract_size = market['contractSize']
    except Exception:
        contract_size = 0.01
        
    # --- TRƯỜNG HỢP VỊ THẾ LONG ---
    if pos["type"] == "LONG":
        # 1. Kiểm tra dừng lỗ (Stop Loss)
        if low <= pos["sl"]:
            loss = (pos["sl"] - pos["entry_price"]) * pos["contracts"] * contract_size
            portfolio["balance"] += loss
            close_simulated_trade(symbol, "LONG", pos["entry_price"], pos["sl"], pos["contracts"], loss, "STOP_LOSS")
            return
            
        # 2. Kiểm tra chốt lời (Take Profit)
        if high >= pos["tp"]:
            profit = (pos["tp"] - pos["entry_price"]) * pos["contracts"] * contract_size
            portfolio["balance"] += profit
            close_simulated_trade(symbol, "LONG", pos["entry_price"], pos["tp"], pos["contracts"], profit, "TAKE_PROFIT")
            return

    # --- TRƯỜNG HỢP VỊ THẾ SHORT ---
    elif pos["type"] == "SHORT":
        # 1. Kiểm tra dừng lỗ (Stop Loss)
        if high >= pos["sl"]:
            loss = (pos["entry_price"] - pos["sl"]) * pos["contracts"] * contract_size
            portfolio["balance"] += loss
            close_simulated_trade(symbol, "SHORT", pos["entry_price"], pos["sl"], pos["contracts"], loss, "STOP_LOSS")
            return
            
        # 2. Kiểm tra chốt lời (Take Profit)
        if low <= pos["tp"]:
            profit = (pos["entry_price"] - pos["tp"]) * pos["contracts"] * contract_size
            portfolio["balance"] += profit
            close_simulated_trade(symbol, "SHORT", pos["entry_price"], pos["tp"], pos["contracts"], profit, "TAKE_PROFIT")
            return

# ==========================================================
# KHỞI TẠO NẾN DỮ LIỆU TỪ OKX API
# ==========================================================
def fetch_okx_candles(symbol: str, timeframe: str, limit: int = 250) -> list:
    """Tải dữ liệu nến từ sàn OKX"""
    try:
        # OKX sử dụng cấu trúc UTC time
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
        logger.error(f"🔴 Lỗi tải dữ liệu nến {timeframe} cho {symbol}: {e}")
        return []

# ==========================================================
# QUÉT TÍN HIỆU CHIẾN LƯỢC PULLBACK THUẬN XU HƯỚNG LỚN
# ==========================================================
def check_signals_for_symbol(sym: str):
    candles_15m = fetch_okx_candles(sym, INTERVAL)
    if len(candles_15m) < 220:
        return
        
    current_candle = candles_15m[-1]
    
    # 1. Kiểm tra quét TP/SL của vị thế đang mở (dựa trên High/Low nến hiện tại)
    check_active_positions(sym, current_candle)
    
    pos = portfolio["positions"].get(sym)
    last_signal_time = pos["entry_time"] if pos else 0
    
    last_closed_candle = candles_15m[-2]
    idx = len(candles_15m) - 2
    
    closes_15m = [c["close"] for c in candles_15m]
    
    # Tính toán chỉ báo Bollinger Bands, RSI và EMA 200 trên nến 15m
    basis, upper, lower = calculate_bb(closes_15m, 20, 2.0)
    rsi = calculate_rsi(closes_15m, 14)
    ema200 = calculate_ema(closes_15m, 200)
    
    close_price = last_closed_candle["close"]
    rsi_val = rsi[idx]
    ema_val = ema200[idx]
    lower_band = lower[idx]
    upper_band = upper[idx]
    
    # Nếu đã xử lý nến này rồi thì bỏ qua
    if last_closed_candle["time"] <= last_signal_time:
        return
        
    # 2. Kiểm tra tín hiệu mở vị thế mới (khi chưa có vị thế mở cho coin này)
    if pos is None:
        # XU HƯỚNG TĂNG: Giá nằm trên EMA 200 -> Chỉ canh Mua rải (LONG) khi pullback
        if close_price > ema_val:
            if close_price < lower_band and rsi_val <= 30:
                sl = close_price * (1.0 - SL_PCT)
                tp = close_price * (1.0 + TP_PCT)
                contracts = calculate_contracts(sym, close_price)
                
                open_simulated_position(sym, "LONG", close_price, sl, tp, contracts)
                
        # XU HƯỚNG GIẢM: Giá nằm dưới EMA 200 -> Chỉ canh Bán rải (SHORT) khi pullback
        elif close_price < ema_val:
            if close_price > upper_band and rsi_val >= 70:
                sl = close_price * (1.0 + SL_PCT)
                tp = close_price * (1.0 - TP_PCT)
                contracts = calculate_contracts(sym, close_price)
                
                open_simulated_position(sym, "SHORT", close_price, sl, tp, contracts)

# ==========================================================
# KHỞI CHẠY MÁY CHỦ HEALTH CHECK SERVER
# ==========================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        balance_str = f"OK - Pullback Paper Balance: {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT"
        self.wfile.write(balance_str.encode('utf-8'))
        
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        
    def log_message(self, format, *args):
        return

def start_health_server():
    # Sử dụng cổng 10002 để tránh trùng với bot chính (10000) và paper bot v5 (10001)
    port = int(os.environ.get("PORT_PULLBACK", 10002))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"🌐 Đã khởi chạy máy chủ Health Check cho Pullback Bot trên cổng {port}.")
    server.serve_forever()

# ==========================================================
# KHỞI ĐỘNG HỆ THỐNG
# ==========================================================
if __name__ == "__main__":
    logger.info("🚀 Khởi động Bot OKX Paper Pullback (Đọc API thật - Thuận xu hướng lớn)...")
    
    # Đồng bộ hóa thị trường sàn OKX để lấy thông tin lotSize, contractSize
    try:
        exchange.load_markets()
        logger.info("✅ Đã kết nối và nạp danh sách sản phẩm sàn OKX.")
    except Exception as e:
        logger.error(f"🔴 Không thể nạp thị trường OKX: {e}")
        sys.exit(1)
        
    load_portfolio()
    
    # Khởi chạy server kiểm tra sức khỏe chạy nền
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    
    # Gửi thông báo khởi động lên Telegram
    send_telegram_message(
        f"🚀 <b>BOT MÔ PHỎNG OKX PULLBACK KHỞI CHẠY THÀNH CÔNG!</b>\n\n"
        f"📈 <b>Cấu hình chiến thuật:</b>\n"
        f"- Bollinger Bands + RSI thuận EMA 200 15m\n"
        f"- Cắt lỗ SL: 0.8% | Chốt lời TP: 1.2% (R:R = 1.5)\n"
        f"- Danh mục quét: 7 coins\n"
        f"💵 <b>Vốn ban đầu:</b> {portfolio.get('balance', INITIAL_BALANCE):.2f} USDT"
    )
    
    # Vòng lặp chính quét tín hiệu mỗi 30 giây
    while True:
        try:
            logger.info("🔍 Bắt đầu chu kỳ quét tín hiệu Pullback...")
            for symbol in SYMBOLS:
                check_signals_for_symbol(symbol)
                time.sleep(1) # Tránh rate limit của sàn
            logger.info("⏳ Chu kỳ quét hoàn thành. Chờ 30 giây...")
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("⏹️ Đang tắt bot...")
            sys.exit(0)
        except Exception as e:
            logger.error(f"🔴 Lỗi trong vòng lặp chính của Bot: {e}")
            time.sleep(15)
