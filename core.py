import asyncio
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet
from telegram import Bot, InputFile
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent
VAULT_PATH = ROOT_DIR / 'vault.json'
VAULT_KEY_PATH = ROOT_DIR / 'vault.key'
STORAGE_DIR = ROOT_DIR / 'storage'
PROXY_FILE = ROOT_DIR / 'proxies.json'
BASE_URL = 'https://morocco.blsspainvisa.com'

STEALTH_INIT_SCRIPT = """
(() => {
  const randomize = () => Math.random().toString(36).substring(2, 12);
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  const originalQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters);
  const getParameter = WebGLRenderingContext.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'WebKit';
    if (parameter === 37446) return 'WebKit WebGL';
    return getParameter.call(this, parameter);
  };
  const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
    const result = originalToDataURL.apply(this, [type, quality]);
    return result;
  };
  const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function(x, y, width, height) {
    const imageData = originalGetImageData.apply(this, [x, y, width, height]);
    for (let i = 0; i < imageData.data.length; i += 4) {
      imageData.data[i] = imageData.data[i] ^ 0;
    }
    return imageData;
  };
  const originalCreateOscillator = AudioContext.prototype.createOscillator;
  AudioContext.prototype.createOscillator = function() {
    const oscillator = originalCreateOscillator.apply(this);
    const originalStart = oscillator.start;
    oscillator.start = function() {
      originalStart.apply(this);
    };
    return oscillator;
  };
})();
"""

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6424.93 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
]

class SecureVault:
    def __init__(self, vault_path: Path = VAULT_PATH, key_path: Path = VAULT_KEY_PATH):
        self.vault_path = vault_path
        self.key_path = key_path
        self.key = self._load_or_create_key()
        self.fernet = Fernet(self.key)
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        STORAGE_DIR.mkdir(exist_ok=True)
        if not self.vault_path.exists():
            self.save_accounts([])

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        return key

    def _encrypt(self, payload: str) -> str:
        return self.fernet.encrypt(payload.encode('utf-8')).decode('utf-8')

    def _decrypt(self, payload: str) -> str:
        return self.fernet.decrypt(payload.encode('utf-8')).decode('utf-8')

    def load_accounts(self) -> List[Dict[str, Any]]:
        raw = json.loads(self.vault_path.read_text(encoding='utf-8'))
        accounts = []
        for entry in raw:
            account = {
                'email': entry['email'],
                'password': self._decrypt(entry['password']),
                'details': json.loads(self._decrypt(entry['details'])),
            }
            accounts.append(account)
        return accounts

    def save_accounts(self, accounts: List[Dict[str, Any]]) -> None:
        encrypted = []
        for account in accounts:
            encrypted.append({
                'email': account['email'],
                'password': self._encrypt(account['password']),
                'details': self._encrypt(json.dumps(account.get('details', {}))),
            })
        self.vault_path.write_text(json.dumps(encrypted, indent=2), encoding='utf-8')

    def upgrade_legacy(self, legacy_path: Path) -> None:
        if not legacy_path.exists() or self.vault_path.exists():
            return
        legacy_value = json.loads(legacy_path.read_text(encoding='utf-8'))
        accounts = []
        if isinstance(legacy_value, dict):
            for email, payload in legacy_value.items():
                accounts.append({
                    'email': email,
                    'password': '',
                    'details': {'legacy': payload},
                })
        else:
            accounts.append({'email': '', 'password': '', 'details': {'legacy': legacy_value}})
        self.save_accounts(accounts)

@dataclass
class AccountRecord:
    email: str
    password: str
    details: Dict[str, Any]
    storage_state: Path

    @property
    def id(self) -> str:
        digest = hashlib.blake2b(self.email.encode('utf-8'), digest_size=8).hexdigest()
        return digest

