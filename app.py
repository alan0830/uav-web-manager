import os
import re
import io
import json
import time
import shutil
import base64
import threading
import subprocess
import urllib.parse
from functools import wraps
from datetime import datetime
from PIL import Image
import requests
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, request, redirect, jsonify, render_template_string, Response, session, url_for
)

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CURL_CFFI = False

# 容器內部目錄配置
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
MEDIA_LIBRARY_DIR = os.getenv("MEDIA_LIBRARY_DIR", "/media_library")

# 宿主機 (NAS) 動態路徑（供網頁動態呈現）
HOST_DOWNLOAD_DIR = os.getenv("HOST_DOWNLOAD_DIR", DOWNLOAD_DIR)
HOST_MEDIA_DIR = os.getenv("HOST_MEDIA_DIR", MEDIA_LIBRARY_DIR)

QUEUE_FILE = os.path.join(DOWNLOAD_DIR, ".uav_queue.json")
LOG_FILE = os.path.join(DOWNLOAD_DIR, ".uav_web_downloader.log")
USERS_FILE = os.path.join(DOWNLOAD_DIR, ".uav_users.json")
SECRET_KEY_FILE = os.path.join(DOWNLOAD_DIR, ".uav_secret_key")
SETTINGS_FILE = os.path.join(DOWNLOAD_DIR, ".uav_settings.json")

MAX_GLOBAL_LOG_SIZE = 5 * 1024 * 1024
KEEP_GLOBAL_LOG_SIZE = 2 * 1024 * 1024
MAX_ITEM_LOG_LINES = 500

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(MEDIA_LIBRARY_DIR, exist_ok=True)

app = Flask(__name__)

# 持久化 Secret Key
if not os.path.exists(SECRET_KEY_FILE):
    try:
        with open(SECRET_KEY_FILE, "wb") as f:
            f.write(os.urandom(32))
    except Exception:
        pass

if os.path.exists(SECRET_KEY_FILE):
    try:
        with open(SECRET_KEY_FILE, "rb") as f:
            app.secret_key = f.read()
    except Exception:
        app.secret_key = os.urandom(32)
else:
    app.secret_key = os.urandom(32)

lock = threading.Lock()
SCRAPED_CACHE = {}


# ==================== 命名設定管理 ====================

DEFAULT_SETTINGS = {
    "naming_rule": "%Name%/%ProductNumber%/%HasSubtitle%[%ReleaseDate%] - [%ProductNumber%] - [%Title%]",
    "multi_naming_rule": "%ActorCount%/%Name%/%ProductNumber%/%HasSubtitle%[%ReleaseDate%] - [%ProductNumber%] - [%Title%]",
    "multi_actor_delimiter": " ",
    "max_actor_count": 5
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            s = DEFAULT_SETTINGS.copy()
            s.update(data)
            return s
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# ==================== 帳號驗證 ====================

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_users(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)

def has_users():
    return len(load_users()) > 0

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not has_users():
            return redirect(url_for("setup"))
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


# ==================== 日誌與佇列 ====================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_global(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_queue(queue):
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)

def update_item(item_id, **kwargs):
    with lock:
        queue = load_queue()
        for item in queue:
            if item.get("id") == item_id:
                item.update(kwargs)
                break
        save_queue(queue)

def parse_progress(line):
    seg = re.search(r"(\d+)\s*/\s*(\d+)(?:\s*片段)?", line)
    if seg:
        try:
            curr, total = int(seg.group(1)), int(seg.group(2))
            if total > 0:
                return min(100, max(0, int((curr / total) * 100)))
        except Exception:
            pass

    for p in [r"下載進度:\s*(\d+)%", r"\[download\]\s*([0-9\.]+)%", r"(\d+)%\|", r"(\d+)%"]:
        m = re.search(p, line)
        if m:
            try:
                v = int(float(m.group(1)))
                if 0 <= v <= 100:
                    return v
            except Exception:
                pass
    return None

def append_item_log(item_id, msg):
    line = f"[{now()}] {msg}"
    with lock:
        queue = load_queue()
        for item in queue:
            if item.get("id") == item_id:
                logs = item.get("logs", [])
                logs.append(line)
                item["logs"] = logs[-MAX_ITEM_LOG_LINES:]
                progress = parse_progress(msg)
                if progress is not None:
                    item["progress"] = progress
                    detail = re.search(r"(\d+/\d+\s*片段.*?)$", msg)
                    if detail:
                        item["message"] = f"下載中 {progress}% ({detail.group(1).strip()})"
                    else:
                        item["message"] = f"下載中，{progress}%"
                break
        save_queue(queue)
    log_global(msg)

def clean_non_video_files(directory):
    video_exts = {".mp4", ".mkv", ".avi", ".ts", ".wmv", ".mov", ".m4v"}
    try:
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.startswith("."):
                    continue
                path = os.path.join(root, f)
                ext = os.path.splitext(f)[1].lower()
                if ext not in video_exts:
                    try:
                        os.remove(path)
                        log_global(f"自動清理非影片暫存檔：{f}")
                    except Exception:
                        pass
    except Exception as e:
        log_global(f"清理暫存檔失敗: {e}")


# ==================== 命名規則演算法 ====================

PRE_FILTERS = [
    r"\[98t\.tv\]", r"\[22sht\.me\]", r"\[7sht\.me\]", r"hhd800\.com@",
    r"SIS001@", r"第一會所新片@", r"fuli\.site", r"gg5\.co", r"sex8\.cc",
    r"thz\.la", r"1080p", r"720p", r"4k", r"2k", r"fhd", r"hd", r"uncensored"
]

def extract_movie_id(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    cleaned = stem
    for flt in PRE_FILTERS:
        cleaned = re.sub(flt, " ", cleaned, flags=re.IGNORECASE)

    fc2_m = re.search(r"(?i)\b(?:fc2[-_]ppv[-_]|fc2[-_]|fc2)(\d{5,8})\b", cleaned)
    if fc2_m:
        return f"FC2-PPV-{fc2_m.group(1)}"

    carib_m = re.search(r"(?i)\bcarib(?:bean)?[-_]?(\d{6}[-_]\d{3})\b", cleaned)
    if carib_m:
        return f"CARIB-{carib_m.group(1)}"

    std_m = re.search(r"(?i)\b([a-z]{2,6})[-_]?(\d{3,5})\b", cleaned)
    if std_m:
        return f"{std_m.group(1).upper()}-{std_m.group(2)}"

    return cleaned.strip()

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def compute_target_path(meta, ext, settings=None):
    if not settings:
        settings = load_settings()

    actors = meta.get("actors", [])
    actor_count = len(actors)
    is_multi = actor_count > 1 or actor_count == 0

    if actor_count == 0:
        actor_count_str = "0人(素人)"
    elif actor_count == 1:
        actor_count_str = "1人"
    elif actor_count == 2:
        actor_count_str = "2人"
    elif actor_count == 3:
        actor_count_str = "3人"
    elif actor_count == 4:
        actor_count_str = "4人"
    else:
        actor_count_str = "5人以上"

    max_c = settings.get("max_actor_count", 5)
    actors_to_show = actors[:max_c] if actors else ["素人"]
    delim = settings.get("multi_actor_delimiter", " ")
    formatted_names = delim.join(actors_to_show)

    rule = settings["multi_naming_rule"] if is_multi else settings["naming_rule"]

    rule_eval = rule.replace("%ActorCount%", actor_count_str)
    rule_eval = rule_eval.replace("%Name%", formatted_names)
    rule_eval = rule_eval.replace("%ProductNumber%", meta.get("id", "UNKNOWN"))
    rule_eval = rule_eval.replace("%Title%", meta.get("title", ""))
    rule_eval = rule_eval.replace("%ReleaseDate%", meta.get("release_date", ""))
    rule_eval = rule_eval.replace("%Year%", meta.get("year", ""))
    rule_eval = rule_eval.replace("%HasSubtitle%", "")

    parts = [p.strip() for p in re.split(r"[\\/]", rule_eval) if p.strip()]
    if not parts:
        parts = [meta.get("id", "UNKNOWN")]

    folder_parts = [sanitize_filename(p) for p in parts[:-1]]
    file_part = sanitize_filename(parts[-1])
    if not file_part:
        file_part = meta.get("id", "UNKNOWN")

    ext_clean = ext if ext.startswith(".") else f".{ext}"
    target_rel = os.path.join(*folder_parts, f"{file_part}{ext_clean}") if folder_parts else f"{file_part}{ext_clean}"
    return target_rel.replace("\\", "/")


# ==================== 多網站備援刮削 ＆ 翻譯 ====================

def translate_to_chinese(text):
    if not text or not text.strip():
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx", "sl": "auto", "tl": "zh-TW", "dt": "t", "q": text.strip()
        }
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                translated = "".join([part[0] for part in data[0] if part and len(part) > 0 and part[0]])
                if translated.strip():
                    return translated.strip()
    except Exception as e:
        log_global(f"Google 翻譯失敗 (保留原文): {e}")
    return text.strip()

def download_image_bytes(url):
    if not url:
        return None

    referer = "https://www.google.com/"
    if "javbus" in url:
        referer = "https://www.javbus.com/"
    elif "jdb" in url or "javdb" in url:
        referer = "https://javdb.com/"
    elif "fc2" in url:
        referer = "https://adult.contents.fc2.com/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
    except Exception as e:
        log_global(f"圖片下載失敗 ({url}): {e}")
    return None

def scrape_fc2_official(movie_id):
    digits = "".join(filter(str.isdigit, movie_id))
    if not digits:
        return None
    url = f"https://adult.contents.fc2.com/article/{digits}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": "contents_adult=1; f_a_c_i=1"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        title_el = soup.select_one("h3.items_article_headerInfo, .main_content h3")
        if not title_el:
            return None
        raw_title = title_el.get_text().strip().split("|")[0].strip()

        cover_el = soup.select_one(".items_article_MainitemThumb img, .items_article_headerInfo img")
        cover_url = ""
        if cover_el:
            cover_url = cover_el.get("src") or cover_el.get("data-src", "")
            if cover_url.startswith("//"):
                cover_url = "https:" + cover_url

        return {
            "source": "FC2官方",
            "id": f"FC2-PPV-{digits}",
            "title": translate_to_chinese(raw_title),
            "original_title": raw_title,
            "release_date": "",
            "year": "",
            "duration": "",
            "studio": "FC2",
            "director": "",
            "actors": ["素人"],
            "genres": ["素人", "FC2-PPV"],
            "cover_url": cover_url
        }
    except Exception:
        return None

def scrape_javbus(movie_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8"
    }
    urls_to_try = [
        f"https://www.javbus.com/{movie_id}",
        f"https://www.javbus.com/uncensored/{movie_id}"
    ]
    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            h3 = soup.find("h3")
            if not h3:
                continue
            raw_title = h3.get_text().strip()

            cover_tag = soup.find("a", class_="bigImage")
            cover_url = ""
            if cover_tag and cover_tag.get("href"):
                h = cover_tag["href"]
                cover_url = h if h.startswith("http") else f"https://www.javbus.com{h}"

            release_date, duration, studio, director = "", "", "", ""
            info_div = soup.find("div", class_="info")
            if info_div:
                for p in info_div.find_all("p"):
                    txt = p.get_text()
                    if "發行日期:" in txt or "发行日期:" in txt:
                        release_date = txt.split(":")[-1].strip()
                    elif "長度:" in txt or "长度:" in txt:
                        duration = "".join(filter(str.isdigit, txt))
                    elif "製作商:" in txt or "制作商:" in txt:
                        studio = txt.split(":")[-1].strip()
                    elif "導演:" in txt or "导演:" in txt:
                        director = txt.split(":")[-1].strip()

            actors = []
            for a in soup.select(".star-name a, .genre a[href*='/star/']"):
                act = a.get_text().strip()
                if act and act not in actors:
                    actors.append(act)

            genres = []
            for g in soup.select("span.genre a"):
                gt = g.get_text().strip()
                if gt and gt not in genres and "star" not in g.get("href", ""):
                    genres.append(gt)

            return {
                "source": "JavBus",
                "id": movie_id,
                "title": translate_to_chinese(raw_title),
                "original_title": raw_title,
                "release_date": release_date,
                "year": release_date[:4] if len(release_date) >= 4 else "",
                "duration": duration,
                "studio": studio,
                "director": director,
                "actors": actors,
                "genres": genres,
                "cover_url": cover_url
            }
        except Exception:
            continue
    return None

