import asyncio
import os
import sys
import json
import time
import re
import math
import logging
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import random
import requests

# Khắc phục hiển thị tiếng Việt trên Windows Console
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Khắc phục lỗi aiohttp DNS resolver trên Windows (Python 3.14)
import aiohttp
try:
    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
except Exception:
    pass

import imageio_ffmpeg
import yt_dlp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("douyin-funny-drive-bot")

# Tải cấu hình từ .env nếu có
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
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
SERVICE_ACCOUNT_FILE = os.environ.get("GDRIVE_SERVICE_ACCOUNT_FILE", "service_account.json")
AUTO_SCOUT_INTERVAL_HOURS = float(os.environ.get("AUTO_SCOUT_INTERVAL_HOURS", 6))  # Mặc định cứ 6 tiếng tự quét 1 lần
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "douyin_funny_output")
PROCESSED_DB_PATH = os.path.join(DATA_DIR, "processed_videos.json")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Cơ sở dữ liệu theo dõi video đã xử lý (để không bao giờ trùng lặp)
def load_processed_db():
    if os.path.exists(PROCESSED_DB_PATH):
        try:
            with open(PROCESSED_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"videos": []}
    return {"videos": []}

def save_processed_db(db):
    try:
        with open(PROCESSED_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"🔴 Lỗi lưu processed DB: {e}")

# Google Drive API Client
gdrive_service = None
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2 import service_account
    
    sa_json_raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    sa_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", SERVICE_ACCOUNT_FILE)
    
    if sa_json_raw:
        sa_info = json.loads(sa_json_raw)
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=['https://www.googleapis.com/auth/drive']
        )
        gdrive_service = build('drive', 'v3', credentials=creds)
        logger.info("✅ Đã kết nối Google Drive API thành công qua biến môi trường GDRIVE_SERVICE_ACCOUNT_JSON!")
    elif os.path.exists(sa_path):
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=['https://www.googleapis.com/auth/drive']
        )
        gdrive_service = build('drive', 'v3', credentials=creds)
        logger.info(f"✅ Đã kết nối thành công Google Drive API qua file: {sa_path}")
    else:
        logger.info(f"ℹ️ Chưa cấu hình Service Account. Video sẽ được lưu tạm tại data/douyin_funny_output/")
except Exception as e:
    logger.warning(f"⚠️ Chưa khởi tạo được Google Drive API: {e}")

def send_telegram_message(text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TELEGRAM PREVIEW]\n{text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as response:
            pass
    except Exception as e:
        logger.error(f"🔴 Lỗi gửi Telegram: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        drive_status = "ĐÃ KẾT NỐI (5TB GOOGLE DRIVE)" if gdrive_service else "CHỜ FILE service_account.json (LƯU LOCAL)"
        db = load_processed_db()
        count = len(db.get("videos", []))
        html = f"""
        <html>
            <head><title>Douyin Funny Autonomous Bot</title></head>
            <body style="font-family: Arial, sans-serif; padding: 25px; background: #0f172a; color: #f8fafc;">
                <h1 style="color: #f43f5e;">🎬 Bot Tự Động Săn Video Hài Hước & Đẩy Lên Google Drive 5TB</h1>
                <p>Trạng thái: <b style="color: #22c55e;">ĐANG TỰ ĐỘNG SĂN 24/7</b></p>
                <p>Tần suất tự quét: <b>Mỗi {AUTO_SCOUT_INTERVAL_HOURS} giờ</b></p>
                <p>Tổng số video đã săn & xử lý: <b>{count} video</b></p>
                <p>Google Drive 5TB: <b>{drive_status}</b></p>
                <p>Thư mục Drive: <code>{GDRIVE_FOLDER_ID or 'Thư mục gốc'}</code></p>
                <hr style="border-color: #334155;">
                <h3>🤖 Cơ chế hoạt động:</h3>
                <p>1. <b>100% Tự động:</b> Bot tự quét các video hài hước hot xu hướng triệu view (Douyin, shorts comedy).</p>
                <p>2. <b>Không tốn bộ nhớ:</b> Tải video sạch &rarr; Lách bản quyền &rarr; Tải thẳng lên Drive 5TB &rarr; Xóa file cục bộ.</p>
                <p>3. <b>Báo về Telegram:</b> Gửi link xem Drive kèm Caption & Hashtag tiếng Việt để bạn đăng ngay.</p>
            </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))

def start_health_check_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    logger.info(f"🌐 Health check HTTP server đang chạy trên cổng {PORT}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

def process_video_anti_detection(input_path, output_path):
    """
    Sử dụng FFmpeg để:
    1. Tăng tốc độ 1.03x (Lách so khớp âm thanh & khung hình)
    2. Zoom nhẹ 1.02x
    3. Xóa sạch metadata cũ (-map_metadata -1)
    """
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-y",
            "-i", input_path,
            "-filter_complex", "[0:v]setpts=PTS/1.03,scale=trunc(iw*1.02/2)*2:trunc(ih*1.02/2)*2[v];[0:a]atempo=1.03[a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "24",
            "-c:a", "aac",
            "-b:a", "128k",
            "-map_metadata", "-1",
            output_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        else:
            logger.warning(f"⚠️ FFmpeg filter warning: {res.stderr[-300:] if res.stderr else 'unknown'}")
            # Dự phòng: Sao chép nếu filter lỗi
            import shutil
            shutil.copy(input_path, output_path)
            return True
    except Exception as e:
        logger.error(f"🔴 Lỗi FFmpeg: {e}")
        try:
            import shutil
            shutil.copy(input_path, output_path)
            return True
        except Exception:
            return False

def download_video_file(url, out_path):
    """Tải file video trực tiếp từ CDN máy chủ TikTok/Douyin (không watermark)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
        'Referer': 'https://www.tiktok.com/'
    }
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=30)
        if r.status_code == 200:
            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk: f.write(chunk)
            return True
    except Exception as e:
        logger.error(f"🔴 Lỗi tải file video từ CDN: {e}")
    return False

