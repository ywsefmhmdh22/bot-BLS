import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from auth import BlsMoroccoAuthenticator, AuthenticationError, CaptchaChallenge
from core import (
    AccountRecord,
    Humanizer,
    PlaywrightFactory,
    ProxyManager,
    SecureVault,
    TelegramNotifier,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class AccountSession:
    email: str
    password: str
    details: Dict[str, Any]
    storage_state: Path
    id: str = field(init=False)

    def __post_init__(self) -> None:
        self.id = hashlib.blake2b(self.email.encode('utf-8'), digest_size=8).hexdigest()


class BlsBotEngine:
    def __init__(
        self,
        vault: SecureVault,
        notifier: TelegramNotifier,
        proxy_manager: ProxyManager,
        concurrency: int = 2,
    ) -> None:
        self.vault = vault
        self.notifier = notifier
        self.proxy_manager = proxy_manager
        self.factory = PlaywrightFactory(headless=False)
        self.authenticator = BlsMoroccoAuthenticator()
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        print("[*] تم تشغيل محرك البوت التلقائي بنجاح وجاهز لبدء المهام...")

    async def start(self) -> None:
        print("\n" + "="*50)
        print("[+] جاري فك تشفير وقراءة الحسابات المضافة للسيستم...")
        raw_accounts = self.vault.load_accounts()
        if not raw_accounts:
            print("[!] تحذير: لا توجد حسابات مضافة في الخزنة حالياً، يرجى إضافتها من الواجهة أولاً.")
            return

        print(f"[+] تم العثور على ({len(raw_accounts)}) حساب جاهز للعمل التلقائي.")
        
        sessions = []
        for acc in raw_accounts:
            state_path = Path(self.vault.vault_path.parent / 'storage' / f"{hashlib.blake2b(acc['email'].encode('utf-8'), digest_size=8).hexdigest()}.storage.json")
            sessions.append(
                AccountSession(
                    email=acc['email'],
                    password=acc['password'],
                    details=acc['details'],
                    storage_state=state_path,
                )
            )

        print("[*] جاري إطلاق المهام بالتوازي طبقاً للإعدادات المحددة...")
        print("="*50 + "\n")
        
        tasks = [self._process_account(session) for session in sessions]
        await self.factory.start()
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """دالة الإغلاق المفقودة لإنهاء المتصفح بشكل آمن وتنظيف الذاكرة"""
        print("[*] جاري إنهاء وإغلاق محرك البوت والبراوزر بشكل آمن...")
        try:
            await self.factory.stop()
            print("[✓] تم إغلاق كافة جلسات المتصفح بنجاح.")
        except Exception as e:
            print(f"[-] خطأ أثناء محاولة إغلاق المصنع: {e}")

    async def _process_account(self, account: AccountSession) -> None:
        async with self._semaphore:
            print(f"\n[🔄 الحساب الحالي: {account.email}] - بدء عملية الفحص وتأمين تسجيل الدخول...")
            attempt = 1
            max_attempts = 5
            
            while attempt <= max_attempts:
                proxy = self.proxy_manager.next_proxy()
                print(f"[{account.email} - محاولة {attempt}/{max_attempts}] 🌐 جاري تفعيل البروكسي: {proxy if proxy else 'بدون بروكسي'}")
                
                context = None
                try:
                    state_str = str(account.storage_state) if account.storage_state.exists() else None
                    if state_str:
                        print(f"[{account.email}] 💾 تم العثور على جلسة سابقة (Cookies)، جاري محاولة فتح المتصفح بها لتخطي الدخول المباشر...")
                    
                    context = await self.factory.create_context(
                        account_id=account.id,
                        storage_state=state_str,
                        proxy_url=proxy,
                    )
                    
                    blocked = {'status': False}
                    context.on('response', lambda res: self._inspect_response(res, blocked, proxy))
                    
                    print(f"[{account.email}] 🌎 جاري توجيه المتصفح السري لصفحة دخول موقع BLS المغرب...")
                    page = await context.new_page()
                    
                    await self.authenticator.ensure_logged_in(
                        context=context,
                        account={'email': account.email, 'password': account.password},
                        page=page
                    )
                    
                    if blocked['status']:
                        raise Exception("تم كشف المتصفح أو حظر البروكسي من قبل سيرفر الموقع (403/429)")

                    print(f"[🎉 نجاح باهر - {account.email}] تم تخطي صفحة تسجيل الدخول والبروكسي مستقر! جاري حفظ ملف الكوكيز للتخطي القادم...")
                    await context.storage_state(path=str(account.storage_state))
                    
                    print(f"[🚀 مراقبة - {account.email}] الحساب مستقر وجاهز تماماً لالتقاط المواعيد الآن وتحديثات السيلفي والتحويل لتليجرام.")
                    await self.notifier.send_message(f'✅ الحساب جاهز ومسجل دخول ومستقر: {account.email}')
                    
                    while True:
                        await asyncio.sleep(10)
                        
                except CaptchaChallenge as exc:
                    print(f"[⚠️ كابتشا - {account.email}] الموقع طلب حل كابتشا يدوية الآن.")
                    await self.notifier.send_message(f'⚠️ طلب كابتشا لحساب {account.email}: {exc}')
                    if context: await context.close()
                    return
                    
                except AuthenticationError as exc:
                    print(f"[❌ فشل الحساب - {account.email}] بيانات الدخول غير صحيحة أو الحقول تغيرت.")
                    await self.notifier.send_message(f'❌ فشل تسجيل دخول حساب {account.email}: {exc}')
                    if context: await context.close()
                    return
                    
                except Exception as exc:
                    print(f"[🚨 خطأ شبكة - {account.email}] حدثت مشكلة أثناء الاتصال: {exc}")
                    if blocked['status'] and proxy:
                        self.proxy_manager.mark_blocked(proxy)
                        print(f"[⛔ حظر بروكسي] تم وضع البروكسي {proxy} في القائمة السوداء تلقائياً لتجنب تكراره.")
                        await self.notifier.send_message(f'🚨 تم كشف البروكسي وحظره لحساب {account.email}. جاري تدوير البروكسي والبدء من جديد.')
                    else:
                        print(f"[⚠️ تنبيه] خطأ عام غير متعلق بالحظر أو البروكسي ميت.")
                    
                    if context:
                        try: await context.close()
                        except: pass
                    
                    attempt += 1
                    wait_time = 2 + attempt * 2
                    print(f"[*] جاري الانتظار لمدة {wait_time} ثواني قبل تدوير البروكسي والمحاولة التالية...")
                    await asyncio.sleep(wait_time)
            
            print(f"[❌ استسلام] تم استنفاذ جميع محاولات البروكسيات المتاحة لهذا الحساب: {account.email}")

    def _inspect_response(self, response: Any, blocked: Dict[str, bool], proxy: Optional[str]) -> None:
        status = response.status
        if status in (403, 429):
            blocked['status'] = True


if __name__ == '__main__':
    pass