def scrape_javdb(movie_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"
    }
    search_url = f"https://javdb.com/search?q={movie_id}&f=all"
    try:
        resp = requests.get(search_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        item = soup.select_one(".grid-item a.box, .movie-list .item a")
        if not item or not item.get("href"):
            return None

        detail_path = item["href"]
        detail_url = detail_path if detail_path.startswith("http") else f"https://javdb.com{detail_path}"

        d_resp = requests.get(detail_url, headers=headers, timeout=10)
        if d_resp.status_code != 200:
            return None
        dsoup = BeautifulSoup(d_resp.text, "html.parser")

        title_el = dsoup.select_one("strong.current-title, .title.is-4 strong")
        raw_title = title_el.get_text().strip() if title_el else movie_id

        cover_el = dsoup.select_one("img.video-cover, .column-video-cover img")
        cover_url = ""
        if cover_el:
            c = cover_el.get("data-src") or cover_el.get("src", "")
            if c.startswith("//"):
                cover_url = "https:" + c
            elif c.startswith("/"):
                cover_url = "https://javdb.com" + c
            else:
                cover_url = c

        release_date, duration, studio, director = "", "", "", ""
        actors = []
        genres = []

        for p in dsoup.select(".panel-block"):
            txt = p.get_text()
            if "日期:" in txt:
                release_date = txt.split(":")[-1].strip()
            elif "時長:" in txt or "时长:" in txt:
                duration = "".join(filter(str.isdigit, txt))
            elif "片商:" in txt:
                studio = txt.split(":")[-1].strip()
            elif "導演:" in txt or "导演:" in txt:
                director = txt.split(":")[-1].strip()
            elif "演員:" in txt or "演员:" in txt:
                for a in p.select("a"):
                    name = a.get_text().strip()
                    if name and name not in actors:
                        actors.append(name)
            elif "類別:" in txt or "类别:" in txt:
                for a in p.select("a"):
                    g = a.get_text().strip()
                    if g and g not in genres:
                        genres.append(g)

        return {
            "source": "JavDB",
            "id": movie_id,
            "title": translate_to_chinese(raw_title),
            "original_title": raw_title,
            "release_date": release_date,
            "year": release_date[:4] if len(release_date) >= 4 else "",
            "duration": duration,
            "studio": studio,
            "director": director,
            "actors": actors,
            "genres": genres,
            "cover_url": cover_url
        }
    except Exception:
        return None

def multi_source_scrape(movie_id):
    clean_id = movie_id.strip()
    candidates = [clean_id]
    upper = clean_id.upper()

    if upper.startswith("STARTS-"):
        candidates.append(upper.replace("STARTS-", "START-", 1))
        candidates.append(upper.replace("STARTS-", "STARS-", 1))

    if "FC2" in upper:
        meta = scrape_fc2_official(clean_id)
        if meta:
            return meta

    for c in candidates:
        meta = scrape_javbus(c)
        if meta:
            return meta

    for c in candidates:
        meta = scrape_javdb(c)
        if meta:
            return meta

    if any(ch.isdigit() for ch in clean_id):
        meta = scrape_fc2_official(clean_id)
        if meta:
            return meta

    return None

def crop_and_create_poster(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        fanart_io = io.BytesIO()
        img.save(fanart_io, "JPEG", quality=95)
        fanart_bytes = fanart_io.getvalue()

        w, h = img.size
        if (w / h) > 1.15:
            ideal_w = int(h * 0.704)
            if ideal_w > w:
                ideal_w = w
            crop_x = w - ideal_w
            poster_img = img.crop((crop_x, 0, w, h))
        else:
            poster_img = img

        poster_io = io.BytesIO()
        poster_img.save(poster_io, "JPEG", quality=95)
        poster_bytes = poster_io.getvalue()

        thumb = poster_img.copy()
        thumb.thumbnail((160, 240))
        thumb_io = io.BytesIO()
        thumb.save(thumb_io, "JPEG", quality=80)
        poster_b64 = "data:image/jpeg;base64," + base64.b64encode(thumb_io.getvalue()).decode()

        return fanart_bytes, poster_bytes, poster_b64
    except Exception as e:
        log_global(f"海報裁切失敗: {e}")
        return None, None, None

def generate_nfo_xml(meta):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>', '<movie>']
    xml.append(f"  <title>[{esc(meta['id'])}] {esc(meta['title'])}</title>")
    if meta.get("original_title"):
        xml.append(f"  <originaltitle>{esc(meta['original_title'])}</originaltitle>")
    xml.append(f"  <sorttitle>{esc(meta['id'])}</sorttitle>")
    xml.append(f"  <num>{esc(meta['id'])}</num>")

    if meta.get("release_date"):
        xml.append(f"  <releasedate>{esc(meta['release_date'])}</releasedate>")
        xml.append(f"  <premiered>{esc(meta['release_date'])}</premiered>")
    if meta.get("year"):
        xml.append(f"  <year>{esc(meta['year'])}</year>")
    if meta.get("duration"):
        xml.append(f"  <runtime>{esc(meta['duration'])}</runtime>")
    if meta.get("studio"):
        xml.append(f"  <studio>{esc(meta['studio'])}</studio>")
        xml.append(f"  <maker>{esc(meta['studio'])}</maker>")
    if meta.get("director"):
        xml.append(f"  <director>{esc(meta['director'])}</director>")

    for act in meta.get("actors", []):
        xml.append("  <actor>")
        xml.append(f"    <name>{esc(act)}</name>")
        xml.append("    <type>Actor</type>")
        xml.append("  </actor>")

    for gen in meta.get("genres", []):
        xml.append(f"  <genre>{esc(gen)}</genre>")
        xml.append(f"  <tag>{esc(gen)}</tag>")

    xml.append("</movie>")
    return "\n".join(xml)


# ==================== 線上影音探索與搜尋爬蟲模組 ====================

_BROWSER_SESSION = None
_BROWSER_LOCK = threading.Lock()

def get_browser_session():
    global _BROWSER_SESSION
    with _BROWSER_LOCK:
        if _BROWSER_SESSION is None:
            if HAS_CURL_CFFI and hasattr(cffi_requests, "Session"):
                try:
                    _BROWSER_SESSION = cffi_requests.Session(impersonate="chrome120")
                except Exception:
                    _BROWSER_SESSION = cffi_requests.Session()
            else:
                _BROWSER_SESSION = cffi_requests.Session()
            _BROWSER_SESSION.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
            })
        return _BROWSER_SESSION

def robust_browser_get(url, referer=None, timeout=12):
    s = get_browser_session()
    headers = {}
    if referer:
        headers["Referer"] = referer
    try:
        return s.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        log_global(f"連線請求異常重試中 ({url}): {e}")
        global _BROWSER_SESSION
        with _BROWSER_LOCK:
            _BROWSER_SESSION = None
        s = get_browser_session()
        return s.get(url, headers=headers, timeout=timeout)

# ==================== 女優頭像快取與查詢 ====================

ACTRESS_AVATAR_CACHE = {
    "森日向子": "https://www.javbus.com/pics/actress/wcd_a.jpg",
    "楓カレン": "https://www.javbus.com/pics/actress/u4m_a.jpg",
    "神木麗": "https://www.javbus.com/pics/actress/zh0_a.jpg",
    "河北彩花": "https://www.javbus.com/pics/actress/sl1_a.jpg",
    "五日市芽依": "https://www.javbus.com/pics/actress/10xw_a.jpg",
    "涼森れむ": "https://www.javbus.com/pics/actress/uly_a.jpg",
    "七瀬アリス": "https://www.javbus.com/pics/actress/wjv_a.jpg",
    "楓ふうあ": "https://www.javbus.com/pics/actress/xog_a.jpg",
    "白峰ミウ": "https://www.javbus.com/pics/actress/wyw_a.jpg",
    "橘メアリー": "https://www.javbus.com/pics/actress/mr2_a.jpg",
    "楪カレン": "https://www.javbus.com/pics/actress/x2w_a.jpg",
    "北野未奈": "https://www.javbus.com/pics/actress/x86_a.jpg",
    "三上悠亜": "https://www.javbus.com/pics/actress/okq_a.jpg",
    "桃乃木かな": "https://www.javbus.com/pics/actress/p6v_a.jpg",
    "森沢かな": "https://www.javbus.com/pics/actress/t54_a.jpg",
    "美園和花": "https://www.javbus.com/pics/actress/s2i_a.jpg",
    "桜空もも": "https://www.javbus.com/pics/actress/r62_a.jpg",
    "波多野結衣": "https://www.javbus.com/pics/actress/2jv_a.jpg",
    "深田えいみ": "https://www.javbus.com/pics/actress/tyv_a.jpg",
    "篠田ゆう": "https://www.javbus.com/pics/actress/2pv_a.jpg",
    "相沢みなみ": "https://www.javbus.com/pics/actress/qfy_a.jpg",
    "明里つむぎ": "https://www.javbus.com/pics/actress/qs6_a.jpg",
    "葵つかさ": "https://www.javbus.com/pics/actress/2xi_a.jpg",
    "橋本ありな": "https://www.javbus.com/pics/actress/pmv_a.jpg",
    "JULIA": "https://www.javbus.com/pics/actress/2de_a.jpg",
    "天使もえ": "https://www.javbus.com/pics/actress/n5q_a.jpg",
    "小野六花": "https://www.javbus.com/pics/actress/w6w_a.jpg",
    "山手梨愛": "https://www.javbus.com/pics/actress/10gq_a.jpg",
    "石川澪": "https://www.javbus.com/pics/actress/101l_a.jpg",
    "八木奈々": "https://www.javbus.com/pics/actress/vve_a.jpg",
}

ACTRESS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "actress_cache.json")
if os.path.exists(ACTRESS_CACHE_FILE):
    try:
        with open(ACTRESS_CACHE_FILE, "r", encoding="utf-8") as f:
            ACTRESS_AVATAR_CACHE.update(json.load(f))
    except Exception:
        pass

def get_actress_avatar(name):
    name_clean = name.strip()
    if name_clean in ACTRESS_AVATAR_CACHE:
        return ACTRESS_AVATAR_CACHE[name_clean]
    for k, v in ACTRESS_AVATAR_CACHE.items():
        if k in name_clean or name_clean in k:
            return v
    try:
        q = urllib.parse.quote(name_clean)
        r = robust_browser_get(f"https://www.javbus.com/searchstar/{q}", referer="https://www.javbus.com/", timeout=4)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            box = soup.select_one("a.avatar-box")
            img = box.find("img") if box else None
            if img and img.get("src"):
                src = img["src"]
                if not src.startswith("http"):
                    src = "https://www.javbus.com" + src
                ACTRESS_AVATAR_CACHE[name_clean] = src
                return src
    except Exception:
        pass
    return ""

