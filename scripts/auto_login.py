"""
ClawCloud 自动登录脚本 (Enhanced Version)
- 强化反爬虫对抗 (User-Agent, Viewport, Languages)
- 失败重试机制
- 自动检测区域跳转
- Telegram 通知
"""

import os
import sys
import time
import base64
import re
import random
import requests
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# ==================== 配置 ====================
LOGIN_ENTRY_URL = "https://console.run.claw.cloud"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))

class Telegram:
    """Telegram 通知"""
    def __init__(self):
        self.token = os.environ.get('TG_BOT_TOKEN')
        self.chat_id = os.environ.get('TG_CHAT_ID')
        self.ok = bool(self.token and self.chat_id)
    
    def send(self, msg):
        if not self.ok: return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=30
            )
        except: pass
    
    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path): return
        try:
            with open(path, 'rb') as f:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": f},
                    timeout=60
                )
        except: pass

    def flush_updates(self):
        if not self.ok: return 0
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates", params={"timeout": 0}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"): return data["result"][-1]["update_id"] + 1
        except: pass
        return 0
    
    def wait_code(self, timeout=120):
        if not self.ok: return None
        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")
        
        while time.time() < deadline:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"timeout": 20, "offset": offset},
                    timeout=30
                )
                data = r.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != str(self.chat_id): continue
                    m = pattern.match((msg.get("text") or "").strip())
                    if m: return m.group(1)
            except: pass
            time.sleep(2)
        return None