class ProxyManager:
    def __init__(self, proxy_path: Optional[Path] = PROXY_FILE, env_key: str = 'PROXIES'):
        self.proxy_path = proxy_path
        self.env_key = env_key
        self.proxies = self._load_sources()
        self.index = 0
        self.blocked = set()

    def _load_sources(self) -> List[str]:
        proxies = []
        if self.proxy_path.exists():
            try:
                raw = json.loads(self.proxy_path.read_text(encoding='utf-8'))
                if isinstance(raw, list):
                    proxies.extend([str(p).strip() for p in raw if p])
            except Exception as exc:
                logger.warning('Invalid proxies.json format: %s', exc)
        env_proxies = os.environ.get(self.env_key, '')
        for line in env_proxies.splitlines():
            token = line.strip()
            if token:
                proxies.append(token)
        return proxies or []

    def next_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        self.index %= len(self.proxies)
        proxy = self.proxies[self.index]
        self.index += 1
        if proxy in self.blocked and len(self.blocked) < len(self.proxies):
            return self.next_proxy()
        return proxy

    def mark_blocked(self, proxy: str) -> None:
        self.blocked.add(proxy)
        logger.warning('Proxy marked blocked: %s', proxy)

    def parse_proxy(self, proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
        if not proxy_url:
            return None
        return {'server': proxy_url}

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.chat_id = chat_id
        self.bot = Bot(token=token) if token and chat_id else None

    async def send_message(self, text: str) -> None:
        if not self.bot:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.bot.send_message, self.chat_id, text)

    async def send_screenshot(self, page: Page, caption: str) -> None:
        if not self.bot:
            return
        screenshot = await page.screenshot(type='png', full_page=False)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._upload_screenshot, screenshot, caption)

    def _upload_screenshot(self, screenshot_bytes: bytes, caption: str) -> None:
        with InputFile(BytesIO(screenshot_bytes), filename='screenshot.png') as image_file:
            self.bot.send_photo(chat_id=self.chat_id, photo=image_file, caption=caption)

class Humanizer:
    @staticmethod
    async def pause(min_seconds: float = 0.2, max_seconds: float = 0.8) -> None:
        await asyncio.sleep(random.uniform(min_seconds, max_seconds))

    @staticmethod
    async def type_text(page: Page, selector: str, text: str) -> None:
        element = page.locator(selector)
        await element.click()
        for character in text:
            await page.keyboard.type(character, delay=random.uniform(80, 180))
            if random.random() < 0.08:
                await asyncio.sleep(random.uniform(0.08, 0.2))
        await Humanizer.pause(0.2, 0.5)

    @staticmethod
    async def click(page: Page, selector: str) -> None:
        element = page.locator(selector).first
        box = await element.bounding_box()
        if box is not None:
            x = random.uniform(box['x'] + 5, box['x'] + box['width'] - 5)
            y = random.uniform(box['y'] + 5, box['y'] + box['height'] - 5)
            steps = random.randint(8, 16)
            for step in range(1, steps + 1):
                await page.mouse.move(
                    box['x'] + (x - box['x']) * step / steps,
                    box['y'] + (y - box['y']) * step / steps,
                    steps=2,
                )
                await asyncio.sleep(random.uniform(0.01, 0.03))
            await page.mouse.click(x, y, delay=random.uniform(50, 140))
        else:
            await element.click()
        await Humanizer.pause(0.2, 0.6)

    @staticmethod
    async def scroll(page: Page) -> None:
        viewport_height = page.viewport_size['height'] if page.viewport_size else 900
        scroll_distance = random.randint(viewport_height // 4, viewport_height // 2)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(0.2, 0.6))

class PlaywrightFactory:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._playwright is not None and self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-gpu',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-extensions',
                    '--window-size=1440,900',
                ],
            )

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None

    async def create_context(
        self,
        account_id: str,
        storage_state: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> BrowserContext:
        if self._browser is None:
            await self.start()
        assert self._browser is not None
        
        if proxy_url:
            clean_url = proxy_url.replace("http://", "").replace("https://", "").replace("socks5://", "")
            proxy = {"server": f"socks5://{clean_url}"}
        else:
            proxy = None
            
        user_agent = random.choice(USER_AGENTS)
        context = await self._browser.new_context(
            user_agent=user_agent,
            viewport={'width': 1440, 'height': 900},
            locale='en-US',
            ignore_https_errors=True,
            java_script_enabled=True,
            storage_state=storage_state,
            proxy=proxy,
        )
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        return context

async def apply_optional_stealth(page: Page) -> None:
    if stealth_async is None:
        return
    try:
        await stealth_async(page)
    except Exception as exc:
        logger.warning('playwright-stealth injection failed: %s', exc)

def storage_state_path(account_id: str) -> str:
    STORAGE_DIR.mkdir(exist_ok=True)
    return str(STORAGE_DIR / f'{account_id}.storage.json')