def fetch_jable_videos(cat="today-hot", page=1):
    urls = {
        "today-hot": "https://jable.tv/hot/",
        "weekly-hot": "https://jable.tv/hot/?sort_by=video_viewed_week",
        "monthly-hot": "https://jable.tv/hot/?sort_by=video_viewed_month",
        "latest": "https://jable.tv/latest-updates/",
        "chinese": "https://jable.tv/categories/chinese-subtitle/",
        "uncensored": "https://jable.tv/categories/uncensored/",
    }
    url = urls.get(cat, "https://jable.tv/hot/")
    if page > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}from={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for box in soup.select("div.video-img-box"):
            a = box.find("a", href=True)
            img = box.find("img")
            title_el = box.find_next("h6") or box.find("h6")
            dur_el = box.find("span", class_="label")
            if not a:
                continue
            v_url = a["href"]
            cover = img.get("data-src") or img.get("src") if img else ""
            title = title_el.text.strip() if title_el else ""
            dur = dur_el.text.strip() if dur_el else ""
            mid = v_url.rstrip("/").split("/")[-1].upper()
            results.append({
                "id": mid,
                "title": title,
                "url": v_url,
                "cover": cover,
                "duration": dur,
                "source": "JableTV"
            })
        return results
    except Exception as e:
        log_global(f"Jable 分類抓取失敗 ({cat}): {e}")
        return []

def search_jable_videos(query, page=1):
    q = urllib.parse.quote(query.strip())
    url = f"https://jable.tv/search/{q}/"
    if page > 1:
        url = f"{url}?from={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for box in soup.select("div.video-img-box"):
            a = box.find("a", href=True)
            img = box.find("img")
            title_el = box.find_next("h6") or box.find("h6")
            dur_el = box.find("span", class_="label")
            if not a:
                continue
            v_url = a["href"]
            cover = img.get("data-src") or img.get("src") if img else ""
            title = title_el.text.strip() if title_el else ""
            dur = dur_el.text.strip() if dur_el else ""
            mid = v_url.rstrip("/").split("/")[-1].upper()
            results.append({
                "id": mid,
                "title": title,
                "url": v_url,
                "cover": cover,
                "duration": dur,
                "source": "JableTV"
            })
        return results
    except Exception as e:
        log_global(f"Jable 搜尋失敗 ({query}): {e}")
        return []

def fetch_jable_actresses(page=1):
    url = "https://jable.tv/models/?sort_by=model_viewed"
    if page > 1:
        url = f"https://jable.tv/models/{page}/?sort_by=model_viewed"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        actresses = []
        for box in soup.select("div.horizontal-img-box"):
            a = box.find("a", href=True)
            if not a or "/models/" not in a["href"]:
                continue
            img = box.find("img")
            title_el = box.find(["h6", "h5", "h4"]) or a
            sub_el = box.find("span")
            name = title_el.text.strip() if title_el else a.text.strip()
            avatar = img.get("data-src") or img.get("src") if img else ""
            if not avatar:
                avatar = get_actress_avatar(name)
            sub = sub_el.text.strip() if sub_el else ""
            if name and a["href"] != "https://jable.tv/models/":
                actresses.append({
                    "name": name,
                    "url": a["href"],
                    "avatar": avatar,
                    "video_count": sub,
                    "source": "JableTV"
                })
        return actresses
    except Exception as e:
        log_global(f"Jable 女優清單抓取失敗: {e}")
        return []

def fetch_jable_actress_videos(actress_url, page=1):
    url = actress_url
    if page > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}from={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for box in soup.select("div.video-img-box"):
            a = box.find("a", href=True)
            img = box.find("img")
            title_el = box.find_next("h6") or box.find("h6")
            dur_el = box.find("span", class_="label")
            if not a:
                continue
            v_url = a["href"]
            cover = img.get("data-src") or img.get("src") if img else ""
            title = title_el.text.strip() if title_el else ""
            dur = dur_el.text.strip() if dur_el else ""
            mid = v_url.rstrip("/").split("/")[-1].upper()
            results.append({
                "id": mid,
                "title": title,
                "url": v_url,
                "cover": cover,
                "duration": dur,
                "source": "JableTV"
            })
        return results
    except Exception as e:
        log_global(f"Jable 女優影片抓取失敗 ({actress_url}): {e}")
        return []

def fetch_missav_videos(cat="today-hot", page=1):
    urls = {
        "today-hot": "https://missav.ai/today-hot",
        "weekly-hot": "https://missav.ai/weekly-hot",
        "monthly-hot": "https://missav.ai/monthly-hot",
        "latest": "https://missav.ai/new",
        "chinese": "https://missav.ai/chinese-subtitle",
        "uncensored": "https://missav.ai/uncensored-leak",
    }
    url = urls.get(cat, "https://missav.ai/today-hot")
    if page > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}page={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for item in soup.select("div.thumbnail"):
            a = item.find("a", href=True)
            img = item.find("img")
            card = item.parent
            title_link = card.find("a", class_="text-secondary") or card.find("a", class_="text-nord4") or card.find("a", href=True)
            dur_el = item.find("span")
            if not a:
                continue
            v_url = a["href"]
            cover = img.get("data-src") or img.get("src") if img else ""
            title = title_link.text.strip() if title_link else (img.get("alt") if img else "")
            dur = dur_el.text.strip() if dur_el else ""
            mid = v_url.rstrip("/").split("/")[-1].upper()
            results.append({
                "id": mid,
                "title": title,
                "url": v_url,
                "cover": cover,
                "duration": dur,
                "source": "MissAV"
            })
        return results
    except Exception as e:
        log_global(f"MissAV 分類抓取失敗 ({cat}): {e}")
        return []

def search_missav_videos(query, page=1):
    q = urllib.parse.quote(query.strip())
    url = f"https://missav.ai/search/{q}"
    if page > 1:
        url = f"{url}?page={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for item in soup.select("div.thumbnail"):
            a = item.find("a", href=True)
            img = item.find("img")
            card = item.parent
            title_link = card.find("a", class_="text-secondary") or card.find("a", class_="text-nord4") or card.find("a", href=True)
            dur_el = item.find("span")
            if not a:
                continue
            v_url = a["href"]
            cover = img.get("data-src") or img.get("src") if img else ""
            title = title_link.text.strip() if title_link else (img.get("alt") if img else "")
            dur = dur_el.text.strip() if dur_el else ""
            mid = v_url.rstrip("/").split("/")[-1].upper()
            results.append({
                "id": mid,
                "title": title,
                "url": v_url,
                "cover": cover,
                "duration": dur,
                "source": "MissAV"
            })
        return results
    except Exception as e:
        log_global(f"MissAV 搜尋失敗 ({query}): {e}")
        return []

def fetch_missav_actresses(page=1):
    url = "https://missav.ai/actresses"
    if page > 1:
        url = f"{url}?page={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        actresses = []
        seen = set()
        for a in soup.select("a[href*='/actresses/']"):
            href = a.get("href", "")
            if href.endswith("/ranking") or href in seen:
                continue
            parent = a.find_parent("div", class_=lambda c: c and ("flex" in c or "col" in c or "card" in c or "grid" in c)) or a.parent
            img = parent.find("img")
            text_links = [l for l in parent.find_all("a", href=True) if "/actresses/" in l["href"] and l.text.strip()]
            if text_links:
                full_text = text_links[0].text.strip()
                lines = [line.strip() for line in full_text.splitlines() if line.strip()]
                name = lines[0] if lines else ""
                count = lines[1] if len(lines) > 1 else ""
            elif img and img.get("alt"):
                name = img.get("alt").strip()
                count = ""
            else:
                name = ""
                count = ""
            avatar = img.get("data-src") or img.get("src") if img else ""
            if name:
                seen.add(href)
                actresses.append({
                    "name": name,
                    "url": href,
                    "avatar": avatar,
                    "video_count": count,
                    "source": "MissAV"
                })
        return actresses
    except Exception as e:
        log_global(f"MissAV 女優清單抓取失敗: {e}")
        return []

def fetch_missav_actress_videos(actress_url, page=1):
    url = actress_url
    if page > 1:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}page={page}"
    try:
        r = robust_browser_get(url)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for item in soup.select("div.thumbnail"):
            a = item.find("a", href=True)
            img = item.find("img")
            card = item.parent
            title_link = card.find("a", class_="text-secondary") or card.find("a", class_="text-nord4") or card.find("a", href=True)
            dur_el = item.find("span")
            if not a:
                continue
            v_url = a["href"]
            cover = img.get("data-src") or img.get("src") if img else ""
            title = title_link.text.strip() if title_link else (img.get("alt") if img else "")
            dur = dur_el.text.strip() if dur_el else ""
            mid = v_url.rstrip("/").split("/")[-1].upper()
            results.append({
                "id": mid,
                "title": title,
                "url": v_url,
                "cover": cover,
                "duration": dur,
                "source": "MissAV"
            })
        return results
    except Exception as e:
        log_global(f"MissAV 女優影片抓取失敗 ({actress_url}): {e}")
        return []

def parse_supjav_posts(soup):
    results = []
    for p in soup.select("div.post"):
        a = p.find("a", class_="img") or p.find("a", href=True)
        if not a:
            continue
        href = a.get("href", "")
        if not href or href == "https://supjav.com/":
            continue
        img = p.find("img")
        title_el = p.find(["h3", "h2"]) or a
        title = title_el.text.strip() if title_el else (a.get("title") or "")
        cover = img.get("data-original") or img.get("data-src") or img.get("src") if img else ""
        if cover.startswith("data:image"):
            cover = img.get("data-original") or ""

        m = re.search(r'([A-Za-z0-9]+(?:PPV)?[-_]\d+)', title)
        code = m.group(1).upper() if m else ""
        if not code:
            parts = title.split()
            if len(parts) >= 2 and parts[0].upper() == "FC2PPV":
                code = f"FC2-PPV-{parts[1]}"
            elif len(parts) >= 1:
                code = parts[0]

        results.append({
            "id": code,
            "title": title,
            "url": href,
            "cover": cover,
            "duration": "",
            "source": "SupJav"
        })
    return results

def fetch_supjav_videos(cat="today-hot", page=1):
    urls = {
        "today-hot": "https://supjav.com/popular",
        "weekly-hot": "https://supjav.com/popular",
        "monthly-hot": "https://supjav.com/popular",
        "latest": "https://supjav.com/",
        "chinese": "https://supjav.com/category/chinese-subtitles",
        "uncensored": "https://supjav.com/category/uncensored-jav",
    }
    base = urls.get(cat, "https://supjav.com/popular")
    if page > 1:
        if base.endswith("/"):
            url = f"{base}page/{page}"
        else:
            url = f"{base}/page/{page}"
    else:
        url = base
    try:
        r = robust_browser_get(url, timeout=12)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        return parse_supjav_posts(soup)
    except Exception as e:
        log_global(f"SupJav 分類抓取失敗 ({cat}): {e}")
        return []

def search_supjav_videos(query, page=1):
    q = urllib.parse.quote(query.strip())
    if page > 1:
        url = f"https://supjav.com/page/{page}/?s={q}"
    else:
        url = f"https://supjav.com/?s={q}"
    try:
        r = robust_browser_get(url, timeout=12)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        return parse_supjav_posts(soup)
    except Exception as e:
        log_global(f"SupJav 搜尋失敗 ({query}): {e}")
        return []

def fetch_supjav_actresses(page=1):
    # 精選高人氣女優清單，並關聯至 SupJav 該女優的影片庫
    popular_actresses = [
        "三上悠亜", "河北彩花", "波多野結衣", "深田えいみ", "桃乃木かな", 
        "篠田ゆう", "桜空もも", "明里つむぎ", "相沢みなみ", "葵つかさ", 
        "橋本ありな", "涼森れむ", "神木麗", "森日向子", "楓カレン", 
        "五日市芽依", "七瀬アリス", "楓ふうあ", "白峰ミウ", "橘メアリー", 
        "楪カレン", "北野未奈", "天使もえ", "山手梨愛", "石川澪", "八木奈々"
    ]
    results = []
    for name in popular_actresses:
        avatar = get_actress_avatar(name)
        q = urllib.parse.quote(name)
        results.append({
            "name": name,
            "url": f"https://supjav.com/?s={q}",
            "avatar": avatar,
            "video_count": "熱門作品",
            "source": "SupJav"
        })
    return results

def fetch_supjav_actress_videos(actress_url, page=1):
    if "?s=" in actress_url or "&s=" in actress_url:
        q = urllib.parse.unquote(actress_url.split("?s=")[-1].split("&")[0])
        return search_supjav_videos(q, page)
    url = actress_url
    if page > 1:
        url = f"{url.rstrip('/')}/page/{page}"
    try:
        r = robust_browser_get(url, timeout=12)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        return parse_supjav_posts(soup)
    except Exception as e:
        log_global(f"SupJav 女優影片抓取失敗 ({actress_url}): {e}")
        return []


# ==================== 下載背景服務 ====================