def upload_to_google_drive(file_path, file_name=None):
    """Tải thẳng video lên Google Drive 5TB và cấp quyền xem công khai"""
    global gdrive_service
    if not gdrive_service:
        return None
        
    try:
        if not file_name:
            file_name = os.path.basename(file_path)
            
        file_metadata = {'name': file_name}
        if GDRIVE_FOLDER_ID:
            file_metadata['parents'] = [GDRIVE_FOLDER_ID]
            
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
        
        file = gdrive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink, webContentLink',
            supportsAllDrives=True
        ).execute()
        
        file_id = file.get('id')
        web_link = file.get('webViewLink')
        
        try:
            gdrive_service.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'},
                supportsAllDrives=True
            ).execute()
        except Exception:
            pass
            
        return web_link
    except Exception as e:
        logger.error(f"🔴 Lỗi tải lên Google Drive: {e}")
        return None

def generate_funny_vietnamese_caption(original_title):
    """Tạo tiêu đề và hashtag tiếng Việt hài hước bắt trend cho video (kết hợp Gemini AI)"""
    clean_title = re.sub(r'#\S+', '', original_title).strip()
    if not clean_title:
        clean_title = "Tiểu phẩm hài hước triệu view"
        
    # 1. Nếu có cấu hình GEMINI_API_KEY, sử dụng Gemini AI sáng tạo bài viết triệu view
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            prompt_instruction = f"""
Bạn là chuyên gia sáng tạo nội dung triệu view hàng đầu trên TikTok và Facebook Reels tại Việt Nam.
Tôi có một video ngắn hài hước với nội dung gốc: "{clean_title}"

Hãy viết caption đăng TikTok cực kỳ cuốn hút, dí dỏm theo đúng định dạng sau:
🎭 [Một câu giật tít siêu hài hước, gây cười tò mò hoặc tranh luận, dưới 15 chữ, kèm icon 🤣/😂/🙈]

📝 [1 câu bình luận dí dỏm, tếu táo theo phong cách Gen Z/mạng xã hội Việt Nam]

👉 [1 câu kêu gọi tương tác tự nhiên, ví dụ: Tag đứa bạn hay làm trò này vào, Xem đi xem lại vẫn không nhịn được cười...]

🏷️ <code>#haihuoc #cuoivobung #douyin #funny #xuhuong #videohai #haihuocvietnam #giaitri #reels #shorts</code>

Lưu ý: Chỉ trả về nội dung theo khung trên, không thêm lời chào hay giải thích gì khác.
"""
            payload = {
                "contents": [{"parts": [{"text": prompt_instruction}]}],
                "generationConfig": {"temperature": 0.9, "maxOutputTokens": 350}
            }
            r = requests.post(url, json=payload, timeout=8)
            if r.status_code == 200:
                data = r.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and parts[0].get("text"):
                        gemini_caption = parts[0].get("text").strip()
                        logger.info("✨ Đã tạo caption triệu view bằng Google Gemini AI thành công!")
                        return gemini_caption
        except Exception as e:
            logger.warning(f"⚠️ Không thể gọi Gemini API ({e}), chuyển sang kho prompt dự phòng.")

    # 2. Kho prompt dự phòng phong phú và chất lượng cao (20+ phong cách hài hước khác nhau)
    prompts = [
        "Xem đi xem lại vẫn không nhịn được cười với quả pha xử lý này! 😂",
        "Đúng là nhân tài ẩn dật, xem mà cười xỉu ngang! 🤣",
        "Cái kết bất ngờ không thể đoán trước được luôn á! 😆",
        "Pha này thì hết nước chấm, ai mà đỡ cho nổi! 🙈",
        "Đang buồn xem xong tỉnh cả ngủ, đúng là chúa hề! 😹",
        "Tình huống khó đỡ nhất quả đất, cười rớt hàm! 🤣",
        "Người bình thường không ai làm thế này cả, hề hước thật sự! 🤪",
        "Không biết nên khóc hay nên cười với pha tấu hài này nữa! 😭😂",
        "Khi bạn cố gắng tỏ ra ngầu và cái kết đi vào lòng đất! 💀",
        "Bảo sao video này triệu view, xem đoạn cuối cười ná thở! 🤣",
        "Cười ẻ với độ lầy lội của mấy thánh này! 🙈",
        "Pha xử lý mang tính sát thương cực cao cho cơ bụng! 😆",
        "Đỉnh cao của sự vụng về nhưng lại vô cùng đáng yêu! 🥰😂",
        "Xem xong tự nhiên thấy yêu đời hẳn ra, đúng là cao thủ troll! 🎭",
        "Ai bày cho quả trò này vậy trời, cười không nhặt được mồm! 😹",
        "Pha xử lý cồng kềnh nhất lịch sử nhân loại! 🤦‍♂️😂",
        "Tưởng thế nào, hóa ra cũng chỉ đến thế thôi à! 🤣",
        "Được phen cười bể bụng với các idol tóp tóp! 🎬",
        "Cuộc sống mà, đôi khi phải có những cú twist như này mới vui! ✨",
        "Xem clip này nhớ tag ngay đứa bạn thân có nết y hệt vào nhé! 🎯"
    ]
    selected_prompt = random.choice(prompts)
    hashtags = "#haihuoc #cuoivobung #videohai #xuhuong #douyin #funny #haihuocvietnam #giaitri #shorts #reels"
    
    caption = (
        f"🎭 <b>{selected_prompt}</b>\n\n"
        f"📝 <i>Nội dung:</i> {clean_title[:90]}\n\n"
        f"👉 <i>Xem đi xem lại vẫn thấy hài, tag đứa bạn lầy lội vào đây nhé!</i>\n\n"
        f"🏷️ <b>Hashtag chuẩn SEO:</b>\n<code>{hashtags}</code>"
    )
    return caption

