import asyncio
from pathlib import Path
from typing import Dict, Optional

from playwright.async_api import BrowserContext, Page

from core import BASE_URL, Humanizer, apply_optional_stealth


class CaptchaChallenge(Exception):
    pass


class AuthenticationError(Exception):
    pass


class BlsMoroccoAuthenticator:
    # التحديث الجديد للمسارات ليتوافق مع صفحات الـ PHP الحالية للموقع
    login_paths = [
        '/arabic/login.php',
        '/login.php'
    ]

    def __init__(self) -> None:
        # الرابط المباشر الصحيح لصفحة تسجيل الدخول
        self.login_url = 'https://morocco.blsspainvisa.com/arabic/login.php'

    async def ensure_logged_in(self, context: BrowserContext, account: Dict[str, str], page: Optional[Page] = None) -> Page:
        if page is None:
            page = await context.new_page()
        await apply_optional_stealth(page)
        await page.goto(self.login_url, wait_until='networkidle')
        await Humanizer.pause(0.3, 0.7)

        if await self._is_already_authenticated(page):
            return page

        await self._perform_login(page, account)
        await asyncio.sleep(2)

        if await self._detect_captcha(page):
            raise CaptchaChallenge('Captcha challenge detected during login flow.')

        if not await self._is_already_authenticated(page):
            raise AuthenticationError('Login verification failed.')

        return page

    async def _is_already_authenticated(self, page: Page) -> bool:
        current_url = page.url
        # إذا كنا بره حقول الدخول أو مسار الـ login معناه تم الدخول بنجاح
        if not any(path in current_url for path in self.login_paths):
            return True
        login_form = page.locator('input[type="password"], input[name*="password"], input[id*="password"]')
        return await login_form.count() == 0

    async def _perform_login(self, page: Page, account: Dict[str, str]) -> None:
        # تحديث الممسكات (Selectors) لضمان العثور على الحقول الجديدة بدقة
        email_selector = 'input[name="email"], input[id="email"], input[type="email"]'
        password_selector = 'input[name="password"], input[type="password"]'
        submit_selector = 'button[type="submit"], input[type="submit"], button:has-text("المتابعة"), button:has-text("Continue")'

        email_field = page.locator(email_selector).first
        password_field = page.locator(password_selector).first

        if await email_field.count() == 0 or await password_field.count() == 0:
            raise AuthenticationError('Unable to locate login fields on the portal.')

        # كتابة البيانات بطريقة تحاكي البشر
        await Humanizer.type_text(page, email_selector, account['email'])
        await Humanizer.pause(0.5, 1.0)
        
        # بعض نسخ الموقع تطلب الضغط على المتابعة لإظهار الباسورد، السيستم سيتعامل معها
        try:
            if await password_field.is_hidden():
                await Humanizer.click(page, submit_selector)
                await asyncio.sleep(1.5)
        except:
            pass

        await Humanizer.type_text(page, password_selector, account['password'])
        await Humanizer.pause(0.4, 0.8)
        await Humanizer.click(page, submit_selector)
        await page.wait_for_load_state('networkidle', timeout=20000)

    async def _detect_captcha(self, page: Page) -> bool:
        selectors = [
            'iframe[src*="recaptcha"]',
            'iframe[src*="turnstile"]',
            '.g-recaptcha',
            '#cf-turnstile-wrapper'
        ]
        for selector in selectors:
            if await page.locator(selector).count() > 0:
                return True
        return False