def worker():
    log_global("UAV 下載器背景服務已啟動")

    while True:
        item = None
        with lock:
            queue = load_queue()
            for q in queue:
                if q.get("status") == "waiting":
                    item = q
                    break

        if not item:
            time.sleep(3)
            continue

        item_id = item["id"]
        url = item["url"]

        update_item(
            item_id,
            status="running",
            message="下載中",
            progress=0,
            started_at=now(),
            finished_at=""
        )
        append_item_log(item_id, f"開始下載：{url}")

        try:
            proc = subprocess.Popen(
                ["python", "-u", "-m", "uav_downloader.cli.headless", url],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            for line in proc.stdout:
                line = line.strip()
                if line:
                    append_item_log(item_id, line)

            rc = proc.wait()

            if rc == 0:
                clean_non_video_files(DOWNLOAD_DIR)
                update_item(
                    item_id,
                    status="done",
                    message="下載完成 (已放入下載資料夾)",
                    progress=100,
                    finished_at=now()
                )
                append_item_log(item_id, f"下載完成：{url}")
            else:
                update_item(
                    item_id,
                    status="failed",
                    message=f"下載失敗 (代碼 {rc})",
                    finished_at=now()
                )
                append_item_log(item_id, f"下載失敗：{url}，代碼={rc}")

        except Exception as e:
            update_item(
                item_id,
                status="failed",
                message=str(e),
                finished_at=now()
            )
            append_item_log(item_id, f"錯誤：{e}")

        time.sleep(2)


# ==================== Web 介面 HTML ====================

MAIN_PAGE_HTML = """
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>UAV 下載 ＆ 刮削整理中心</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<style>
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  background: #f4f6f9; margin: 20px; color: #2d3748;
}
.header-bar {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
}
h1 { margin: 0; font-size: 22px; color: #1a202c; }
.card {
  background: white; padding: 18px; margin-bottom: 18px; border-radius: 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
textarea {
  width: 100%; height: 85px; padding: 10px; font-size: 14px;
  border: 1px solid #cbd5e0; border-radius: 6px; box-sizing: border-box;
}
input[type="text"] {
  width: 100%; padding: 8px 10px; font-size: 14px;
  border: 1px solid #cbd5e0; border-radius: 6px; box-sizing: border-box;
}
button {
  padding: 8px 14px; font-size: 14px; border-radius: 6px; cursor: pointer; border: 0;
  transition: background 0.2s, transform 0.1s;
}
button:active { transform: scale(0.98); }
.btn-primary { background: #2b6cb0; color: white; }
.btn-primary:hover { background: #2c5282; }
.btn-success { background: #2f855a; color: white; }
.btn-success:hover { background: #276749; }
.btn-danger { background: #e53e3e; color: white; }
.btn-danger:hover { background: #c53030; }
.btn-secondary { background: #edf2f7; color: #4a5568; border: 1px solid #cbd5e0; }
.btn-secondary:hover { background: #e2e8f0; }

table { width: 100%; border-collapse: collapse; }
td, th { padding: 10px 8px; border-bottom: 1px solid #edf2f7; text-align: left; vertical-align: middle; }
th { background: #f7fafc; color: #4a5568; font-size: 13px; }

.waiting { color: #718096; font-weight: bold; }
.running { color: #3182ce; font-weight: bold; }
.done { color: #38a169; font-weight: bold; }
.failed { color: #e53e3e; font-weight: bold; }

.status-badge {
  display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;
}
.status-badge.running { background: #ebf8ff; color: #2b6cb0; }
.status-badge.done { background: #f0fff4; color: #276749; }
.status-badge.failed { background: #fff5f5; color: #c53030; }
.status-badge.waiting { background: #edf2f7; color: #4a5568; }

.progress-box { display: flex; align-items: center; gap: 8px; }
.progress-wrap {
  width: 120px; height: 10px; background: #e2e8f0; border-radius: 5px;
  overflow: hidden; display: inline-block; vertical-align: middle;
}
.progress-bar { height: 10px; width: 0%; background: #3182ce; transition: width 0.3s; }
.progress-num { font-size: 12px; font-weight: bold; color: #4a5568; min-width: 36px; }

.item-log {
  display: none; background: #1a202c; color: #edf2f7; padding: 10px;
  border-radius: 6px; max-height: 280px; overflow: auto; font-family: monospace; font-size: 12px;
}
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  background: #ebf8ff; color: #2b6cb0; font-size: 12px; font-weight: bold;
}
.source-tag {
  display: inline-block; padding: 1px 6px; border-radius: 4px;
  background: #feebc8; color: #c05621; font-size: 11px; margin-left: 4px;
}
.poster-thumb {
  width: 56px; height: 80px; object-fit: cover; border-radius: 4px;
  border: 1px solid #e2e8f0; background: #f7fafc; display: block;
}
.path-info { font-size: 13px; color: #718096; margin-top: 4px; word-break: break-all; }
.path-info code { background: #edf2f7; padding: 2px 6px; border-radius: 4px; color: #2d3748; word-break: break-all; }
.flex-between { display: flex; justify-content: space-between; align-items: center; }
.action-gap { display: flex; gap: 8px; flex-wrap: wrap; }

.rule-box {
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
  padding: 14px; margin-bottom: 14px;
}
.preset-links { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.btn-preset {
  background: #edf2f7; border: 1px solid #cbd5e0; padding: 3px 8px;
  font-size: 12px; border-radius: 4px; cursor: pointer; color: #2d3748;
}
.btn-preset:hover { background: #e2e8f0; color: #1a202c; }
.var-picker { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; align-items: center; }
.var-tag {
  background: #e2e8f0; border: 0; padding: 3px 8px; font-size: 11px;
  border-radius: 4px; cursor: pointer; color: #4a5568; font-weight: 500;
}
.var-tag:hover { background: #cbd5e0; color: #1a202c; }
.rule-preview {
  margin-top: 8px; font-size: 12px; color: #2b6cb0; background: #ebf8ff;
  padding: 6px 10px; border-radius: 6px; word-break: break-all;
}

.table-responsive { width: 100%; }
.mobile-only { display: none !important; }
.desktop-only { display: table-cell; }
tr.desktop-only { display: table-row; }
div.desktop-only { display: block; }

/* 行動端 RWD 排版設計 (螢幕小於等於 768px) */
@media (max-width: 768px) {
  body { margin: 10px; }
  .header-bar {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 12px;
  }
  .header-bar div {
    display: flex;
    justify-content: space-between;
    width: 100%;
    font-size: 14px;
  }
  h1 { font-size: 19px; }
  .card { padding: 14px; margin-bottom: 14px; border-radius: 10px; }

  .mobile-only { display: flex !important; }
  td.mobile-only { display: block !important; }
  .desktop-only { display: none !important; }
  tr.desktop-only { display: none !important; }
  td.desktop-only { display: none !important; }
  div.desktop-only { display: none !important; }

  /* 卡片頭部自適應排版 */
  .card-header-responsive {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
    margin-bottom: 12px;
  }
  .card-header-responsive h2 {
    font-size: 16px;
    margin: 0;
  }

  /* 待整理影片庫頂部 4 個操作按鈕轉為 2x2 網格，整齊好按 */
  .unscraped-top-actions {
    display: grid !important;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    width: 100%;
  }
  .unscraped-top-actions button {
    padding: 10px 6px;
    font-size: 13px;
    text-align: center;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .unscraped-top-actions button.btn-batch-del {
    grid-column: span 2;
  }

  /* 清空佇列按鈕與送出任務按鈕全寬化 */
  .queue-clear-btn, .btn-submit-task {
    width: 100%;
    padding: 10px;
    font-size: 14px;
    text-align: center;
  }

  /* 規則設定內部排版 */
  .rule-header-responsive {
    flex-direction: column;
    align-items: flex-start !important;
    gap: 6px;
  }

  /* ================= 手機端下載佇列卡片化 ================= */
  #queue-table thead { display: none; }
  #queue-table tbody tr.queue-row {
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 10px;
  }
  #queue-table tbody tr.queue-row > td {
    display: block;
    padding: 0;
    border: none;
  }
  #queue-table tbody tr.queue-log-row {
    display: block;
    margin-top: -6px;
    margin-bottom: 10px;
  }
  #queue-table tbody tr.queue-log-row > td {
    display: block;
    padding: 0;
    border: none;
  }

  .q-mobile-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
  }
  .q-mobile-time {
    font-size: 12px;
    color: #718096;
  }
  .q-mobile-actions {
    display: flex;
    gap: 6px;
  }
  .q-mobile-actions button {
    padding: 4px 10px;
    font-size: 12px;
  }

  .queue-row .progress-box {
    width: 100%;
  }
  .queue-row .progress-wrap {
    flex: 1;
    height: 12px;
  }
  .queue-row .progress-bar {
    height: 12px;
  }

  .q-msg-box {
    font-size: 13px;
    font-weight: 500;
    color: #2d3748;
    line-height: 1.4;
  }

  .q-url-box {
    font-size: 12px;
    color: #4a5568;
    background: #edf2f7;
    padding: 6px 8px;
    border-radius: 4px;
    word-break: break-all;
    font-family: monospace;
  }

  /* ================= 手機端待整理影片庫卡片化 ================= */
  #unscraped-table thead { display: none; }
  #unscraped-table tbody tr.unscraped-row {
    display: flex;
    flex-direction: column;
    gap: 10px;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 12px;
    margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
  }
  #unscraped-table tbody tr.unscraped-row > td {
    display: block;
    padding: 0;
    border: none;
  }

  /* 手機專屬快速全選列 */
  .mobile-select-bar {
    display: flex !important;
    justify-content: space-between;
    align-items: center;
    background: #edf2f7;
    padding: 8px 12px;
    border-radius: 6px;
    margin-bottom: 10px;
    font-size: 13px;
  }

  /* 影片卡片頂部：勾選 + 番號 + 狀態 + 大小 */
  .u-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding-bottom: 6px;
    border-bottom: 1px dashed #e2e8f0;
  }
  .u-card-header-left {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .u-card-header-left input[type="checkbox"] {
    width: 18px;
    height: 18px;
  }

  /* 影片卡片主體：海報 + 內容 */
  .u-card-body {
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  .u-card-poster {
    flex-shrink: 0;
  }
  .u-card-poster .poster-thumb {
    width: 65px;
    height: 92px;
    border-radius: 6px;
  }
  .u-card-details {
    flex: 1;
    min-width: 0;
  }
  .u-card-title {
    font-size: 14px;
    font-weight: 600;
    color: #2b6cb0;
    line-height: 1.4;
    word-break: break-word;
  }
  .u-card-meta {
    font-size: 12px;
    color: #718096;
    margin-top: 4px;
    line-height: 1.3;
  }
  .u-card-target {
    font-size: 11px;
    color: #2f855a;
    margin-top: 4px;
    word-break: break-all;
    background: #f0fff4;
    padding: 3px 6px;
    border-radius: 4px;
  }

  /* 影片卡片底部按鈕均分全寬 */
  .u-card-ops {
    display: grid !important;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    width: 100%;
    margin-top: 2px;
  }
  .u-card-ops button {
    padding: 8px 6px;
    font-size: 13px;
    text-align: center;
  }

  /* 行動端影音探索網格為 2 欄 */
  .media-grid {
    grid-template-columns: 1fr 1fr !important;
    gap: 8px !important;
  }
  .media-card-title {
    font-size: 12px !important;
    -webkit-line-clamp: 2 !important;
  }
  .actress-grid {
    grid-template-columns: repeat(3, 1fr) !important;
    gap: 8px !important;
  }
}

/* ================= 線上探索專區樣式 ================= */
.explore-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 2px solid #edf2f7;
  padding-bottom: 8px;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 10px;
}
.explore-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.tab-btn {
  padding: 8px 14px;
  font-size: 14px;
  font-weight: 600;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: #4a5568;
  cursor: pointer;
  transition: all 0.2s;
}
.tab-btn:hover { background: #edf2f7; color: #2b6cb0; }
.tab-btn.active {
  background: #ebf8ff;
  color: #2b6cb0;
  border-color: #bee3f8;
}

.source-switcher {
  display: flex;
  background: #edf2f7;
  padding: 3px;
  border-radius: 8px;
}
.source-btn {
  padding: 5px 12px;
  font-size: 12px;
  font-weight: bold;
  border-radius: 6px;
  border: 0;
  background: transparent;
  color: #718096;
  cursor: pointer;
  transition: all 0.2s;
}
.source-btn.active {
  background: white;
  color: #2d3748;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}

.category-bar {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 8px;
  margin-bottom: 14px;
  -webkit-overflow-scrolling: touch;
}
.cat-pill {
  padding: 6px 12px;
  font-size: 13px;
  font-weight: 500;
  border-radius: 20px;
  background: #f7fafc;
  color: #4a5568;
  border: 1px solid #e2e8f0;
  white-space: nowrap;
  cursor: pointer;
  transition: all 0.2s;
}
.cat-pill:hover { background: #edf2f7; }
.cat-pill.active {
  background: #2b6cb0;
  color: white;
  border-color: #2b6cb0;
}

.batch-floating-bar {
  display: none;
  background: #2b6cb0;
  color: white;
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 12px;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 4px 12px rgba(43,108,176,0.25);
}

.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 14px;
}
.media-card {
  background: white;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  transition: transform 0.2s, box-shadow 0.2s;
  position: relative;
}
.media-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0,0,0,0.08);
}
.media-thumb-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #1a202c;
  overflow: hidden;
}
.media-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.media-card-chk {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 22px;
  height: 22px;
  cursor: pointer;
  accent-color: #2b6cb0;
  z-index: 2;
}
.media-duration {
  position: absolute;
  bottom: 6px;
  right: 6px;
  background: rgba(0,0,0,0.75);
  color: white;
  font-size: 11px;
  padding: 2px 5px;
  border-radius: 4px;
  font-weight: 500;
}
.media-source-tag {
  position: absolute;
  top: 8px;
  left: 8px;
  background: rgba(0,0,0,0.65);
  backdrop-filter: blur(4px);
  color: #fff;
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
}
.media-card-body {
  padding: 10px;
  display: flex;
  flex-direction: column;
  flex: 1;
}
.media-card-code {
  font-size: 11px;
  font-weight: bold;
  color: #2b6cb0;
  margin-bottom: 4px;
  text-overflow: ellipsis;
  overflow: hidden;
  white-space: nowrap;
}
.media-card-title {
  font-size: 13px;
  color: #2d3748;
  font-weight: 600;
  line-height: 1.35;
  margin-bottom: 8px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  flex: 1;
}
.btn-quick-download {
  width: 100%;
  padding: 6px 10px;
  font-size: 12px;
  font-weight: 600;
  border-radius: 6px;
  background: #ebf8ff;
  color: #2b6cb0;
  border: 1px solid #bee3f8;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-quick-download:hover {
  background: #2b6cb0;
  color: white;
}
.btn-quick-download.added {
  background: #f0fff4;
  color: #276749;
  border-color: #9ae6b4;
  pointer-events: none;
}

/* 女優清單網格 */
.actress-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 12px;
}
.actress-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 12px 8px;
  text-align: center;
  cursor: pointer;
  transition: all 0.2s;
}
.actress-card:hover {
  background: #ebf8ff;
  border-color: #bee3f8;
  transform: translateY(-2px);
}
.actress-avatar {
  width: 72px;
  height: 72px;
  border-radius: 50%;
  object-fit: cover;
  margin: 0 auto 8px auto;
  display: block;
  border: 2px solid #e2e8f0;
  background: #edf2f7;
}
.actress-avatar-initial {
  display: flex !important;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: #ffffff;
  font-size: 26px;
  font-weight: 700;
  text-shadow: 0 1px 2px rgba(0,0,0,0.3);
  border: 2px solid #cbd5e0;
}
.actress-name {
  font-size: 13px;
  font-weight: bold;
  color: #2d3748;
  margin-bottom: 2px;
}
.actress-count {
  font-size: 11px;
  color: #718096;
}

/* Toast 提示 */
.toast-msg {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #2b6cb0;
  color: white;
  padding: 12px 20px;
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.2);
  font-size: 14px;
  font-weight: bold;
  z-index: 9999;
  opacity: 0;
  transform: translateY(10px);
  transition: all 0.3s;
  pointer-events: none;
}
.toast-msg.show {
  opacity: 1;
  transform: translateY(0);
}
</style>
</head>
<body>

<div class="header-bar">
  <h1>UAV 下載 ＆ 刮削整理中心</h1>
  <div>
    <span>👤 <strong>{{ current_user }}</strong></span>
    <a href="/logout" style="color:#e53e3e;margin-left:12px;text-decoration:none;">登出</a>
  </div>
</div>

<!-- 命名規則與歸檔設定 (JavHelper Pro 核心) -->
<div class="card">
  <div class="flex-between" style="cursor:pointer;" onclick="toggleRuleSection()">
    <h2 style="margin: 0; font-size: 18px;">⚙️ 歸檔與命名規則設定 (點擊展開 / 收合)</h2>
    <span id="rule-toggle-icon" style="color:#718096;font-size:14px;">▼ 展開設定</span>
  </div>

  <div id="rule-section" style="display: none; margin-top: 14px;">
    <!-- 單女優規則 -->
    <div class="rule-box">
      <div class="flex-between rule-header-responsive">
        <strong>單女優命名規則</strong>
        <div class="preset-links">
          <span style="font-size:12px;color:#718096;">推薦範本：</span>
          <button type="button" class="btn-preset" onclick="setSinglePreset('standard')">標準分層</button>
          <button type="button" class="btn-preset" onclick="setSinglePreset('flat')">女優平鋪</button>
          <button type="button" class="btn-preset" onclick="setSinglePreset('number')">番號優先</button>
          <button type="button" class="btn-preset" onclick="setSinglePreset('clean')">簡潔乾淨</button>
        </div>
      </div>
      <input type="text" id="single-rule-input" oninput="updatePreviews()" style="margin-top:6px;">
      <div class="var-picker">
        <span style="font-size:12px;color:#718096;">插入變數：</span>
        <button type="button" class="var-tag" onclick="insertVar('single-rule-input', '%Name%')">+ %Name% (女優)</button>
        <button type="button" class="var-tag" onclick="insertVar('single-rule-input', '%ProductNumber%')">+ %ProductNumber% (番號)</button>
        <button type="button" class="var-tag" onclick="insertVar('single-rule-input', '%Title%')">+ %Title% (片名)</button>
        <button type="button" class="var-tag" onclick="insertVar('single-rule-input', '%ReleaseDate%')">+ %ReleaseDate% (發行日)</button>
        <button type="button" class="var-tag" onclick="insertVar('single-rule-input', '/')">+ / (子目錄)</button>
        <button type="button" class="var-tag" onclick="insertVar('single-rule-input', ' - ')">+ - (連接)</button>
      </div>
      <div class="rule-preview">
        <strong>即時預覽：</strong><span id="single-preview"></span>
      </div>
    </div>

    <!-- 多女優規則 -->
    <div class="rule-box">
      <div class="flex-between rule-header-responsive">
        <strong>多女優命名規則 (出演大於 1 位時套用)</strong>
        <div class="preset-links">
          <span style="font-size:12px;color:#718096;">推薦範本：</span>
          <button type="button" class="btn-preset" onclick="setMultiPreset('count')">人數分層</button>
          <button type="button" class="btn-preset" onclick="setMultiPreset('standard')">女優合併</button>
          <button type="button" class="btn-preset" onclick="setMultiPreset('multi_dir')">多人集中</button>
        </div>
      </div>
      <input type="text" id="multi-rule-input" oninput="updatePreviews()" style="margin-top:6px;">
      <div class="var-picker">
        <span style="font-size:12px;color:#718096;">插入變數：</span>
        <button type="button" class="var-tag" onclick="insertVar('multi-rule-input', '%ActorCount%')">+ %ActorCount% (人數目錄)</button>
        <button type="button" class="var-tag" onclick="insertVar('multi-rule-input', '%Name%')">+ %Name% (多女優)</button>
        <button type="button" class="var-tag" onclick="insertVar('multi-rule-input', '%ProductNumber%')">+ %ProductNumber% (番號)</button>
        <button type="button" class="var-tag" onclick="insertVar('multi-rule-input', '%Title%')">+ %Title% (片名)</button>
        <button type="button" class="var-tag" onclick="insertVar('multi-rule-input', '%ReleaseDate%')">+ %ReleaseDate% (發行日)</button>
        <button type="button" class="var-tag" onclick="insertVar('multi-rule-input', '/')">+ / (子目錄)</button>
      </div>
      <div class="rule-preview">
        <strong>即時預覽：</strong><span id="multi-preview"></span>
      </div>
    </div>

    <div style="text-align: right;">
      <button class="btn-primary btn-submit-task" onclick="saveSettingsToServer()">💾 儲存命名設定</button>
    </div>
  </div>
</div>

<!-- 線上影音探索 ＆ 一鍵下載中心 -->
<div class="card">
  <div class="explore-nav">
    <div class="explore-tabs">
      <button type="button" class="tab-btn active" onclick="switchExploreTab('trending', this)">🔥 熱門排行</button>
      <button type="button" class="tab-btn" onclick="switchExploreTab('actresses', this)">💃 人氣女優</button>
      <button type="button" class="tab-btn" onclick="switchExploreTab('search', this)">🔍 關鍵字搜尋</button>
      <button type="button" class="tab-btn" onclick="switchExploreTab('manual', this)">📋 傳統貼網址</button>
    </div>
    <div class="source-switcher">
      <button type="button" class="source-btn active" id="src-jable" onclick="setExploreSource('jable')">JableTV</button>
      <button type="button" class="source-btn" id="src-missav" onclick="setExploreSource('missav')">MissAV</button>
      <button type="button" class="source-btn" id="src-supjav" onclick="setExploreSource('supjav')">SupJav</button>
    </div>
  </div>

  <!-- 批次下載操作浮動列 -->
  <div id="batch-bar" class="batch-floating-bar">
    <div style="font-weight:600;"><span id="batch-selected-count">0</span> 部影片已勾選</div>
    <div style="display:flex;gap:8px;">
      <button type="button" class="btn-success" style="padding:6px 12px;font-size:13px;" onclick="addSelectedToQueue()">📦 批次加入下載佇列</button>
      <button type="button" class="btn-secondary" style="padding:6px 10px;font-size:13px;" onclick="clearExploreSelection()">✕ 取消</button>
    </div>
  </div>

  <!-- 1. 熱門排行面板 -->
  <div id="panel-trending">
    <div class="category-bar">
      <button type="button" class="cat-pill active" onclick="setCategory('today-hot', this)">🔥 今日熱門</button>
      <button type="button" class="cat-pill" onclick="setCategory('weekly-hot', this)">🌟 本週熱門</button>
      <button type="button" class="cat-pill" onclick="setCategory('monthly-hot', this)">🏆 本月熱門</button>
      <button type="button" class="cat-pill" onclick="setCategory('latest', this)">🆕 最新上架</button>
      <button type="button" class="cat-pill" onclick="setCategory('chinese', this)">🀄 中文字幕</button>
      <button type="button" class="cat-pill" onclick="setCategory('uncensored', this)">🔓 無碼流出</button>
    </div>
    <div id="trending-grid" class="media-grid">
      <div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">載入影片中...</div>
    </div>
  </div>

  <!-- 2. 人氣女優面板 -->
  <div id="panel-actresses" style="display: none;">
    <div id="actress-breadcrumb" style="display:none;margin-bottom:12px;align-items:center;justify-content:space-between;background:#ebf8ff;padding:8px 12px;border-radius:8px;">
      <div style="font-weight:bold;color:#2b6cb0;">🌸 當前女優：<span id="current-actress-title"></span></div>
      <button type="button" class="btn-secondary" style="padding:3px 8px;font-size:12px;" onclick="backToActressList()">✕ 返回女優榜</button>
    </div>
    <div id="actress-grid" class="actress-grid">
      <div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">載入女優列表中...</div>
    </div>
    <div id="actress-videos-grid" class="media-grid" style="display:none;"></div>
  </div>

  <!-- 3. 關鍵字搜尋面板 -->
  <div id="panel-search" style="display: none;">
    <div style="display:flex;gap:8px;margin-bottom:14px;">
      <input type="text" id="explore-search-input" placeholder="輸入番號 (如 SSIS-001)、女優名或關鍵字..." onkeydown="if(event.key==='Enter') executeExploreSearch()" style="flex:1;">
      <button type="button" class="btn-primary" onclick="executeExploreSearch()" style="min-width:80px;">🔍 搜尋</button>
    </div>
    <div id="search-grid" class="media-grid">
      <div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">請在上方輸入關鍵字開始搜尋</div>
    </div>
  </div>

  <!-- 4. 傳統手動貼網址面板 -->
  <div id="panel-manual" style="display: none;">
    <div class="path-info">下載目標：<code>{{ host_download_dir }}</code>（僅下載純影像檔）</div>
    <form method="post" action="/add" style="margin-top: 10px;">
      <textarea name="urls" placeholder="貼上 Jable / MissAV 網址，一行一個..." required></textarea>
      <div style="margin-top: 10px;">
        <button type="submit" class="btn-primary btn-submit-task">加入下載佇列</button>
      </div>
    </form>
  </div>
</div>

<!-- 浮動提示 Toast -->
<div id="toast" class="toast-msg"></div>

<!-- 下載狀態管理 -->
<div class="card">
  <div class="card-header-responsive">
    <h2 style="margin: 0;">下載佇列進度</h2>
    <div>
      <button class="btn-secondary queue-clear-btn" onclick="clearFinishedQueue()">🗑️ 一鍵清空已完成與失敗項目</button>
    </div>
  </div>
  <div class="table-responsive">
    <table id="queue-table">
      <thead>
        <tr>
          <th>狀態</th>
          <th>進度</th>
          <th>網址</th>
          <th>訊息</th>
          <th>開始時間</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="queue"></tbody>
    </table>
  </div>
</div>

<!-- 手動刮削與入庫管理 -->
<div class="card">
  <div class="card-header-responsive">
    <div>
      <h2 style="margin: 0;">待整理影片庫 (支援子資料夾掃描 ＆ 自訂命名規則)</h2>
      <div class="path-info">整理庫基底：<code>{{ host_media_dir }}/</code></div>
    </div>
    <div class="action-gap unscraped-top-actions">
      <button class="btn-secondary" onclick="refreshUnscraped()">🔄 重新掃描資料夾</button>
      <button class="btn-primary" onclick="scrapeAllPreview()">🔍 全部刮削預覽</button>
      <button class="btn-success" onclick="commitSelected()">📦 勾選批次入庫</button>
      <button class="btn-success" onclick="commitAll()">🚀 一鍵全部入庫</button>
      <button class="btn-danger btn-batch-del" onclick="deleteSelectedUnscraped()">🗑️ 勾選批次刪除</button>
    </div>
  </div>

  <!-- 手機專屬快速全選工具列 -->
  <div class="mobile-select-bar mobile-only">
    <div style="display:flex;align-items:center;gap:6px;">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;">
        <input type="checkbox" id="select-all-mobile" onclick="toggleSelectAll(this)">
        <strong>全選</strong>
      </label>
      <span id="select-summary-mobile" style="color:#718096;font-size:12px;"></span>
    </div>
    <button class="btn-danger" style="padding:4px 8px;font-size:12px;" onclick="deleteSelectedUnscraped()">🗑️ 刪除已選</button>
  </div>

  <div class="table-responsive">
    <table id="unscraped-table">
      <thead>
        <tr>
          <th style="width: 30px;"><input type="checkbox" id="select-all" onclick="toggleSelectAll(this)"></th>
          <th style="width: 60px;">海報</th>
          <th>來源影片</th>
          <th>識別番號</th>
          <th>刮削預覽 ＆ 預計入庫路徑</th>
          <th>狀態</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="unscraped-list">
        <tr><td colspan="7" style="color:#718096;text-align:center;padding:20px;">載入中...</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
const HOST_MEDIA_DIR = {{ host_media_dir|tojson }};
let currentSettings = {};
const openedLogs = new Set();

function escapeHtml(t) {
  return String(t || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#039;");
}

function toggleRuleSection() {
  const el = document.getElementById("rule-section");
  const icon = document.getElementById("rule-toggle-icon");
  if (el.style.display === "none") {
    el.style.display = "block";
    icon.innerText = "▲ 收合設定";
  } else {
    el.style.display = "none";
    icon.innerText = "▼ 展開設定";
  }
}

async function loadSettingsFromServer() {
  const res = await fetch("/api/settings");
  currentSettings = await res.json();
  document.getElementById("single-rule-input").value = currentSettings.naming_rule;
  document.getElementById("multi-rule-input").value = currentSettings.multi_naming_rule;
  updatePreviews();
}

function updatePreviews() {
  const singleRule = document.getElementById("single-rule-input").value;
  const multiRule = document.getElementById("multi-rule-input").value;

  const sPrev = singleRule
    .replaceAll("%Name%", "三上悠亞")
    .replaceAll("%ProductNumber%", "SSIS-001")
    .replaceAll("%Title%", "新人初登場")
    .replaceAll("%ReleaseDate%", "2021-08-19")
    .replaceAll("%HasSubtitle%", "[中字]");
  document.getElementById("single-preview").innerText = HOST_MEDIA_DIR + "/" + sPrev + ".mp4";

  const mPrev = multiRule
    .replaceAll("%ActorCount%", "2人")
    .replaceAll("%Name%", "三上悠亞 橋本有菜")
    .replaceAll("%ProductNumber%", "SSIS-001")
    .replaceAll("%Title%", "奇蹟的夢幻共演")
    .replaceAll("%ReleaseDate%", "2021-08-19")
    .replaceAll("%HasSubtitle%", "");
  document.getElementById("multi-preview").innerText = HOST_MEDIA_DIR + "/" + mPrev + ".mp4";
}

function setSinglePreset(type) {
  if (type === 'standard') {
    document.getElementById("single-rule-input").value = "%Name%/%ProductNumber%/%HasSubtitle%[%ReleaseDate%] - [%ProductNumber%] - [%Title%]";
  } else if (type === 'flat') {
    document.getElementById("single-rule-input").value = "%Name%/%HasSubtitle%[%ProductNumber%] %Title%";
  } else if (type === 'number') {
    document.getElementById("single-rule-input").value = "%ProductNumber%/%HasSubtitle%[%ProductNumber%] - %Name% - %Title%";
  } else if (type === 'clean') {
    document.getElementById("single-rule-input").value = "%Name%/%ProductNumber%/%ProductNumber%";
  }
  updatePreviews();
}

function setMultiPreset(type) {
  if (type === 'count') {
    document.getElementById("multi-rule-input").value = "%ActorCount%/%Name%/%ProductNumber%/%HasSubtitle%[%ReleaseDate%] - [%ProductNumber%] - [%Title%]";
  } else if (type === 'standard') {
    document.getElementById("multi-rule-input").value = "%Name%/%ProductNumber%/%HasSubtitle%[%ReleaseDate%] - [%ProductNumber%] - [%Title%]";
  } else if (type === 'multi_dir') {
    document.getElementById("multi-rule-input").value = "共演片/%ProductNumber%/%HasSubtitle%[%ReleaseDate%] - [%ProductNumber%] - [%Title%]";
  }
  updatePreviews();
}

function insertVar(inputId, varStr) {
  const el = document.getElementById(inputId);
  const start = el.selectionStart || el.value.length;
  const end = el.selectionEnd || el.value.length;
  el.value = el.value.substring(0, start) + varStr + el.value.substring(end);
  el.focus();
  el.selectionStart = el.selectionEnd = start + varStr.length;
  updatePreviews();
}

async function saveSettingsToServer() {
  const payload = {
    naming_rule: document.getElementById("single-rule-input").value,
    multi_naming_rule: document.getElementById("multi-rule-input").value
  };
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  alert(data.message);
  currentSettings = data.settings;
  refreshUnscraped();
}

function toggleLog(id) {
  const el = document.getElementById("log-" + id);
  if (!el) return;
  if (openedLogs.has(id)) {
    openedLogs.delete(id);
    el.style.display = "none";
  } else {
    openedLogs.add(id);
    el.style.display = "block";
  }
}

async function refreshQueue() {
  const r = await fetch("/api/status");
  if (r.status === 401) { location.reload(); return; }
  const data = await r.json();
  const tbody = document.getElementById("queue");
  tbody.innerHTML = "";

  if (data.queue.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:#a0aec0;text-align:center;padding:16px;">目前沒有下載中的項目</td></tr>`;
    return;
  }

  for (const item of data.queue) {
    const progress = Math.min(100, Math.max(0, Number(item.progress || 0)));
    const logs = (item.logs || []).slice().reverse().join("\\n");
    const id = String(item.id);
    const safeId = escapeHtml(id);
    const isOpen = openedLogs.has(id);
    const isRunning = item.status === "running";

    const tr = document.createElement("tr");
    tr.className = "queue-row";
    tr.innerHTML = `
      <!-- 手機端專用頂部標頭 (狀態、時間、操作) -->
      <td class="mobile-only" style="width:100%;">
        <div class="q-mobile-header">
          <div>
            <span class="status-badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
            <span class="q-mobile-time" style="margin-left:6px;">${escapeHtml(item.started_at)}</span>
          </div>
          <div class="q-mobile-actions">
            <button class="btn-secondary" onclick="toggleLog('${safeId}')">${isOpen ? '收合' : 'Log'}</button>
            <button class="btn-danger" ${isRunning ? 'disabled' : ''} onclick="deleteQueueItem('${safeId}')">刪除</button>
          </div>
        </div>
      </td>

      <!-- 桌機端狀態欄 -->
      <td class="desktop-only"><span class="status-badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td>

      <!-- 進度欄位 (桌機與手機共用，手機自動延展) -->
      <td>
        <div class="progress-box">
          <span class="progress-wrap"><span class="progress-bar" style="width: ${progress}%;"></span></span>
          <span class="progress-num">${progress}%</span>
        </div>
      </td>

      <!-- 訊息 (手機端排在進度下方) -->
      <td class="mobile-only" style="width:100%;">
        <div class="q-msg-box">${escapeHtml(item.message)}</div>
      </td>

      <!-- 網址 (桌機端正常欄位 / 手機端以整行色塊展開，絕不擠壓) -->
      <td class="desktop-only" style="max-width:300px;word-break:break-all;">${escapeHtml(item.url)}</td>
      <td class="mobile-only" style="width:100%;">
        <div class="q-url-box">🔗 ${escapeHtml(item.url)}</div>
      </td>

      <!-- 桌機端訊息、開始時間、操作欄位 -->
      <td class="desktop-only">${escapeHtml(item.message)}</td>
      <td class="desktop-only">${escapeHtml(item.started_at)}</td>
      <td class="desktop-only">
        <button class="btn-secondary" style="padding:4px 8px;" onclick="toggleLog('${safeId}')">${isOpen ? '收合' : 'Log'}</button>
        <button class="btn-danger" style="padding:4px 8px;margin-left:4px;" ${isRunning ? 'disabled' : ''} onclick="deleteQueueItem('${safeId}')">刪除</button>
      </td>
    `;
    tbody.appendChild(tr);

    const logTr = document.createElement("tr");
    logTr.className = "queue-log-row";
    logTr.innerHTML = `
      <td colspan="6" style="padding:0;">
        <pre class="item-log" id="log-${safeId}" style="display:${isOpen ? 'block' : 'none'};margin-top:4px;">${escapeHtml(logs)}</pre>
      </td>
    `;
    tbody.appendChild(logTr);
  }
}

async function deleteQueueItem(id) {
  if (!confirm("確定要刪除這筆下載紀錄？")) return;
  await fetch("/delete/" + encodeURIComponent(id), { method: "POST" });
  refreshQueue();
}

async function clearFinishedQueue() {
  if (!confirm("確定要清空所有已完成或失敗的下載項目？")) return;
  await fetch("/api/clear_finished_queue", { method: "POST" });
  refreshQueue();
}

async function refreshUnscraped() {
  const r = await fetch("/api/unscraped");
  if (r.status === 401) return;
  const data = await r.json();
  const tbody = document.getElementById("unscraped-list");
  tbody.innerHTML = "";

  if (!data.files || data.files.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="color:#718096;text-align:center;padding:24px;">🎉 目前沒有等待整理的影片檔案</td></tr>`;
    syncSelectSummary();
    return;
  }

  for (const f of data.files) {
    const isReady = f.status === "preview_ready";
    const posterImg = f.poster_b64 ? `<img src="${f.poster_b64}" class="poster-thumb">` : `<div class="poster-thumb" style="display:flex;align-items:center;justify-content:center;color:#a0aec0;font-size:11px;">無圖</div>`;
    
    let infoHtml = `<span style="color:#a0aec0;">尚未刮削，點擊「刮削預覽」抓取資料</span>`;
    if (isReady && f.meta) {
      infoHtml = `
        <div style="font-weight:600;color:#2b6cb0;">
          ${escapeHtml(f.meta.title)}
          <span class="source-tag">${escapeHtml(f.meta.source || 'JavBus')}</span>
        </div>
        <div style="font-size:11px;color:#a0aec0;margin-top:1px;">原名：${escapeHtml(f.meta.original_title || '-')}</div>
        <div style="font-size:12px;color:#718096;margin-top:2px;">
          女優：<strong>${escapeHtml(f.meta.actors.join(', ') || '素人')}</strong> ｜ 日期：${escapeHtml(f.meta.release_date || '-')}
        </div>
        <div style="font-size:11px;color:#2f855a;margin-top:3px;word-break:break-all;">
          <strong>預計目標：</strong>${escapeHtml(HOST_MEDIA_DIR + '/' + f.target_rel)}
        </div>
      `;
    }

    const tr = document.createElement("tr");
    tr.className = "unscraped-row";
    tr.innerHTML = `
      <!-- 手機端專屬精美卡片佈局 -->
      <td class="mobile-only" style="width:100%;">
        <!-- 頂部列：核取方塊 + 番號 + 狀態 + 大小 + 刪除按鈕 -->
        <div class="u-card-header">
          <div class="u-card-header-left">
            <input type="checkbox" class="file-chk" value="${escapeHtml(f.rel_path)}" ${isReady ? 'checked' : ''} onchange="onCheckboxChange(this, '${escapeHtml(f.rel_path)}')">
            <span class="badge">${escapeHtml(f.movie_id)}</span>
            <span style="font-size:12px;color:#718096;">${f.size_mb} MB</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <span class="${isReady ? 'done' : 'waiting'}" style="font-size:12px;">${isReady ? '✅ 預覽完成' : '⏳ 待刮削'}</span>
            <button class="btn-danger" style="padding:2px 8px;font-size:11px;" onclick="deleteSingleUnscraped('${escapeHtml(f.rel_path)}')">🗑️ 刪除</button>
          </div>
        </div>

        <!-- 卡片主體：海報 + 刮削詳細資訊 -->
        <div class="u-card-body" style="margin-top:8px;">
          <div class="u-card-poster">
            ${posterImg}
          </div>
          <div class="u-card-details">
            ${isReady && f.meta ? `
              <div class="u-card-title">
                ${escapeHtml(f.meta.title)}
                <span class="source-tag">${escapeHtml(f.meta.source || 'JavBus')}</span>
              </div>
              <div class="u-card-meta">
                ${f.meta.original_title ? `<div>原名：${escapeHtml(f.meta.original_title)}</div>` : ''}
                <div>女優：<strong style="color:#2d3748;">${escapeHtml(f.meta.actors.join(', ') || '素人')}</strong></div>
                <div>日期：${escapeHtml(f.meta.release_date || '-')}</div>
              </div>
              <div class="u-card-target">
                <strong>目標：</strong>${escapeHtml(HOST_MEDIA_DIR + '/' + f.target_rel)}
              </div>
            ` : `
              <div class="u-card-title" style="color:#4a5568;">${escapeHtml(f.rel_path)}</div>
              <div class="u-card-meta" style="color:#a0aec0;margin-top:4px;">
                尚未刮削，點擊下方「刮削預覽」抓取資料
              </div>
            `}
          </div>
        </div>

        <!-- 底部按鈕：雙按鈕均分 50% 寬度，極易觸控 -->
        <div class="u-card-ops" style="margin-top:10px;">
          <button class="btn-primary" onclick="previewSingle(this, '${escapeHtml(f.rel_path)}')">🔍 刮削預覽</button>
          <button class="btn-success" ${!isReady ? 'disabled' : ''} onclick="commitSingle('${escapeHtml(f.rel_path)}')">📦 確認入庫</button>
        </div>
      </td>

      <!-- 桌機端 7 欄標準表格 -->
      <td class="desktop-only"><input type="checkbox" class="file-chk" value="${escapeHtml(f.rel_path)}" ${isReady ? 'checked' : ''} onchange="onCheckboxChange(this, '${escapeHtml(f.rel_path)}')"></td>
      <td class="desktop-only">${posterImg}</td>
      <td class="desktop-only"><strong>${escapeHtml(f.rel_path)}</strong><br><span style="font-size:12px;color:#718096;">${f.size_mb} MB</span></td>
      <td class="desktop-only"><span class="badge">${escapeHtml(f.movie_id)}</span></td>
      <td class="desktop-only">${infoHtml}</td>
      <td class="desktop-only">
        <span class="${isReady ? 'done' : 'waiting'}">${isReady ? '✅ 預覽完成' : '⏳ 待刮削'}</span>
      </td>
      <td class="desktop-only">
        <div class="action-gap">
          <button class="btn-primary" style="padding:6px 10px;" onclick="previewSingle(this, '${escapeHtml(f.rel_path)}')">🔍 刮削預覽</button>
          <button class="btn-success" style="padding:6px 10px;" ${!isReady ? 'disabled' : ''} onclick="commitSingle('${escapeHtml(f.rel_path)}')">📦 確認入庫</button>
          <button class="btn-danger" style="padding:6px 10px;" onclick="deleteSingleUnscraped('${escapeHtml(f.rel_path)}')">🗑️ 刪除</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  }
  syncSelectSummary();
}

function onCheckboxChange(el, path) {
  document.querySelectorAll(`.file-chk[value="${CSS.escape(path)}"]`).forEach(c => c.checked = el.checked);
  syncSelectSummary();
}

function toggleSelectAll(master) {
  const isChecked = master.checked;
  document.querySelectorAll(".file-chk").forEach(c => c.checked = isChecked);
  const m1 = document.getElementById("select-all");
  const m2 = document.getElementById("select-all-mobile");
  if (m1) m1.checked = isChecked;
  if (m2) m2.checked = isChecked;
  syncSelectSummary();
}

function syncSelectSummary() {
  const allChecked = Array.from(document.querySelectorAll(".file-chk:checked")).map(c => c.value);
  const uniqueCount = new Set(allChecked).size;
  const allValues = Array.from(document.querySelectorAll(".file-chk")).map(c => c.value);
  const totalCount = new Set(allValues).size;
  const el = document.getElementById("select-summary-mobile");
  if (el) {
    el.innerText = totalCount > 0 ? `已選 ${uniqueCount} / ${totalCount} 部` : "";
  }
}

async function previewSingle(btnEl, rel_path) {
  const btn = btnEl || (window.event ? window.event.target : null);
  if (btn) {
    btn.disabled = true;
    btn.innerText = "刮削中...";
  }
  try {
    const res = await fetch("/api/scrape_preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ rel_path })
    });
    const data = await res.json();
    if (!data.ok) alert(data.message);
  } catch(e) {
    alert("連線失敗: " + e);
  }
  refreshUnscraped();
}

async function scrapeAllPreview() {
  if (!confirm("確定要對所有影片執行多站備援刮削、繁中翻譯並載入海報？")) return;
  const res = await fetch("/api/scrape_all_preview", { method: "POST" });
  const data = await res.json();
  alert(data.message);
  refreshUnscraped();
}

async function commitSingle(rel_path) {
  if (!confirm(`確定要將【${rel_path}】與其海報資料寫入整理資料庫？`)) return;
  const res = await fetch("/api/commit_organize", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ rel_paths: [rel_path] })
  });
  const data = await res.json();
  alert(data.message);
  refreshUnscraped();
}

async function commitSelected() {
  const selected = Array.from(new Set(Array.from(document.querySelectorAll(".file-chk:checked")).map(c => c.value)));
  if (selected.length === 0) {
    alert("請先勾選要入庫的項目！");
    return;
  }
  if (!confirm(`確定要將已勾選的 ${selected.length} 部影片正式入庫整理？`)) return;
  const res = await fetch("/api/commit_organize", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ rel_paths: selected })
  });
  const data = await res.json();
  alert(data.message);
  refreshUnscraped();
}

async function commitAll() {
  const allFiles = Array.from(new Set(Array.from(document.querySelectorAll(".file-chk")).map(c => c.value)));
  if (allFiles.length === 0) {
    alert("目前沒有可入庫的項目！");
    return;
  }
  if (!confirm(`確定要將全部 ${allFiles.length} 部影片執行入庫整理？(未預覽者將自動多站刮削)`)) return;
  const res = await fetch("/api/commit_organize", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ rel_paths: allFiles, auto_scrape: true })
  });
  const data = await res.json();
  alert(data.message);
  refreshUnscraped();
}

async function deleteSingleUnscraped(rel_path) {
  if (!confirm(`確定要刪除待整理影片【${rel_path}】？\n⚠️ 此動作將永久刪除下載資料夾中的實體影片檔案，無法復原！`)) return;
  try {
    const res = await fetch("/api/delete_unscraped", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ rel_paths: [rel_path] })
    });
    const data = await res.json();
    alert(data.message);
  } catch(e) {
    alert("刪除失敗: " + e);
  }
  refreshUnscraped();
}

async function deleteSelectedUnscraped() {
  const selected = Array.from(new Set(Array.from(document.querySelectorAll(".file-chk:checked")).map(c => c.value)));
  if (selected.length === 0) {
    alert("請先勾選要刪除的項目！");
    return;
  }
  if (!confirm(`確定要永久刪除已勾選的 ${selected.length} 部影片檔案？\n⚠️ 此動作將從硬碟永久刪除檔案，無法復原！`)) return;
  try {
    const res = await fetch("/api/delete_unscraped", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ rel_paths: selected })
    });
    const data = await res.json();
    alert(data.message);
  } catch(e) {
    alert("刪除失敗: " + e);
  }
  refreshUnscraped();
}

// ================= 線上影音探索交互邏輯 =================
let exploreSource = 'jable';
let exploreTab = 'trending';
let exploreCategory = 'today-hot';
const exploreSelectedUrls = new Set();
let loadedActresses = false;

function showToast(msg) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.innerText = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2800);
}

function switchExploreTab(tab, btnEl) {
  exploreTab = tab;
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  if (btnEl) {
    btnEl.classList.add("active");
  } else {
    const el = document.querySelector(`.tab-btn[onclick*="${tab}"]`);
    if (el) el.classList.add("active");
  }

  document.getElementById("panel-trending").style.display = tab === "trending" ? "block" : "none";
  document.getElementById("panel-actresses").style.display = tab === "actresses" ? "block" : "none";
  document.getElementById("panel-search").style.display = tab === "search" ? "block" : "none";
  document.getElementById("panel-manual").style.display = tab === "manual" ? "block" : "none";

  if (tab === "actresses" && !loadedActresses) {
    loadExploreActresses();
  }
}

function getSourceDisplayName(src) {
  if (src === 'missav') return 'MissAV';
  if (src === 'supjav') return 'SupJav';
  return 'JableTV';
}

function getActressInitial(name) {
  if (!name) return "🌸";
  const clean = name.trim().replace(/^[^a-zA-Z0-9\u4e00-\u9fa5\u3040-\u30ff]+/, '');
  return clean.charAt(0) || "🌸";
}

function setExploreSource(src) {
  exploreSource = src;
  document.getElementById("src-jable").classList.toggle("active", src === "jable");
  document.getElementById("src-missav").classList.toggle("active", src === "missav");
  const sjBtn = document.getElementById("src-supjav");
  if (sjBtn) sjBtn.classList.toggle("active", src === "supjav");
  
  if (exploreTab === "trending") {
    loadExploreTrending();
  } else if (exploreTab === "actresses") {
    loadedActresses = false;
    loadExploreActresses();
  } else if (exploreTab === "search") {
    executeExploreSearch();
  }
}

function setCategory(cat, btnEl) {
  exploreCategory = cat;
  document.querySelectorAll(".cat-pill").forEach(p => p.classList.remove("active"));
  btnEl.classList.add("active");
  loadExploreTrending();
}

async function loadExploreTrending() {
  const container = document.getElementById("trending-grid");
  const sName = getSourceDisplayName(exploreSource);
  container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">正在抓取 ${sName} 最新熱門影片...</div>`;
  try {
    const res = await fetch(`/api/explore/videos?source=${exploreSource}&cat=${exploreCategory}`);
    const data = await res.json();
    renderMediaCards("trending-grid", data.videos || []);
  } catch(e) {
    container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #e53e3e; padding: 30px;">載入失敗: ${escapeHtml(e)}</div>`;
  }
}

async function loadExploreActresses() {
  const container = document.getElementById("actress-grid");
  const sName = getSourceDisplayName(exploreSource);
  container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">正在加載 ${sName} 人氣女優榜...</div>`;
  document.getElementById("actress-breadcrumb").style.display = "none";
  document.getElementById("actress-grid").style.display = "grid";
  document.getElementById("actress-videos-grid").style.display = "none";

  try {
    const res = await fetch(`/api/explore/actresses?source=${exploreSource}`);
    const data = await res.json();
    const actresses = data.actresses || [];
    if (actresses.length === 0) {
      container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 30px;">目前未找到女優資料</div>`;
      return;
    }
    container.innerHTML = "";
    for (const act of actresses) {
      const card = document.createElement("div");
      card.className = "actress-card";
      const proxyAvatar = act.avatar ? `/api/explore/proxy_image?url=${encodeURIComponent(act.avatar)}` : '';
      const initial = getActressInitial(act.name);
      const avatarHtml = proxyAvatar
        ? `<img src="${proxyAvatar}" class="actress-avatar" loading="lazy" onerror="this.onerror=null; this.outerHTML='<div class=\\'actress-avatar actress-avatar-initial\\'>${escapeHtml(initial)}</div>';">`
        : `<div class="actress-avatar actress-avatar-initial">${escapeHtml(initial)}</div>`;
      card.innerHTML = `
        ${avatarHtml}
        <div class="actress-name" title="${escapeHtml(act.name)}">${escapeHtml(act.name)}</div>
        <div class="actress-count">${escapeHtml(act.video_count || '')}</div>
      `;
      card.onclick = () => exploreActressVideos(act.url, act.name);
      container.appendChild(card);
    }
    loadedActresses = true;
  } catch(e) {
    container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #e53e3e; padding: 30px;">載入失敗: ${escapeHtml(e)}</div>`;
  }
}

async function exploreActressVideos(actressUrl, actressName) {
  document.getElementById("actress-grid").style.display = "none";
  document.getElementById("actress-breadcrumb").style.display = "flex";
  document.getElementById("current-actress-title").innerText = actressName;
  const vGrid = document.getElementById("actress-videos-grid");
  vGrid.style.display = "grid";
  vGrid.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">正在載入 ${escapeHtml(actressName)} 的專屬作品...</div>`;

  try {
    const res = await fetch(`/api/explore/actress_videos?source=${exploreSource}&url=${encodeURIComponent(actressUrl)}`);
    const data = await res.json();
    renderMediaCards("actress-videos-grid", data.videos || []);
  } catch(e) {
    vGrid.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #e53e3e; padding: 30px;">作品載入失敗: ${escapeHtml(e)}</div>`;
  }
}

function backToActressList() {
  document.getElementById("actress-grid").style.display = "grid";
  document.getElementById("actress-breadcrumb").style.display = "none";
  document.getElementById("actress-videos-grid").style.display = "none";
}

async function executeExploreSearch() {
  const query = document.getElementById("explore-search-input").value.trim();
  if (!query) {
    alert("請輸入搜尋關鍵字！");
    return;
  }
  const container = document.getElementById("search-grid");
  container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 40px;">正在多站搜尋「${escapeHtml(query)}」...</div>`;

  try {
    const res = await fetch(`/api/explore/search?source=${exploreSource}&query=${encodeURIComponent(query)}`);
    const data = await res.json();
    renderMediaCards("search-grid", data.videos || []);
  } catch(e) {
    container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #e53e3e; padding: 30px;">搜尋失敗: ${escapeHtml(e)}</div>`;
  }
}

function renderMediaCards(containerId, videos) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!videos || videos.length === 0) {
    container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: #a0aec0; padding: 30px;">查無相關影片</div>`;
    return;
  }

  for (const v of videos) {
    const card = document.createElement("div");
    card.className = "media-card";
    const proxyCover = v.cover ? `/api/explore/proxy_image?url=${encodeURIComponent(v.cover)}` : '';
    const isSelected = exploreSelectedUrls.has(v.url);

    card.innerHTML = `
      <div class="media-thumb-wrap">
        <img src="${proxyCover}" class="media-thumb" loading="lazy" onerror="this.src='/favicon.svg'">
        <input type="checkbox" class="media-card-chk" ${isSelected ? 'checked' : ''} onchange="onExploreChkChange(this, '${escapeHtml(v.url)}')">
        <span class="media-source-tag">${escapeHtml(v.source)}</span>
        ${v.duration ? `<span class="media-duration">${escapeHtml(v.duration)}</span>` : ''}
      </div>
      <div class="media-card-body">
        <div class="media-card-code">${escapeHtml(v.id || '')}</div>
        <div class="media-card-title" title="${escapeHtml(v.title)}">${escapeHtml(v.title)}</div>
        <button type="button" class="btn-quick-download" onclick="addSingleToQueue('${escapeHtml(v.url)}', this)">➕ 一鍵下載</button>
      </div>
    `;
    container.appendChild(card);
  }
}

function onExploreChkChange(chk, url) {
  if (chk.checked) {
    exploreSelectedUrls.add(url);
  } else {
    exploreSelectedUrls.delete(url);
  }
  updateExploreBatchBar();
}

function updateExploreBatchBar() {
  const bar = document.getElementById("batch-bar");
  const countSpan = document.getElementById("batch-selected-count");
  if (exploreSelectedUrls.size > 0) {
    bar.style.display = "flex";
    countSpan.innerText = exploreSelectedUrls.size;
  } else {
    bar.style.display = "none";
  }
}

function clearExploreSelection() {
  exploreSelectedUrls.clear();
  document.querySelectorAll(".media-card-chk").forEach(c => c.checked = false);
  updateExploreBatchBar();
}

async function addSingleToQueue(url, btnEl) {
  btnEl.innerText = "加入中...";
  btnEl.disabled = true;
  try {
    const res = await fetch("/api/add_batch", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ urls: [url] })
    });
    const data = await res.json();
    if (data.ok) {
      btnEl.innerText = "✅ 已加入佇列";
      btnEl.classList.add("added");
      showToast("🎉 已成功加入下載佇列！");
      refreshQueue();
    } else {
      alert("加入失敗: " + data.message);
      btnEl.innerText = "➕ 一鍵下載";
      btnEl.disabled = false;
    }
  } catch(e) {
    alert("連線失敗: " + e);
    btnEl.innerText = "➕ 一鍵下載";
    btnEl.disabled = false;
  }
}

async function addSelectedToQueue() {
  const urls = Array.from(exploreSelectedUrls);
  if (urls.length === 0) return;
  try {
    const res = await fetch("/api/add_batch", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ urls: urls })
    });
    const data = await res.json();
    alert(data.message);
    clearExploreSelection();
    showToast(`🎉 成功加入 ${urls.length} 部影片至佇列！`);
    refreshQueue();
  } catch(e) {
    alert("批次加入失敗: " + e);
  }
}

loadSettingsFromServer();
refreshQueue();
refreshUnscraped();
loadExploreTrending();
setInterval(refreshQueue, 3000);
</script>
</body>
</html>
"""

AUTH_PAGE_HTML = """
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>{{ title }} - UAV 中心</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
  background: #f4f6f9; display: flex; align-items: center; justify-content: center;
  min-height: 90vh; margin: 0;
}
.login-card {
  background: white; padding: 30px; border-radius: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.08); width: 100%; max-width: 360px;
}
h2 { margin-top: 0; color: #2b6cb0; }
.form-group { margin-bottom: 16px; }
label { display: block; margin-bottom: 6px; font-size: 14px; font-weight: 500; }
input { width: 100%; padding: 10px; border: 1px solid #cbd5e0; border-radius: 6px; box-sizing: border-box; }
button { width: 100%; padding: 11px; background: #2b6cb0; color: white; border: 0; border-radius: 6px; font-weight: bold; cursor: pointer; }
.error { background: #fed7d7; color: #9b2c2c; padding: 10px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
</style>
</head>
<body>
<div class="login-card">
  <h2>{{ title }}</h2>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post" action="{{ action }}">
    <div class="form-group"><label>帳號</label><input type="text" name="username" required autofocus></div>
    <div class="form-group"><label>密碼</label><input type="password" name="password" required></div>
    {% if is_setup %}<div class="form-group"><label>確認密碼</label><input type="password" name="confirm_password" required></div>{% endif %}
    <button type="submit">{{ btn_text }}</button>
  </form>
</div>
</body>
</html>
"""


# ==================== 路由處理 ====================

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if has_users():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        cp = request.form.get("confirm_password", "")
        if not u or not p:
            error = "帳號與密碼不能為空"
        elif p != cp:
            error = "兩次密碼不一致"
        else:
            save_users({u: generate_password_hash(p)})
            session["logged_in"] = True
            session["username"] = u
            return redirect(url_for("index"))
    return render_template_string(AUTH_PAGE_HTML, title="初始化管理者帳號", action="/setup", btn_text="建立並開始使用", is_setup=True, error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_users():
        return redirect(url_for("setup"))
    if session.get("logged_in"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        users = load_users()
        if u in users and check_password_hash(users[u], p):
            session["logged_in"] = True
            session["username"] = u
            return redirect(url_for("index"))
        error = "帳號或密碼錯誤"
    return render_template_string(AUTH_PAGE_HTML, title="登入下載中心", action="/login", btn_text="登入", is_setup=False, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/favicon.svg")
def favicon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#2b6cb0"/><text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle" font-family="Arial" font-size="24" font-weight="700" fill="#fff">UAV</text></svg>"""
    return Response(svg, mimetype="image/svg+xml")

@app.route("/")
@login_required
def index():
    return render_template_string(
        MAIN_PAGE_HTML,
        current_user=session.get("username", "管理員"),
        host_download_dir=HOST_DOWNLOAD_DIR,
        host_media_dir=HOST_MEDIA_DIR
    )

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def handle_settings():
    if request.method == "POST":
        data = request.json or {}
        cur = load_settings()
        if "naming_rule" in data and data["naming_rule"].strip():
            cur["naming_rule"] = data["naming_rule"].strip()
        if "multi_naming_rule" in data and data["multi_naming_rule"].strip():
            cur["multi_naming_rule"] = data["multi_naming_rule"].strip()
        save_settings(cur)
        return jsonify({"ok": True, "message": "命名規則設定已成功儲存！", "settings": cur})
    return jsonify(load_settings())

@app.route("/add", methods=["POST"])
@login_required
def add():
    text = request.form.get("urls", "")
    urls = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    with lock:
        queue = load_queue()
        for u in urls:
            item_id = str(int(time.time() * 1000)) + "_" + str(len(queue))
            queue.append({
                "id": item_id, "url": u, "status": "waiting", "message": "等待下載",
                "progress": 0, "added_at": now(), "started_at": "", "finished_at": "", "logs": []
            })
        save_queue(queue)
    return redirect("/")

@app.route("/delete/<item_id>", methods=["POST"])
@login_required
def delete(item_id):
    with lock:
        queue = load_queue()
        new_q = []
        found = False
        for it in queue:
            if it.get("id") == item_id:
                found = True
                if it.get("status") == "running":
                    return jsonify({"ok": False, "message": "下載中的任務無法刪除"})
                continue
            new_q.append(it)
        if not found:
            return jsonify({"ok": False, "message": "找不到項目"})
        save_queue(new_q)
    return jsonify({"ok": True})

@app.route("/api/clear_finished_queue", methods=["POST"])
@login_required
def clear_finished():
    with lock:
        queue = load_queue()
        new_q = [it for it in queue if it.get("status") == "running"]
        save_queue(new_q)
    return jsonify({"ok": True})

@app.route("/api/status")
@login_required
def status():
    return jsonify({"queue": load_queue()})

@app.route("/api/unscraped")
@login_required
def get_unscraped_files():
    video_exts = {".mp4", ".mkv", ".avi", ".ts", ".wmv", ".mov", ".m4v"}
    settings = load_settings()
    files = []
    try:
        for root, dirs, filenames in os.walk(DOWNLOAD_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in filenames:
                if f.startswith("."):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in video_exts:
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, DOWNLOAD_DIR).replace("\\", "/")
                    size_mb = os.path.getsize(full_p) // (1024 * 1024)
                    mid = extract_movie_id(f)
                    cache = SCRAPED_CACHE.get(rel_p, {})

                    target_rel = ""
                    if cache.get("meta"):
                        target_rel = compute_target_path(cache["meta"], ext, settings)

                    files.append({
                        "rel_path": rel_p,
                        "filename": f,
                        "movie_id": mid,
                        "size_mb": size_mb,
                        "status": cache.get("status", "unscraped"),
                        "meta": cache.get("meta"),
                        "poster_b64": cache.get("poster_b64"),
                        "target_rel": target_rel
                    })
    except Exception:
        pass
    return jsonify({"files": files})

@app.route("/api/scrape_preview", methods=["POST"])
@login_required
def scrape_preview():
    rel_path = request.json.get("rel_path")
    if not rel_path:
        return jsonify({"ok": False, "message": "缺少檔案路徑"})

    src_file = os.path.join(DOWNLOAD_DIR, rel_path)
    if not os.path.exists(src_file):
        return jsonify({"ok": False, "message": "檔案不存在"})

    mid = extract_movie_id(os.path.basename(rel_path))
    meta = multi_source_scrape(mid)
    if not meta:
        return jsonify({"ok": False, "message": f"多來源庫中皆找不到番號 {mid} 的元數據"})

    raw_img = download_image_bytes(meta.get("cover_url"))
    fanart_bytes, poster_bytes, poster_b64 = (None, None, None)
    if raw_img:
        fanart_bytes, poster_bytes, poster_b64 = crop_and_create_poster(raw_img)

    SCRAPED_CACHE[rel_path] = {
        "status": "preview_ready",
        "meta": meta,
        "fanart_bytes": fanart_bytes,
        "poster_bytes": poster_bytes,
        "poster_b64": poster_b64
    }

    return jsonify({"ok": True, "message": f"預覽成功 [來源: {meta.get('source')}]"})

@app.route("/api/scrape_all_preview", methods=["POST"])
@login_required
def scrape_all_preview():
    video_exts = {".mp4", ".mkv", ".avi", ".ts", ".wmv", ".mov", ".m4v"}
    count = 0
    for root, dirs, filenames in os.walk(DOWNLOAD_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in filenames:
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() in video_exts:
                full_p = os.path.join(root, f)
                rel_p = os.path.relpath(full_p, DOWNLOAD_DIR).replace("\\", "/")
                mid = extract_movie_id(f)
                meta = multi_source_scrape(mid)
                if meta:
                    raw_img = download_image_bytes(meta.get("cover_url"))
                    f_bytes, p_bytes, p_b64 = crop_and_create_poster(raw_img) if raw_img else (None, None, None)
                    SCRAPED_CACHE[rel_p] = {
                        "status": "preview_ready",
                        "meta": meta,
                        "fanart_bytes": f_bytes,
                        "poster_bytes": p_bytes,
                        "poster_b64": p_b64
                    }
                    count += 1
    return jsonify({"ok": True, "message": f"成功獲取 {count} 部影片之刮削資料與海報"})

@app.route("/api/commit_organize", methods=["POST"])
@login_required
def commit_organize():
    rel_paths = request.json.get("rel_paths", [])
    auto_scrape = request.json.get("auto_scrape", False)
    if not rel_paths:
        return jsonify({"ok": False, "message": "未選取任何檔案"})

    video_exts = {".mp4", ".mkv", ".avi", ".ts", ".wmv", ".mov", ".m4v"}
    settings = load_settings()
    success_cnt = 0
    fail_cnt = 0

    for rel_p in rel_paths:
        src = os.path.join(DOWNLOAD_DIR, rel_p)
        if not os.path.exists(src):
            fail_cnt += 1
            continue

        cached = SCRAPED_CACHE.get(rel_p)
        if not cached or not cached.get("meta"):
            if auto_scrape:
                mid = extract_movie_id(os.path.basename(rel_p))
                meta = multi_source_scrape(mid)
                if not meta:
                    fail_cnt += 1
                    continue
                raw_img = download_image_bytes(meta.get("cover_url"))
                f_bytes, p_bytes, _ = crop_and_create_poster(raw_img) if raw_img else (None, None, None)
                cached = {"meta": meta, "fanart_bytes": f_bytes, "poster_bytes": p_bytes}
            else:
                fail_cnt += 1
                continue

        meta = cached["meta"]
        ext = os.path.splitext(rel_p)[1].lower()

        target_rel = compute_target_path(meta, ext, settings)
        target_video_path = os.path.join(MEDIA_LIBRARY_DIR, target_rel)
        target_dir = os.path.dirname(target_video_path)
        os.makedirs(target_dir, exist_ok=True)

        shutil.move(src, target_video_path)

        if cached.get("poster_bytes"):
            with open(os.path.join(target_dir, "poster.jpg"), "wb") as img_f:
                img_f.write(cached["poster_bytes"])
        if cached.get("fanart_bytes"):
            with open(os.path.join(target_dir, "fanart.jpg"), "wb") as img_f:
                img_f.write(cached["fanart_bytes"])

        nfo_text = generate_nfo_xml(meta)
        target_stem = os.path.splitext(os.path.basename(target_video_path))[0]
        with open(os.path.join(target_dir, "movie.nfo"), "w", encoding="utf-8") as nf:
            nf.write(nfo_text)
        with open(os.path.join(target_dir, f"{target_stem}.nfo"), "w", encoding="utf-8") as nf:
            nf.write(nfo_text)

        src_parent = os.path.dirname(src)
        if src_parent != DOWNLOAD_DIR and os.path.exists(src_parent):
            try:
                has_other = False
                for _, _, flist in os.walk(src_parent):
                    if any(os.path.splitext(x)[1].lower() in video_exts for x in flist):
                        has_other = True
                        break
                if not has_other:
                    shutil.rmtree(src_parent, ignore_errors=True)
            except Exception:
                pass

        SCRAPED_CACHE.pop(rel_p, None)
        success_cnt += 1
        log_global(f"成功入庫整理：{target_rel} [來源: {meta.get('source')}]")

    return jsonify({"ok": True, "message": f"入庫完成：成功 {success_cnt} 部，失敗/略過 {fail_cnt} 部"})

@app.route("/api/delete_unscraped", methods=["POST"])
@login_required
def delete_unscraped():
    data = request.json or {}
    rel_paths = data.get("rel_paths", [])
    if isinstance(rel_paths, str):
        rel_paths = [rel_paths]
    if not rel_paths:
        return jsonify({"ok": False, "message": "未選取要刪除的檔案"})

    video_exts = {".mp4", ".mkv", ".avi", ".ts", ".wmv", ".mov", ".m4v"}
    deleted_cnt = 0
    fail_cnt = 0

    for rel_p in rel_paths:
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, rel_p))
        # 安全邊界檢查，防止路徑穿越 (Path Traversal)
        if not full_path.startswith(os.path.abspath(DOWNLOAD_DIR)):
            fail_cnt += 1
            continue

        if os.path.exists(full_path) and os.path.isfile(full_path):
            try:
                os.remove(full_path)
                SCRAPED_CACHE.pop(rel_p, None)
                deleted_cnt += 1

                # 若所在子資料夾已無其他影片檔，自動清理該空資料夾
                parent_dir = os.path.dirname(full_path)
                if parent_dir != os.path.abspath(DOWNLOAD_DIR) and os.path.exists(parent_dir):
                    has_other = False
                    for _, _, flist in os.walk(parent_dir):
                        if any(os.path.splitext(x)[1].lower() in video_exts for x in flist):
                            has_other = True
                            break
                    if not has_other:
                        shutil.rmtree(parent_dir, ignore_errors=True)
            except Exception:
                fail_cnt += 1
        else:
            fail_cnt += 1

    return jsonify({"ok": True, "message": f"刪除完成：成功刪除 {deleted_cnt} 部影片" + (f"，失敗 {fail_cnt} 部" if fail_cnt > 0 else "")})

# ==================== 線上探索 API 路由 ====================

@app.route("/api/explore/videos")
@login_required
def api_explore_videos():
    src = request.args.get("source", "jable").lower()
    cat = request.args.get("cat", "today-hot")
    page = int(request.args.get("page", 1))
    if src == "missav":
        videos = fetch_missav_videos(cat, page)
    elif src == "supjav":
        videos = fetch_supjav_videos(cat, page)
    else:
        videos = fetch_jable_videos(cat, page)
    return jsonify({"ok": True, "videos": videos, "source": src, "cat": cat, "page": page})

@app.route("/api/explore/search")
@login_required
def api_explore_search():
    src = request.args.get("source", "jable").lower()
    query = request.args.get("query", "").strip()
    page = int(request.args.get("page", 1))
    if not query:
        return jsonify({"ok": True, "videos": []})
    if src == "missav":
        videos = search_missav_videos(query, page)
    elif src == "supjav":
        videos = search_supjav_videos(query, page)
    else:
        videos = search_jable_videos(query, page)
    return jsonify({"ok": True, "videos": videos, "query": query, "source": src, "page": page})

@app.route("/api/explore/actresses")
@login_required
def api_explore_actresses():
    src = request.args.get("source", "jable").lower()
    page = int(request.args.get("page", 1))
    if src == "missav":
        actresses = fetch_missav_actresses(page)
    elif src == "supjav":
        actresses = fetch_supjav_actresses(page)
    else:
        actresses = fetch_jable_actresses(page)
    return jsonify({"ok": True, "actresses": actresses, "source": src, "page": page})

@app.route("/api/explore/actress_videos")
@login_required
def api_explore_actress_videos():
    src = request.args.get("source", "jable").lower()
    actress_url = request.args.get("url", "").strip()
    page = int(request.args.get("page", 1))
    if not actress_url:
        return jsonify({"ok": True, "videos": []})
    if src == "missav" or "missav" in actress_url:
        videos = fetch_missav_actress_videos(actress_url, page)
    elif src == "supjav" or "supjav" in actress_url:
        videos = fetch_supjav_actress_videos(actress_url, page)
    else:
        videos = fetch_jable_actress_videos(actress_url, page)
    return jsonify({"ok": True, "videos": videos, "url": actress_url, "page": page})

@app.route("/api/explore/proxy_image")
def api_explore_proxy_image():
    img_url = request.args.get("url", "")
    if not img_url:
        return Response("", status=404)
    if "jable" in img_url:
        referer = "https://jable.tv/"
    elif "supjav" in img_url:
        referer = "https://supjav.com/"
    elif "javbus" in img_url:
        referer = "https://www.javbus.com/"
    elif "fourhoi" in img_url or "missav" in img_url:
        referer = "https://missav.ai/"
    else:
        referer = "https://missav.ai/"
    try:
        r = robust_browser_get(img_url, referer=referer, timeout=10)
        if r.status_code == 200:
            c_type = r.headers.get("Content-Type", "image/jpeg")
            resp = Response(r.content, mimetype=c_type)
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    except Exception:
        pass
    return Response("", status=404)

@app.route("/api/add_batch", methods=["POST"])
@login_required
def api_add_batch():
    data = request.json or {}
    urls = data.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    valid_urls = [u.strip() for u in urls if u and u.strip().startswith("http")]
    if not valid_urls:
        return jsonify({"ok": False, "message": "未提供有效網址"})
    with lock:
        queue = load_queue()
        added_count = 0
        for u in valid_urls:
            item_id = str(int(time.time() * 1000)) + "_" + str(len(queue))
            queue.append({
                "id": item_id, "url": u, "status": "waiting", "message": "等待下載",
                "progress": 0, "added_at": now(), "started_at": "", "finished_at": "", "logs": []
            })
            added_count += 1
        save_queue(queue)
    return jsonify({"ok": True, "message": f"成功加入 {added_count} 部影片至下載佇列！", "count": added_count})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