# Danh sách từ khóa tìm kiếm video hài hước triệu view cập nhật xu hướng mới nhất
SCOUT_KEYWORDS = [
    "douyin funny clips 2026 #shorts",
    "tiểu phẩm hài douyin mới nhất #shorts",
    "clip hài hước triệu view mới nhất #shorts",
    "funny viral clips try not to laugh 2026 #shorts",
    "troll hài hước douyin cười bể bụng #shorts",
    "những pha xử lý đi vào lòng đất mới nhất #shorts",
    "funny pet animals comedy #shorts",
    "douyin comedy viral moments #shorts",
    "clip hài hước lầy lội triệu view #shorts"
]

def scout_tiktok_douyin_via_rapidapi(limit=5):
    """Sử dụng TokApi trên RapidAPI để săn video hài hước triệu view trực tiếp từ TikTok/Douyin"""
    if not RAPIDAPI_KEY:
        return []
        
    keywords = ["hai huoc douyin", "tieu pham hai", "funny comedy", "hai huoc tiktok", "chua he"]
    chosen_kw = random.choice(keywords)
    logger.info(f"🔍 [RapidAPI TokApi] Đang quét video trực tiếp từ TikTok/Douyin: '{chosen_kw}'...")
    
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "tokapi-mobile-version.p.rapidapi.com"
    }
    
    db = load_processed_db()
    processed_ids = set(db.get("videos", []))
    candidates = []
    
    # 1. Endpoint chính: /v1/search/post (tìm kiếm video theo từ khóa, sắp xếp theo Most Liked)
    url_post = "https://tokapi-mobile-version.p.rapidapi.com/v1/search/post"
    params_post = {
        "keyword": chosen_kw,
        "count": 10,
        "region": "VN",
        "sort_type": "1"  # 1: Most liked (triệu view / viral nhất)
    }
    
    aweme_list = []
    try:
        r = requests.get(url_post, headers=headers, params=params_post, timeout=15)
        if r.status_code == 200:
            data = r.json()
            aweme_list = data.get("aweme_list", []) or data.get("data", []) or data.get("items", []) or []
        else:
            logger.warning(f"⚠️ RapidAPI /v1/search/post status: {r.status_code} - {r.text[:150]}")
    except Exception as e:
        logger.error(f"🔴 Lỗi gọi RapidAPI /v1/search/post: {e}")
        
    # 2. Endpoint dự phòng: /v1/feed/recommended (khám phá video xu hướng thịnh hành)
    if not aweme_list:
        try:
            url_rec = "https://tokapi-mobile-version.p.rapidapi.com/v1/feed/recommended"
            r_rec = requests.get(url_rec, headers=headers, params={"pull_type": "0", "region": "VN", "count": 10}, timeout=15)
            if r_rec.status_code == 200:
                data_rec = r_rec.json()
                aweme_list = data_rec.get("aweme_list", []) or data_rec.get("data", []) or data_rec.get("items", []) or []
            else:
                logger.warning(f"⚠️ RapidAPI /v1/feed/recommended status: {r_rec.status_code} - {r_rec.text[:150]}")
        except Exception as e:
            logger.error(f"🔴 Lỗi gọi RapidAPI /v1/feed/recommended: {e}")
            
    # Phân tích danh sách video lấy được từ TokApi
    for item in aweme_list:
        v_id = str(item.get("aweme_id") or item.get("id") or "")
        if not v_id or v_id in processed_ids:
            continue
            
        title = item.get("desc") or "Video Hài Hước TikTok Douyin"
        play_addr = item.get("video", {}).get("play_addr", {})
        url_list = play_addr.get("url_list", [])
        if not url_list:
            download_addr = item.get("video", {}).get("download_addr", {})
            url_list = download_addr.get("url_list", [])
            
        if url_list:
            candidates.append({
                "id": v_id,
                "title": title,
                "url": url_list[0],
                "is_direct_cdn": True,
                "duration": 30
            })
            
    if candidates:
        logger.info(f"✅ [RapidAPI TokApi] Tìm thấy {len(candidates)} video TikTok/Douyin mới không watermark!")
    else:
        logger.info("ℹ️ [RapidAPI TokApi] Chưa tìm thấy video mới từ TokApi, chuyển qua kênh phụ...")
        
    return candidates