class SecretUpdater:
    """GitHub Secret 更新器"""
    def __init__(self):
        self.token = os.environ.get('REPO_TOKEN')
        self.repo = os.environ.get('GITHUB_REPOSITORY')
        self.ok = bool(self.token and self.repo)
    
    def update(self, name, value):
        if not self.ok: return False
        try:
            from nacl import encoding, public
            headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
            r = requests.get(f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key", headers=headers, timeout=30)
            if r.status_code != 200: return False
            key_data = r.json()
            pk = public.PublicKey(key_data['key'].encode(), encoding.Base64Encoder())
            encrypted = public.SealedBox(pk).encrypt(value.encode())
            r = requests.put(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                headers=headers,
                json={"encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key_data['key_id']},
                timeout=30
            )
            return r.status_code in [201, 204]
        except: return False

class AutoLogin:
    def __init__(self):
        self.username = os.environ.get('GH_USERNAME')
        self.password = os.environ.get('GH_PASSWORD')
        self.gh_session = os.environ.get('GH_SESSION', '').strip()
        self.tg = Telegram()
        self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0
        self.detected_region = None
        self.region_base_url = None
        
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        print(f"{icons.get(level, '•')} {msg}")
        self.logs.append(f"{icons.get(level, '•')} {msg}")
    
    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f, full_page=True)
            self.shots.append(f)
        except: pass
        return f
    
    def click(self, page, sels, desc=""):
        for s in sels:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=3000):
                    el.click()
                    self.log(f"已点击: {desc}", "SUCCESS")
                    return True
            except: pass
        return False

    def detect_region(self, url):
        try:
            parsed = urlparse(url)
            host = parsed.netloc
            if host.endswith('.console.claw.cloud'):
                region = host.replace('.console.claw.cloud', '')
                if region and region != 'console':
                    self.detected_region = region
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    return region
            path = parsed.path
            region_match = re.search(r'/(?:region|r)/([a-z]+-[a-z]+-\d+)', path)
            if region_match:
                region = region_match.group(1)
                self.detected_region = region
                self.region_base_url = f"https://{region}.console.claw.cloud"
                self.log(f"从路径检测到区域: {region}", "SUCCESS")
                return region
            self.region_base_url = f"{parsed.scheme}://{parsed.netloc}"
        except: pass
        return None

    def get_base_url(self):
        return self.region_base_url or LOGIN_ENTRY_URL

    def get_session(self, context):
        try:
            for c in context.cookies():
                if c['name'] == 'user_session' and 'github' in c.get('domain', ''):
                    return c['value']
        except: pass
        return None

    def save_cookie(self, value):
        if not value: return
        self.log(f"新 Cookie: {value[:15]}...", "SUCCESS")
        if self.secret.update('GH_SESSION', value):
            self.tg.send("🔑 <b>Cookie 已自动更新</b>")
        else:
            self.tg.send(f"🔑 <b>新 Cookie</b>\n<code>{value}</code>")

    def wait_device(self, page):
        self.log(f"等待设备验证 ({DEVICE_VERIFY_WAIT}s)...", "WARN")
        self.shot(page, "设备验证")
        self.tg.send(f"⚠️ <b>需要设备验证</b>\n请在 {DEVICE_VERIFY_WAIT} 秒内批准。")
        if self.shots: self.tg.photo(self.shots[-1])
        
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                if 'verified-device' not in page.url and 'device-verification' not in page.url:
                    self.log("验证通过！", "SUCCESS")
                    return True
                try: page.reload()
                except: pass
        return 'verified-device' not in page.url

    def wait_two_factor_mobile(self, page):
        self.log(f"等待 2FA (Mobile) ({TWO_FACTOR_WAIT}s)...", "WARN")
        shot = self.shot(page, "2FA_Mobile")
        self.tg.send(f"⚠️ <b>GitHub Mobile 2FA</b>\n请在手机上批准。\n等待 {TWO_FACTOR_WAIT} 秒")
        if shot: self.tg.photo(shot)
        
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            if "github.com/sessions/two-factor/" not in page.url:
                self.log("2FA 通过", "SUCCESS")
                return True
            if "github.com/login" in page.url: return False
        return False

    def handle_2fa_code_input(self, page):
        self.log("需要 2FA 验证码", "WARN")
        shot = self.shot(page, "2FA_Code")
        self.tg.send(f"🔐 <b>需要验证码</b>\n发送: <code>/code 123456</code>\n等待 {TWO_FACTOR_WAIT} 秒")
        if shot: self.tg.photo(shot)
        
        code = self.tg.wait_code(timeout=TWO_FACTOR_WAIT)
        if not code: return False
        
        try:
            page.locator('input[autocomplete="one-time-code"], input[name="app_otp"], input[id="otp"]').fill(code)
            time.sleep(1)
            if not self.click(page, ['button:has-text("Verify")', 'button[type="submit"]']):
                page.keyboard.press("Enter")
            time.sleep(3)
            return "github.com/sessions/two-factor/" not in page.url
        except: return False

    def login_github(self, page, context):
        self.log("登录 GitHub...", "STEP")
        
        # 尝试刷新几次，防止页面加载不全
        for i in range(3):
            try:
                page.wait_for_selector('input[name="login"]', timeout=10000)
                break
            except:
                self.log(f"未找到输入框，重试刷新 ({i+1}/3)...", "WARN")
                self.shot(page, f"刷新前_{i}")
                page.reload()
                time.sleep(3)
        
        try:
            page.locator('input[name="login"]').fill(self.username)
            page.locator('input[name="password"]').fill(self.password)
            self.shot(page, "输入后")
            page.locator('input[type="submit"], button[type="submit"]').first.click()
        except Exception as e:
            self.log(f"登录输入失败: {e}", "ERROR")
            return False
        
        time.sleep(3)
        page.wait_for_load_state('networkidle', timeout=30000)
        
        # 设备验证 / 2FA 处理...
        url = page.url
        if 'verified-device' in url:
            if not self.wait_device(page): return False
        if 'two-factor' in page.url:
            if 'two-factor/mobile' in url:
                if not self.wait_two_factor_mobile(page): return False
            else:
                if not self.handle_2fa_code_input(page): return False
        
        return True

    def run(self):
        print("🚀 ClawCloud 自动登录 (Enhanced)")
        if not self.username or not self.password:
            self.notify(False, "缺少凭据")
            sys.exit(1)
            
        with sync_playwright() as p:
            # 增强浏览器伪装
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-blink-features=AutomationControlled'])
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='Asia/Shanghai'
            )
            page = context.new_page()
            
            # 添加 stealth 脚本注入 (绕过简单检测)
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            
            try:
                # 预加载 Cookie
                if self.gh_session:
                    try:
                        context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}, {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}])
                        self.log("已加载 Cookie", "SUCCESS")
                    except: pass
                
                # 访问入口
                self.log("步骤1: 访问入口", "STEP")
                page.goto(SIGNIN_URL, timeout=60000)
                time.sleep(random.uniform(2, 4)) # 随机等待
                
                if 'signin' not in page.url and 'claw.cloud' in page.url:
                    self.log("Cookie 有效，已登录", "SUCCESS")
                    self.detect_region(page.url)
                    self.keepalive(page)
                    new = self.get_session(context)
                    if new: self.save_cookie(new)
                    self.notify(True)
                    return

                # 点击 GitHub
                self.log("步骤2: 点击 GitHub", "STEP")
                if not self.click(page, ['button:has-text("GitHub")', '[data-provider="github"]'], "GitHub"):
                    self.log("找不到入口按钮", "ERROR")
                    self.shot(page, "找不到入口")
                    self.notify(False, "找不到入口")
                    return

                # GitHub 登录流程
                if 'github.com/login' in page.url:
                    if not self.login_github(page, context):
                        self.shot(page, "登录失败")
                        self.notify(False, "GitHub 登录失败")
                        return
                
                # 处理 OAuth
                if 'oauth/authorize' in page.url:
                    self.log("处理 OAuth...", "STEP")
                    self.click(page, ['button[name="authorize"]', 'button:has-text("Authorize")'])
                    time.sleep(3)

                # 等待跳转
                self.log("步骤4: 等待跳转 (120s)...", "STEP")
                redirected = False
                for _ in range(60): # 60 * 2s = 120s
                    if 'claw.cloud' in page.url and 'signin' not in page.url:
                        redirected = True
                        break
                    # 如果还卡在 GitHub，尝试点授权
                    if 'oauth' in page.url:
                        self.click(page, ['button[name="authorize"]'])
                    time.sleep(2)
                
                if not redirected:
                    self.log("重定向超时", "ERROR")
                    self.shot(page, "重定向失败")
                    self.notify(False, "重定向超时")
                    return
                
                self.detect_region(page.url)
                self.keepalive(page)
                
                # 更新 Cookie
                new = self.get_session(context)
                if new: self.save_cookie(new)
                
                self.notify(True)
                print("✅ 成功！")
                
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                self.notify(False, str(e))
            finally:
                browser.close()

if __name__ == "__main__":
    AutoLogin().run()