def scout_trending_funny_videos(limit=5):
    """Tự động tìm kiếm các video hài hước hot xu hướng mới nhất chưa từng xử lý"""
    # 1. ƯU TIÊN HÀNG ĐẦU: Quét trực tiếp TikTok/Douyin qua RapidAPI TokApi nếu có RAPIDAPI_KEY
    if RAPIDAPI_KEY:
        rapid_candidates = scout_tiktok_douyin_via_rapidapi(limit)
        if rapid_candidates:
            return rapid_candidates
            
    # 2. DỰ PHÒNG: Quét YouTube Shorts nếu chưa cấu hình RAPIDAPI_KEY
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'skip_download': True,
        'ffmpeg_location': ffmpeg_exe,
        'playlist_items': '1-8'
    }
    
    db = load_processed_db()
    processed_ids = set(db.get("videos", []))
    
    # Chọn ngẫu nhiên 2 từ khóa để làm mới nội dung mỗi lần quét
    chosen_keywords = random.sample(SCOUT_KEYWORDS, 2)
    candidates = []
    
    for kw in chosen_keywords:
        try:
            # Tìm kiếm video mới tải lên gần đây (sp=CAISAhAB) để đảm bảo video luôn mới nhất và bắt trend
            encoded_kw = urllib.parse.quote_plus(kw)
            search_url = f"https://www.youtube.com/results?search_query={encoded_kw}&sp=CAISAhAB"
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(search_url, download=False)
                entries = res.get('entries', [])
                
                # Dự phòng: nếu tìm kiếm theo ngày không có thì tìm kiếm từ khóa thông thường
                if not entries:
                    res = ydl.extract_info(f"ytsearch6:{kw}", download=False)
                    entries = res.get('entries', [])
                    
                for e in entries:
                    v_id = e.get('id')
                    dur = e.get('duration')
                    # Chỉ lấy video ngắn chuẩn TikTok/Shorts (dưới 65 giây)
                    if v_id and v_id not in processed_ids and (dur is None or dur <= 65):
                        candidates.append({
                            "id": v_id,
                            "title": e.get('title', 'Video Hài Hước'),
                            "url": f"https://www.youtube.com/watch?v={v_id}",
                            "duration": dur
                        })
        except Exception as e:
            logger.error(f"🔴 Lỗi khi quét từ khóa '{kw}': {e}")
            
    return candidates

def download_and_process_video(video_url, video_id, title, source_desc="Tự động quét xu hướng", is_direct_cdn=False):
    """Thực thi chuỗi xử lý: Tải -> FFmpeg Lách Bản Quyền -> Đẩy Drive 5TB -> Gửi Telegram"""
    raw_file = os.path.join(DOWNLOAD_DIR, f"raw_{video_id}.mp4")
    clean_file = os.path.join(DOWNLOAD_DIR, f"clean_{video_id}.mp4")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    
    send_telegram_message(
        f"🎯 <b>[PHÁT HIỆN VIDEO HÀI HƯỚC TRIỆU VIEW MỚI]</b>\n\n"
        f"🎬 <b>Tiêu đề:</b> {title[:75]}...\n"
        f"🔍 <b>Nguồn:</b> {source_desc}\n\n"
        f"⏳ <i>Đang tự động tải về và xử lý lách bản quyền...</i>"
    )
    
    # 1. Tải video (Ưu tiên tải thẳng từ CDN TikTok/Douyin nếu có link trực tiếp)
    if is_direct_cdn or (".mp4" in video_url and "youtube" not in video_url) or "byteoversea" in video_url or "tiktokcdn" in video_url:
        logger.info("📥 Đang tải trực tiếp từ CDN máy chủ TikTok/Douyin...")
        if not download_video_file(video_url, raw_file):
            logger.error(f"🔴 Lỗi tải video từ CDN: {video_url}")
            return False
    else:
        # Tải qua yt-dlp (Bỏ qua webpage để tránh 429/403)
        ydl_opts = {
            'outtmpl': raw_file,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            'ffmpeg_location': ffmpeg_exe,
            'quiet': True,
            'noplaylist': True
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            logger.error(f"🔴 Lỗi tải video {video_url}: {e}")
            send_telegram_message(f"⚠️ Lỗi tải video {video_id}: {e}")
            return False

    if not os.path.exists(raw_file) or os.path.getsize(raw_file) == 0:
        send_telegram_message(f"⚠️ File tải về rỗng: {video_id}")
        return False
        
    # 2. Xử lý lách bản quyền
    success = process_video_anti_detection(raw_file, clean_file)
    if not success or not os.path.exists(clean_file):
        send_telegram_message(f"⚠️ Lỗi xử lý video bằng FFmpeg: {video_id}")
        return False
        
    # 3. Tải lên Google Drive 5TB
    drive_link = None
    target_upload_file = clean_file if os.path.exists(clean_file) else raw_file
    drive_file_name = f"TikTok_HaiHuoc_{video_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    
    if gdrive_service:
        drive_link = upload_to_google_drive(target_upload_file, drive_file_name)
        
    # 4. Gửi kết quả hoàn chỉnh về Telegram
    caption = generate_funny_vietnamese_caption(title)
    
    if drive_link:
        success_msg = (
            f"🎉 <b>[VIDEO HÀI HƯỚC ĐÃ LƯU VÀO GOOGLE DRIVE 5TB]</b>\n\n"
            f"{caption}\n\n"
            f"─────────────────────────\n"
            f"☁️ <b>LINK DRIVE 5TB (XEM & ĐĂNG NGAY):</b>\n"
            f"👉 <a href='{drive_link}'><b>BẤM VÀO ĐÂY ĐỂ MỞ VIDEO TRÊN DRIVE</b></a>\n\n"
            f"📱 <b>CÁCH ĐĂNG TIỆN LỢI (0MB BỘ NHỚ ĐIỆN THOẠI):</b>\n"
            f"<i>Mở link trên điện thoại &rarr; Bấm 3 chấm (...) &rarr; Chọn 'Gửi bản sao' &rarr; Chọn TikTok để đăng trực tiếp!</i>"
        )
    else:
        success_msg = (
            f"🎉 <b>[VIDEO HÀI HƯỚC ĐÃ XỬ LÝ XONG]</b>\n\n"
            f"{caption}\n\n"
            f"─────────────────────────\n"
            f"💾 <b>Video đã lưu cục bộ tại:</b> <code>{target_upload_file}</code>\n\n"
            f"💡 <i>Mẹo: Hãy thêm file <code>service_account.json</code> vào thư mục bot để tự động đẩy thẳng lên Google Drive 5TB!</i>"
        )
        
    send_telegram_message(success_msg)
    
    # 5. Lưu ID vào CSDL để không trùng
    db = load_processed_db()
    if "videos" not in db:
        db["videos"] = []
    db["videos"].append(video_id)
    save_processed_db(db)
    
    # 6. Dọn dẹp bộ nhớ đệm (0MB tồn đọng)
    try:
        if os.path.exists(raw_file): os.remove(raw_file)
        if drive_link and os.path.exists(clean_file): os.remove(clean_file)
    except Exception:
        pass
        
    return True

def execute_auto_scout_job():
    """Thực hiện một lượt quét tự động săn video hot"""
    logger.info("🔍 Bắt đầu lượt tự động quét video hot xu hướng...")
    candidates = scout_trending_funny_videos(limit=5)
    
    if not candidates:
        logger.info("ℹ️ Không tìm thấy video mới hoặc tất cả đã được xử lý.")
        return False
        
    for target in candidates[:3]:
        logger.info(f"🎯 Đang thử tải video hot: {target['id']} - {target['title']}")
        is_direct_cdn = target.get("is_direct_cdn", False)
        success = download_and_process_video(target['url'], target['id'], target['title'], "Hệ thống tự động săn xu hướng", is_direct_cdn=is_direct_cdn)
        if success:
            return True
        logger.warning(f"⚠️ Video {target['id']} không thể tải, thử video tiếp theo...")
        
    return False

async def auto_scout_loop():
    """Vòng lặp tự động chạy quét video theo giờ quy định"""
    # Đợi 10 giây sau khi khởi động để bot ổn định kết nối Telegram
    await asyncio.sleep(10)
    
    # Thực hiện 1 lượt quét ngay khi khởi động
    try:
        await asyncio.to_thread(execute_auto_scout_job)
    except Exception as e:
        logger.error(f"🔴 Lỗi trong lượt quét khởi động: {e}")
        
    while True:
        try:
            sleep_seconds = int(AUTO_SCOUT_INTERVAL_HOURS * 3600)
            logger.info(f"⏰ Chờ {AUTO_SCOUT_INTERVAL_HOURS} giờ cho lượt quét tự động tiếp theo...")
            await asyncio.sleep(sleep_seconds)
            await asyncio.to_thread(execute_auto_scout_job)
        except Exception as e:
            logger.error(f"🔴 Lỗi trong vòng lặp auto_scout: {e}")
            await asyncio.sleep(60)

async def poll_telegram_messages():
    """Lắng nghe lệnh tương tác từ người dùng trên Telegram"""
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    
    while True:
        try:
            params = {"offset": offset, "timeout": 20}
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 200:
                data = r.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    
                    if not text:
                        continue
                        
                    if text in ["/scout", "/san", "/sanvideo", "/tim"]:
                        send_telegram_message("🔍 <b>Đang kích hoạt tìm kiếm video hài hước hot xu hướng ngay bây giờ...</b>")
                        asyncio.create_task(asyncio.to_thread(execute_auto_scout_job))
                    elif text.startswith("/status"):
                        db = load_processed_db()
                        count = len(db.get("videos", []))
                        drive_txt = "ĐÃ KẾT NỐI (5TB)" if gdrive_service else "CHƯA KẾT NỐI"
                        status_msg = (
                            f"📊 <b>[TRẠNG THÁI BOT TỰ ĐỘNG SĂN VIDEO HÀI HƯỚC]</b>\n\n"
                            f"🤖 <b>Chế độ tự động quét:</b> ĐANG BẬT\n"
                            f"⏰ <b>Tần suất quét:</b> Mỗi {AUTO_SCOUT_INTERVAL_HOURS} giờ/lần\n"
                            f"🎬 <b>Số video đã xử lý:</b> {count} video\n"
                            f"☁️ <b>Google Drive 5TB:</b> {drive_txt}\n\n"
                            f"💡 <i>Gõ /scout để yêu cầu bot săn ngay 1 video mới bất kỳ lúc nào!</i>"
                        )
                        send_telegram_message(status_msg)
                    elif text.startswith("/start") or text.startswith("/help"):
                        help_msg = (
                            f"🤖 <b>BOT TỰ ĐỘNG SĂN VIDEO HÀI HƯỚC & ĐẨY DRIVE 5TB</b>\n\n"
                            f"🎯 <b>Cơ chế hoạt động 100% tự động:</b>\n"
                            f"• Bạn KHÔNG cần phải tự lướt tìm video!\n"
                            f"• Bot định kỳ tự quét các video hài hước triệu view.\n"
                            f"• Tự động lách bản quyền & đẩy thẳng lên Google Drive 5TB.\n"
                            f"• Soạn sẵn tiêu đề tiếng Việt + hashtag SEO gửi về đây cho bạn.\n\n"
                            f"⚡ <b>Các lệnh nhanh:</b>\n"
                            f"👉 <code>/scout</code> : Săn ngay 1 video hot ngay lập tức\n"
                            f"👉 <code>/status</code> : Kiểm tra trạng thái bot & số video đã lưu\n\n"
                            f"📱 <i>Ngoài ra, nếu bạn thấy link video nào hay thì vẫn có thể dán link vào đây để bot xử lý!</i>"
                        )
                        send_telegram_message(help_msg)
                    elif "http" in text:
                        # Hỗ trợ xử lý link thủ công nếu người dùng tiện tay dán vào
                        logger.info(f"📩 Nhận link thủ công: {text}")
                        v_id = f"manual_{int(time.time())}"
                        asyncio.create_task(asyncio.to_thread(download_and_process_video, text, v_id, "Video người dùng yêu cầu", "Nhập link thủ công"))
        except Exception as e:
            await asyncio.sleep(5)
        await asyncio.sleep(2)

async def main():
    start_health_check_server()
    
    welcome_msg = (
        f"🚀 <b>[BOT TỰ ĐỘNG SĂN VIDEO HÀI HƯỚC & DRIVE 5TB ĐÃ KHỞI ĐỘNG]</b>\n\n"
        f"🎭 <b>Chủ đề:</b> Video Hài Hước, Tiểu Phẩm Triệu View\n"
        f"🤖 <b>Tính năng chính:</b> 100% TỰ ĐỘNG QUÉT & SĂN VIDEO (Bạn không cần phải tự lướt tìm!)\n"
        f"⏰ <b>Lịch trình:</b> Tự động quét và đẩy video lên Drive mỗi {AUTO_SCOUT_INTERVAL_HOURS} giờ\n"
        f"☁️ <b>Google Drive 5TB:</b> {'Đã kết nối' if gdrive_service else 'Chờ file service_account.json'}\n\n"
        f"⚡ <i>Hệ thống đang bắt đầu quét lượt video đầu tiên...</i>"
    )
    send_telegram_message(welcome_msg)
    logger.info("🤖 Bot Tự Động Săn Video Hài Hước đang hoạt động...")
    
    # Chạy song song: vòng lặp tự động quét theo giờ + vòng lặp lắng nghe lệnh Telegram
    await asyncio.gather(
        auto_scout_loop(),
        poll_telegram_messages()
    )

if __name__ == "__main__":
    asyncio.run(main())
