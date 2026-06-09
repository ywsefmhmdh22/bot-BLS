import asyncio
import threading
import io
import time
import json
import queue
import re
import subprocess
import sys
import http.client
from urllib.parse import quote, urlparse, parse_qs, unquote

from telegram import Bot, InputFile
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Request as PlaywrightRequest, Response as PlaywrightResponse, Route
from PIL import Image
from bs4 import BeautifulSoup
from h2spacex import H2OnTlsConnection, h2_frames

class TimeoutException(Exception):
    pass

class By:
    ID = 'id'
    XPATH = 'xpath'
    CSS_SELECTOR = 'css selector'
    NAME = 'name'
    CLASS_NAME = 'class name'
    TAG_NAME = 'tag name'

class Keys:
    ARROW_DOWN = 'ArrowDown'
    ENTER = 'Enter'
    BACKSPACE = 'Backspace'
    TAB = 'Tab'
    ESCAPE = 'Escape'
    SPACE = ' '

class WebDriverWait:
    def __init__(self, driver, timeout):
        self.driver = driver
        self.timeout = timeout

    def until(self, condition):
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                result = condition(self.driver)
                if result:
                    return result
            except Exception:
                pass
            time.sleep(0.2)
        raise TimeoutException(f"Timeout waiting for condition {condition}")

class EC:
    @staticmethod
    def presence_of_element_located(locator):
        def _predicate(driver):
            element = driver.find_element(locator)
            return element
        return _predicate

class PlaywrightChromeOptions:
    def __init__(self):
        self.arguments = []
        self.experimental_options = {}
        self.user_agent = None

    def add_experimental_option(self, name, value):
        self.experimental_options[name] = value

    def add_argument(self, argument):
        if argument.startswith('--user-agent='):
            self.user_agent = argument.split('=', 1)[1]
        self.arguments.append(argument)

class PlaywrightRequestProxy:
    def __init__(self, request: PlaywrightRequest):
        self._request = request
        self.method = request.method
        self.url = request.url
        self.path = urlparse(self.url).path
        self.headers = dict(request.headers)
        self._body = self._extract_body(request)
        self.aborted = False
        self.modified = False

    def _extract_body(self, request: PlaywrightRequest):
        body = None
        try:
            body = request.post_data_bytes()
        except Exception:
            body = None
        if body is None:
            try:
                post_data = request.post_data
                if post_data is not None:
                    body = post_data.encode('utf-8')
            except Exception:
                body = b''
        return body or b''

    @property
    def body(self):
        return self._body

    @body.setter
    def body(self, value):
        if value is None:
            self._body = b''
        elif isinstance(value, bytes):
            self._body = value
        else:
            self._body = str(value).encode('utf-8')
        self.modified = True

    def abort(self):
        self.aborted = True

class PlaywrightResponseProxy:
    def __init__(self, response: PlaywrightResponse, body: bytes):
        self._response = response
        self.status_code = response.status
        self.status_text = response.status_text
        self.headers = dict(response.headers)
        self.body = body or b''
        self.modified = False

class PlaywrightAlert:
    def __init__(self, adapter: 'PlaywrightBrowserAdapter'):
        self._adapter = adapter

    @property
    def text(self):
        if self._adapter._last_dialog is None:
            return ''
        return self._adapter._last_dialog.message

    def accept(self):
        if self._adapter._last_dialog is not None:
            self._adapter._run(self._adapter._last_dialog.accept())
            self._adapter._last_dialog = None

class PlaywrightElement:
    def __init__(self, adapter: 'PlaywrightBrowserAdapter', element_handle):
        self._adapter = adapter
        self._handle = element_handle

    def click(self):
        return self._adapter._run(self._handle.click())

    def send_keys(self, *keys):
        for key in keys:
            if key in (Keys.ARROW_DOWN, Keys.ENTER, Keys.TAB, Keys.ESCAPE, Keys.SPACE):
                self._adapter._run(self._handle.press(key))
            else:
                self._adapter._run(self._handle.type(str(key)))

    def clear(self):
        return self._adapter._run(self._handle.fill(''))

    def get_attribute(self, name):
        return self._adapter._run(self._handle.get_attribute(name))

    @property
    def text(self):
        return self._adapter._run(self._handle.inner_text())

    def is_displayed(self):
        return self._adapter._run(self._handle.is_visible())

    def submit(self):
        return self._adapter._run(self._handle.evaluate('(element) => element.submit && element.submit()'))

    def select_by_value(self, value):
        return self._adapter._run(self._handle.select_option(value=value))

    def select_by_visible_text(self, text):
        return self._adapter._run(self._handle.select_option(label=text))

class PlaywrightBrowserAdapter:
    def __init__(self, page: Page, context: BrowserContext, loop: asyncio.AbstractEventLoop):
        self.page = page
        self.context = context
        self.loop = loop
        self.current_frame = None
        self.switch_to = self.SwitchTo(self)
        self._request_interceptor = None
        self._response_interceptor = None
        self._route_enabled = False
        self._last_dialog = None
        self.page.on('dialog', self._on_dialog)

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def _on_dialog(self, dialog):
        self._last_dialog = dialog

    def _ensure_route(self):
        if self._route_enabled:
            return
        asyncio.run_coroutine_threadsafe(self.context.route('**/*', self._route_handler), self.loop).result()
        self._route_enabled = True

    def _convert_argument(self, argument):
        return getattr(argument, '_handle', argument)

    @property
    def request_interceptor(self):
        return self._request_interceptor

    @request_interceptor.setter
    def request_interceptor(self, callback):
        self._request_interceptor = callback
        self._ensure_route()

    @property
    def response_interceptor(self):
        return self._response_interceptor

    @response_interceptor.setter
    def response_interceptor(self, callback):
        self._response_interceptor = callback
        self._ensure_route()

    async def _route_handler(self, route: Route, request: PlaywrightRequest):
        proxy_request = PlaywrightRequestProxy(request)

        if self._request_interceptor is not None:
            try:
                self._request_interceptor(proxy_request)
            except Exception as ex:
                print('Request interceptor error:', ex)

        if proxy_request.aborted:
            await route.abort()
            return

        if proxy_request.modified and self._response_interceptor is None:
            await route.continue_(headers=proxy_request.headers, post_data=proxy_request.body, url=proxy_request.url)
            return

        if self._response_interceptor is not None:
            if proxy_request.modified:
                response = await route.fetch(headers=proxy_request.headers, post_data=proxy_request.body, url=proxy_request.url)
            else:
                response = await route.fetch()
            body = await response.body()
            proxy_response = PlaywrightResponseProxy(response, body)
            try:
                self._response_interceptor(proxy_request, proxy_response)
            except Exception as ex:
                print('Response interceptor error:', ex)
            await route.fulfill(
                status=proxy_response.status_code,
                headers=proxy_response.headers,
                body=proxy_response.body,
            )
            return

        await route.continue_()

    def _current_frame(self):
        return self.current_frame or self.page.main_frame

    def _locator_from_tuple(self, locator):
        if not isinstance(locator, (list, tuple)) or len(locator) != 2:
            raise ValueError('Locator must be a tuple like (By.ID, value)')
        by, value = locator
        if by == By.ID:
            return f'#{value}'
        if by == By.NAME:
            return f'[name="{value}"]'
        if by == By.CSS_SELECTOR:
            return value
        if by == By.XPATH:
            return f'xpath={value}'
        if by == By.CLASS_NAME:
            return f'.{value}'
        if by == By.TAG_NAME:
            return value
        return value

    def find_element(self, locator):
        selector = self._locator_from_tuple(locator)
        handle = self._run(self._current_frame().wait_for_selector(selector, timeout=10000))
        if handle is None:
            raise Exception(f'Element not found: {locator}')
        return PlaywrightElement(self, handle)

    def find_elements(self, locator):
        selector = self._locator_from_tuple(locator)
        handles = self._run(self._current_frame().query_selector_all(selector))
        return [PlaywrightElement(self, handle) for handle in handles]

    def get(self, url):
        return self._run(self.page.goto(url, wait_until='domcontentloaded'))

    def execute_script(self, script, *args):
        converted_args = [self._convert_argument(arg) for arg in args]
        return self._run(self.page.evaluate(script, *converted_args))

    def get_screenshot_as_png(self):
        return self._run(self.page.screenshot(type='png'))

    def get_cookies(self):
        return self._run(self.context.cookies())

    def add_cookie(self, cookie):
        payload = {
            'name': cookie.get('name'),
            'value': cookie.get('value'),
            'path': cookie.get('path', '/'),
        }
        if 'domain' in cookie:
            payload['domain'] = cookie['domain']
        if 'expiry' in cookie:
            try:
                payload['expires'] = int(cookie['expiry'])
            except Exception:
                pass
        return self._run(self.context.add_cookies([payload]))

    def delete_all_cookies(self):
        return self._run(self.context.clear_cookies())

    def get_window_size(self):
        viewport = self.page.viewport_size or {'width': 1440, 'height': 900}
        return {'width': viewport['width'], 'height': viewport['height']}

    def close(self):
        try:
            self._run(self.page.close())
        except Exception:
            pass
        try:
            self._run(self.context.close())
        except Exception:
            pass

    def refresh(self):
        return self._run(self.page.reload())

    def switch_to_frame(self, frame_ref):
        if isinstance(frame_ref, int):
            frames = self.page.frames
            if frame_ref < 0 or frame_ref >= len(frames):
                raise Exception(f'Frame index out of range: {frame_ref}')
            self.current_frame = frames[frame_ref]
        else:
            frame = self.page.frame(name=frame_ref)
            if frame is None:
                raise Exception(f'Frame not found: {frame_ref}')
            self.current_frame = frame

    class SwitchTo:
        def __init__(self, adapter):
            self._adapter = adapter

        def frame(self, frame_ref):
            self._adapter.switch_to_frame(frame_ref)

        def default_content(self):
            self._adapter.current_frame = None

        @property
        def alert(self):
            return PlaywrightAlert(self._adapter)

class ActionChains:
    def __init__(self, driver):
        self.driver = driver
        self.steps = []

    def move_by_offset(self, x, y):
        self.steps.append(('move', x, y))
        return self

    def click(self):
        self.steps.append(('click',))
        return self

    def perform(self):
        x = 0
        y = 0
        for step in self.steps:
            if step[0] == 'move':
                x += step[1]
                y += step[2]
                self.driver._run(self.driver.page.mouse.move(x, y))
            elif step[0] == 'click':
                self.driver._run(self.driver.page.mouse.click(x, y))
        self.steps.clear()

class Select:
    def __init__(self, element):
        self.element = element

    def select_by_value(self, value):
        return self.element.select_by_value(value)

    def select_by_visible_text(self, text):
        return self.element.select_by_visible_text(text)

class PlaywrightManager:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.playwright = None
        self.browser = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._initialize_browser()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _initialize_browser(self):
        future = asyncio.run_coroutine_threadsafe(self._start_browser(), self.loop)
        self.playwright, self.browser = future.result(timeout=60)
        self._ready.set()

    async def _start_browser(self):
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-gpu',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-extensions',
                '--disable-infobars',
            ],
        )
        return playwright, browser

    def create_chrome(self, options):
        self._ready.wait()
        future = asyncio.run_coroutine_threadsafe(self._create_adapter(options), self.loop)
        return future.result(timeout=60)

    async def _create_adapter(self, options):
        user_agent = options.user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        context = await self.browser.new_context(
            user_agent=user_agent,
            locale='en-US',
            viewport={'width': 1440, 'height': 900},
            ignore_https_errors=True,
            java_script_enabled=True,
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        return PlaywrightBrowserAdapter(page, context, self.loop)

    def shutdown(self):
        if self.browser:
            asyncio.run_coroutine_threadsafe(self.browser.close(), self.loop).result(timeout=30)
        if self.playwright:
            asyncio.run_coroutine_threadsafe(self.playwright.stop(), self.loop).result(timeout=30)
        self.loop.call_soon_threadsafe(self.loop.stop)

PLAYWRIGHT_MANAGER = PlaywrightManager()

class PlaywrightDriverFactory:
    def Chrome(self, options):
        return PLAYWRIGHT_MANAGER.create_chrome(options)

ChromeOptions = PlaywrightChromeOptions

class MyCustomError(Exception):
    pass

class BlsFacilateWork:
    # Map commonly used names as class attributes so existing method code
    # referencing `self.<name>` continues to work even after moving imports.
    webdriver = PlaywrightDriverFactory()
    Keys = Keys
    By = By
    WebDriverWait = WebDriverWait
    EC = EC
    ActionChains = ActionChains
    Image = Image
    io = io
    time = time
    Chrome = None
    ChromeOptions = PlaywrightChromeOptions
    Select = Select
    json = json
    Alert = None
    BeautifulSoup = BeautifulSoup
    queue = queue
    quote = quote
    urlparse = urlparse
    parse_qs = parse_qs
    unquote = unquote
    H2OnTlsConnection = H2OnTlsConnection
    h2_frames = h2_frames

    def get_available_date_hours(self, headerr: str, bodyy: str, session_number: str) -> None:
        # h2spacex and re are imported at module level when available
        
        h2_conn = H2OnTlsConnection(
    hostname='algeria.blsspainglobal.com',
    port_number=443
)

        h2_conn.setup_connection()
        headers = headerr
        body = bodyy
        stream_ids_list = [1,3,5,7,9,11,13,15,17,19,21,23,25,27,29,31,33,35,37,39,41,43,45,47,49,51,53,55,57,59]
        self.key_date_list= {'2024-02-21': 43, '2024-02-22': 45, '2024-02-23': 47, '2024-02-24': 49, '2024-02-25': 51, '2024-02-28': 53, '2024-02-29': 55, '2024-02-30': 57, '2024-02-31': 59, '2024-03-01': 1, '2024-03-04': 3, '2024-03-05': 5, '2024-03-06': 7, '2024-03-07': 9, '2024-03-08': 11, '2024-03-11': 13, '2024-03-12': 15, '2024-03-13': 17, '2024-03-14': 19, '2024-03-15': 21, '2024-03-18': 23, '2024-03-19': 25, '2024-03-20': 27, '2024-03-21': 29, '2024-03-22': 31, '2024-03-25': 33, '2024-03-26': 35, '2024-03-27': 37, '2024-03-28': 39, '2024-03-29': 41}
        date_key_list = {43:"2024-02-21",45:"2024-02-22",47:"2024-02-23",49:"2024-02-24",51:"2024-02-25",53:"2024-02-28",55:"2024-02-29",57:"2024-02-30",59:"2024-02-31",1:"2024-03-01",3:"2024-03-04",5:"2024-03-05",7:"2024-03-06",9:"2024-03-07",11:"2024-03-08",13:"2024-03-11",15:"2024-03-12",17:"2024-03-13",19:"2024-03-14",21:"2024-03-15",23:"2024-03-18",25:"2024-03-19",27:"2024-03-20",29:"2024-03-21",31:"2024-03-22",33:"2024-03-25",35:"2024-03-26",37:"2024-03-27",39:"2024-03-28",41:"2024-03-29"}
        
        date_key_list_2 = {1:"2024-02-24",3:"2024-02-02",5:"2024-02-03",7:"2024-02-04",9:"2024-02-07",11:"2024-02-08",13:"2024-02-9",15:"2024-02-10",17:"2024-02-11",19:"2024-02-14",21:"2024-02-15",23:"2024-02-18",25:"2024-02-21",27:"2024-02-2",29:"2024-02-23",31:"2024-02-01",33:"2024-02-25",35:"2024-02-28",37:"2024-02-29",39:"2024-02-30",41:"2024-02-31"}



        all_headers_frames = []  # all headers frame + data frames which have not the last byte
        all_data_frames = []
        for s_id in stream_ids_list:
            new_body = modified_url = re.sub(r"(AppointmentDate=)\d{4}-\d{2}-\d{2}", rf"AppointmentDate={date_key_list[s_id]}", body)
    
            header_frames_without_last_byte, last_data_frame_with_last_byte = h2_conn.create_single_packet_http2_post_request_frames(
        method='POST',
        headers_string=headers,
        scheme='https',
        stream_id=s_id,
        authority="algeria.blsspainglobal.com",
        body=new_body,
        path='/DZA/blsappointment/gasd'
    )
    
            all_headers_frames.append(header_frames_without_last_byte)
            all_data_frames.append(last_data_frame_with_last_byte)
        temp_headers_bytes = b''
        for h in all_headers_frames:
            temp_headers_bytes += bytes(h)
            
        temp_data_bytes = b''
        for d in all_data_frames:
            temp_data_bytes += bytes(d) 
        h2_conn.send_frames(temp_headers_bytes)

    # wait some time
        time.sleep(0.1)
        h2_conn.send_frames(temp_data_bytes)
        resp = h2_conn.read_response_from_socket(_timeout=10)
        self.frame_parser[session_number] = h2_frames.FrameParser(h2_connection=h2_conn)
        self.frame_parser[session_number].add_frames(resp)
        for sid in stream_ids_list:
            try:
                print(f"------ headers for date {date_key_list[sid]}")
                print(type(self.frame_parser[session_number].headers_and_data_frames[sid]['header']))
                print(self.frame_parser[session_number].headers_and_data_frames[sid]['header'])
                print(f"------ response for date {date_key_list[sid]}")
                print(self.frame_parser[session_number].headers_and_data_frames[sid]['data'])
                print("----------------------   ----------------------------")
            except:
                pass
#frame_parser.show_response_of_sent_requests()

# close the connection to stop response parsing and exit the script
        h2_conn.close_connection()
    def modify_response(self, request, response, session_number: str) -> None:
        # urllib.parse.quote, re and BeautifulSoup are available at module scope
        if request.method == 'GET' and   '/DZA/CaptchaPublic/GenerateCaptcha' in request.path and self.auto_captcha :
            original_body = response.body
            soup = BeautifulSoup(original_body, 'html.parser')
            input_element = soup.find_all('input', {'name': '__RequestVerificationToken'})

# Extract the value attribute
            if input_element[-1]:
                self.token_value = input_element[-1]['value']
                print("Token Value:", self.token_value)
            else:
                print("Input element not found.")
        
        
        if request.method == 'GET' and (request.path.upper()=="/DZA/ACCOUNT/LOGIN" ) and self.all_manual:
            original_body = response.body
            email = b'type = "text" value="{}"'.replace(b'{}',self.data[session_number][0].encode('utf-8'))
            modified_body = original_body.replace(b'type = "text"', email)
            password = b'type="password" value="{}"'.replace(b'{}',"A2z0@I0z0".encode('utf-8'))
            modified_body = modified_body.replace(b'type="password"', password)
            response.body = modified_body
            
        if request.method == 'POST' and   '/DZA/bls/vt8809/' in request.path:
            
            self.all_date[session_number] = False
            location_value = "0566245a-7ba1-4b5a-b03b-3dd33e051f46" if self.data[session_number][1] == "alger" else "8457a52e-98be-4860-88fc-2ce11b80a75e"
            appointmentFor_value = "Individual" if self.data[session_number][2] == "1" else "Family"   
            
            value= "/DZA/blsappointment/manageappointment/a838?appointmentFor="+appointmentFor_value+"&applicantsNo="+self.data[session_number][2]+"&visaType=c805c157-7e8f-4932-89cf-d7ab69e1af96&visaSubType=b563f6e3-58c2-48c4-ab37-a00145bfce7c&appointmentCategory="+self.visa_category[session_number]+"&location="+location_value+"&missionId=&data="+str(self.captcha_data[session_number])
            
            vv = b'{"success":true,"available":true,"returnUrl":"{}"}'.replace(b'{}',value.encode('utf-8'))
            
            if b"returnUrl" in response.body and b"appointment" in response.body :
                self.available_date=True
            
            #print(value)
            
            #response.body = vv
        if (("cib.satim.dz" in request.url )and (request.method == 'GET') and (response.headers.get("Content-Type", "").startswith("text/html")) and self.paymentFill) :
            print("in cib.satim.dz")
            print(request.path)
            
            original_body = response.body
            ipan = b'id="iPAN" value="{}"'.replace(b'{}',self.data[session_number][3].encode('utf-8'))
            modified_body = original_body.replace(b'id="iPAN"', ipan)
            icvc = b'id="iCVC" value="{}"'.replace(b'{}',self.data[session_number][4].encode('utf-8'))
            modified_body = modified_body.replace(b'id="iCVC"', icvc)
            itext = b'id="iTEXT" value="{}"'.replace(b'{}',self.data[session_number][5].encode('utf-8'))
            modified_body = modified_body.replace(b'id="iTEXT"', itext)
            expiration_date = b'Expiration Date {}  '.replace(b'{}',self.data[session_number][6].encode('utf-8'))
            modified_body = modified_body.replace(b'Expiration Date', expiration_date)
            
            response.body = modified_body
            
            print("-----------response.body--------------")
            print(response.body)
            
            print("------------modified_body--------")
            print(modified_body)
            
        if  "/js/site.js" in request.url and request.method == 'GET' and False:   
            original_body = response.body
            
            modified_body = original_body.replace(b'document.getElementById("global-overlay").style.display = "block";', b'document.getElementById("global-overlay").style.display = "none";')
            response.body = modified_body
        if request.method == 'POST' and   request.path=='/DZA/query/UploadProfileImage':
            original_body = response.body.decode('utf-8')
            j = self.json.loads(original_body)
            #print(j["fileId"])
            if j["fileId"] :
                if len(self.data[session_number]) != 8:
                    self.data[session_number].append(j["fileId"])
                else:
                    self.data[session_number][7]=j["fileId"]
                    
                with open('data.json', 'w') as f4:
                    self.json.dump(self.data, f4)
            
        
        
        if  "/DZA/blsAppointment/ManageAppointment".upper() in request.url.upper() and response.headers.get("Content-Type", "").startswith("text/html") :
            self.all_date[session_number] = False
            original_body = response.body
            modified_body = original_body.replace(b'enable(false)', b'enable(true)')
            modified_body = modified_body.replace(b'valid == false', b'false')
            modified_body = modified_body.replace(b'$("#btnVerifyAppointment").hide();', b'')
            modified_body = modified_body.replace(b'$("#btnVerifyEmail").hide();', b'')
            modified_body = modified_body.replace(b'$(".upload-photo-btn").hide();', b'')
            modified_body = modified_body.replace(b'$("#btnVerifyApplicant").hide();', b'')
            if len(self.data[session_number])==8 :
                erre = b'/DZA/query/getfile?fileid={}'.replace(b'{}',self.data[session_number][7].encode('utf-8'))
                modified_body = modified_body.replace(b'/assets/images/avatar/01.jpg', erre)
                erre = b'name="ApplicantPhotoId" type="hidden" value="{}"'.replace(b'{}',self.data[session_number][7].encode('utf-8'))
                modified_body = modified_body.replace(b'name="ApplicantPhotoId" type="hidden" value=""', erre)
            
            response.body = modified_body
            
        if request.method == 'POST' and '/DZA/blsappointment/gasd' in request.path:
            
            response_content = response.body.decode('utf-8')
            #print(response_content)
            print("-------- request header ------")
            print(request.headers)
            print("-------- request body --------")
            print(request.body)
            
            print("the true response")
            print(response.body)
            print("-----the true response------")
            
            pattern = r'AppointmentDate=([^\&]+)'

# Use re.search to find the first match in the string
            match = re.search(pattern, request.body.decode('utf-8'))
            
            appointment_date = match.group(1)
            print("Appointment Date:", appointment_date)
            #request.path= "/dza/bls"
            
            
            

            if self.dateGetType == 1:
                response_to_send = b'[{"Name":"08:00-08:30","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"08:00-08:30","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"08:30-09:00","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"08:30-09:00","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"09:00-09:30","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"09:00-09:30","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"09:30-10:00","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"09:30-10:00","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"10:00-10:30","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"10:00-10:30","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"10:30-11:00","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"10:30-11:00","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"11:00-11:30","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"11:00-11:30","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null},{"Name":"11:30-12:00","Value":null,"Code":"1","Count":1,"EnumId":0,"Error":null,"DataType":null,"ClassName":null,"title":null,"key":null,"lazy":false,"selected":false,"DepartmentOwnerUserId":null,"HasChildren":false,"UserId":null,"Id":"11:30-12:00","CreatedDate":"0001-01-01T00:00:00","CreatedBy":null,"LastUpdatedDate":"0001-01-01T00:00:00","LastUpdatedBy":null,"IsDeleted":false,"SequenceOrder":null,"CompanyId":null,"LegalEntityId":null,"DataAction":0,"Status":0,"VersionNo":0,"PortalId":null}]'
                response.body=response_to_send 
                
                
            if self.all_date[session_number] == False and self.dateGetType == 2:
            
                self.get_available_date_hours(str(request.headers),request.body.decode('utf-8'),session_number)
                self.all_date[session_number]  = True
                response.body=self.frame_parser[session_number].headers_and_data_frames[self.key_date_list[appointment_date]]['data']
                multiline_string = self.frame_parser[session_number].headers_and_data_frames[self.key_date_list[appointment_date]]['header']
                lines = multiline_string.splitlines()

# Extract all lines except the first one
                response.status_code = 200
                remaining_lines = "\n".join(lines[1:])
                #print(remaining_lines)
                new_headers = dict(re.findall(r'(?P<name>.*?): (?P<value>.*?)\n', remaining_lines))
                #response.headers.clear()
                for name, value in new_headers.items():
                    response.headers[name] = value
            elif self.all_date[session_number] == True and self.dateGetType == 2:
                response.body=self.frame_parser[session_number].headers_and_data_frames[self.key_date_list[appointment_date]]['data']
                multiline_string = self.frame_parser[session_number].headers_and_data_frames[self.key_date_list[appointment_date]]['header']
                lines = multiline_string.splitlines()

    # Extract all lines except the first one
                response.status_code = 200
                remaining_lines = "\n".join(lines[1:])
                    #print(remaining_lines)
                new_headers = dict(re.findall(r'(?P<name>.*?): (?P<value>.*?)\n', remaining_lines))
                    #response.headers.clear()
                for name, value in new_headers.items():
                    response.headers[name] = value
                
            
        
               
        
    def interceptor(self, request, session_number: str) -> None:
        # urllib.parse.quote, http.client, re, subprocess and threading are module-level
    # Check if the request path matches '/DZA/bls/visatype'
    
    
        if request.method ==    'POST' and '/DZA/CaptchaPublic/SubmitCaptcha' in request.path and self.auto_captcha :
            new_body = b'SelectedImages=nvzfagp%2Cslcfz%2Cokbpks&Id=1edaac76-1a34-4815-9112-482f6efc47b5&Captcha=Bf0KU6r4PHzEtR9My6uzzPdKSddwylXruf9ExVC2AqwgiR5ycEqqKD0n6sTVxpXFAMEiyxKbKypeIJeRKluBctR3LnnxxPJy2rnOI%2BvCTXd%2FdFEObgxYW8YwyGW58oGBY3%2BnQ87uJvgs3HZgc%2BZOft1fFK82dImahOv4G4ZaWzOqa%2FP%2F5MCDtejXzT9Oz0ZR7ADLJ6J%2BMzD2LrB8OZpKBsr5JdNjSEfcIQHHX2aY%2Fc4Ax%2BXw%2BFLWvYTC4N6oeceaAWvVATxJpBxADKkI79Ltu0o1Mw6cF2lgS8IwQsXuzLTQYCnRbl7D1dh8O556BQackiPdUnRtfWHbsnpXSESSH%2FJfofZ%2FkIZak4qxQ6%2BBthlxsg6H2hVJx%2B44GdBwkoDN4V7E47kPAlSRiZtJUzoyozyG8rvqKeXwbucRyLBywkuntGcq0k%2BIi1JFe6RGqjjMNaZhtN6Tu1TNkmbkgWDN9INioEUgYRpcKO%2BMNCDJh62yWwsZQOOetq3FVlxmCs3lwsy3LJJfUI8DkK3KY9b2T87JmHPvRgur9zY5prh3MyYPTjUKMFd20qkQenYtXOrQi9aM3tUBRzffyydaO6aWjy0iF5km9WXBZKBdG07NY0SUBkd55Ay4Sl1HWmb7UCmPN4u2I90HWPSj2GT8pd2BSRJLuiCkekZ4Db5OCiUx%2BHiCU9Tmsbbk05oXQ5Gd1O%2FenEaa4blRkizW0zwohCUY8Kz8fD%2BSEUPeoubqMCi%2BK%2FlYjxygULdORM06dKLsRkfmpQYbloVKO8rfCU6V3am9HNVR6Et90HLWLlrymwAvSZGgW8hfteLQPA6NHfbsgOq4inPZfarrjy0tseo1a%2Fr55zlHmKVmPY%2BM3LOkfO3cluI7GQBy3FXR1Y5NkKb8hfcS%2FV77k95fgLob%2BYs5s6Nj1fFirhrQfWuYi%2FJZ3Vi6rMUnAfU2%2FuECs3Ffsk%2BQCNTnjq1mekfwlMOL2u4H%2BqEzXchmwAp2gOQg%2FYd2%2B4zFGe%2BCnsKzuFS4Sfl9vMlZnXM%2BANn1eQoENjjjwM0dQmV4ls7CIa4gv7cGPD2WZuM0Wh%2F9gKSDmuCBkApFpozNz5Y29yPXMZ1Iydj6erDWMy9%2B3Ibjn4OxVSCLHAAAK74EYLzeauXLJ2NuTAmtusGVBDHGQfSxhE3J34%2BZsP8Yq62k5xYWBUxLcJSqCMHHXXyFQ9wiTc8u9PEuuuNuVX4Bst%2B7L8pXoaXoBCAMe75I%2BVWCb6XP1mGJcKDfM9AoomVBoVFNyu8Hj4ttqQQ0uconXigmDkVWzETWi4CYTyEhqN5fTqQ2eh34VOHROP2lgZ1NU6w5I7OHsB7sGL0L26Vk3NOcSXKrrF2BSWwB788YyYolr%2BN3lqEax0EMAa4udX22tY1hhhDG2GUOim7Xqnc1Vx8%2FkJk68Bu0fBb4mjidm5XFrG7Ou1ud1shJvclDVgFUTBG1oQC%2Fk0O%2FW%2Fu3k4Z14J9trPoQExN8E6KcxtHIDTyjxz7AFroXaJqLHlD9CEYeDF8wkaXSZb8vWmbfAQwmOPcI6tW5R40XcPKqOMW6CKX4XqoKAu%2FB2Yq0qerLkBucZiJJcWsS4t1Sv5phTD92TYGJzJjWKVujJXE0hXJR6ijVnOEOoL4dg2DSWJAT3rCot2QvqwZACpni3sBcH9b3O36CscHpVS5K0ltWwoFfpockbAynFZMutAc3t7L8SmwSzxdxa4qRVc0RRE5LTbIoOXv2f4X3NDxqA0injEJEkbnd7bpD1hw9EMAtEcJSISvG83qEAqoVN%2FLiV5aS3DOTRTRVtUbC7gw5eDn7l0RlNWB95Zezel55UkjI4cSXUJlGWZ1RWZH4SMoE4QgJFHvRLVdZn%2FPl4%2FnOzfind3Z7%2B2uVv6oL08KOUyTApBh78SrViMyVpeENAjR%2Bo4oYE1YNKyhQPE%2BIqX6KE7%2BrmnpWi8yXrONvOcBIIEQaiYWJpV2T81n%2F%2Bom7goKZe8uz3GDpcuem3HFhtfakGG27ek3L4iO%2Btva4Kx4IE3ANSjJ2zz%2FHWy1%2BUz5DpCPSdXeGjIWiR3HleZztADA4S0VtavwQydbPcYNOQfpj6eDyvdptHepq3hmPgbZPu6Jzfy3MLSQNCmH2d4df%2FbhjvQTJenv%2Faam37cI4YOyPysFYLlalD3PGnBA14pZOre1yUAPEho7GamK0iMIm7cW50pZaci9181esybkFCwoF%2FRkl2GN5Eq5uNKhjUix0gukgczd0wWKb2KqYiPVBKt6ZIIraRTgbcZOKAB4K6emO3oIJZHrv%2ByjiFES0ORmjUeDkQbHyG1M88e97KV8ORBqMWUVtXggJgKeTLJ87KWY5OMhpA2sbxusdvGijcIGMK9XVCbPGzWdnDMDweaUoHIbejeas5rbuQpJckFY1i6BO0lqSuLcxkj18w%2FTi7LZs7E%2FgTCwDaMHRznROV6ahFMx1JpTHF%2Bx9GyIY6OkCWq5bd5G1KbXw72W00VsJGJtot3tZbdULGKlibhgTNa%2BYQdawEHAAPtfl7Xw0ATu0q9b97e5IMJF01HgGfGUs%2BRFnNX3Moy4TJD2liowbdK7TNG9hTyy9hxmuUvo1hkx2sYZihykxmbTYh%2Bk%2Bs8ml%2FuWlrbqIujwYjCwHF%2BVy%2BJRpO%2FqIyXWVEJgo6iM%2FZYcccUvVpbmkB9UYObYsKq%2BTrAeCeAhqupVrFMrzFpbSN1iX3UVL20FNpGbKiX8YAOIoezL9i2TiTWaf2UKv%2BTY%3D&__RequestVerificationToken={}&X-Requested-With=XMLHttpRequest'
            final_body = new_body.replace(b'{}',self.token_value.encode())
            print(final_body)
            request.body = final_body
            del request.headers['Content-Length']
            request.headers['Content-Length'] = str(len(request.body))
        if request.method ==    'POST' and '/DZA/blsappointment/gasd' in request.path:
            if self.dateGetType == 2:
                request.abort()
            
        if request.method == 'GET' and 'stopauto' in request.path:
            self.stop_auto =True
            request.abort()
            
        if request.method == 'POST' and ("/DZA/bls/vt/" in request.path ) :
            if b"15044668-9bb4-477d-918b-4809370190b9" in request.body:
                self.visa_category[session_number] = "15044668-9bb4-477d-918b-4809370190b9"
            elif b"37ba2fe4-4551-4c7d-be6e-5214617295a9" in request.body :
                self.visa_category[session_number] = "37ba2fe4-4551-4c7d-be6e-5214617295a9"
            else :
                self.visa_category[session_number] = "5c2e8e01-796d-4347-95ae-0c95a9177b26"
                
            
        
        if request.method == 'POST' and (request.path=="/DZA/account/loginPost" ) :
            browser = self.session_chrome.get(session_number)
            if browser is None:
                raise MyCustomError(f"No browser session available for session_number={session_number}")

            cookiess = browser.get_cookies()
            list_cookies=""
            for cookie_value in cookiess:
                list_cookies+= f"{cookie_value['name']}={cookie_value['value']}; "
            print(cookiess)
            request.path="/DZA/account/loGinPost"
            if self.login_all:
                the_email_passed = quote(self.data[session_number][0], safe='')
                original_string = request.body.decode('utf-8')
                for i in self.data:
                    print("--- the body loGin ---  " + str(i))
                    the_email = quote(self.data[i][0], safe='')
                    modified_string = original_string.replace(the_email_passed, the_email)
                    curl_command = f"curl -X POST -H 'Cookie: {list_cookies}' -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36' -H 'Origin: https://algeria.blsspainglobal.com' -H 'Referer: https://algeria.blsspainglobal.com/DZA/Account/LogIn?ReturnUrl=%2FDZA%2Fbls%2Fvtv9850' -d '{modified_string}' https://algeria.blsspainglobal.com/DZA/account/loGinPost -i"
                    print(curl_command)
        # Execute the curl command using subprocess
                    result = subprocess.run(curl_command, shell=True, check=True, text=True, capture_output=True)
            
                    set_cookie_headers = re.findall(r'set-cookie:(.*?)\n', result.stdout, re.I)
                    print('set_cookie_headers')
                    print(result.stdout)
            #print(set_cookie_headers)

                    cookies = {}
                    for header in set_cookie_headers:
                        header = header.strip()
                        cookie_attributes = header.split(';')

            # Split the first attribute into name and value
                        name, value = cookie_attributes[0].strip().split('=', 1) if '=' in cookie_attributes[0] else (cookie_attributes[0].strip(), '')

                # Add the cookie to the dictionary
                        cookies[name] = value
            
                    for cookie_name, cookie_value in cookies.items():
                        print(f"Cookie Name: {cookie_name}")
                        print(f"Cookie Value: {cookie_value}")
                        self.cookie = {
            "name'": cookie_name,
            "value": cookie_value,
            "domain": "algeria.blsspainglobal.com" 
        }  
                    self.Cookies[i]= {
            "name": cookie_name,
            "value": cookie_value,
            "domain": "algeria.blsspainglobal.com"}
            #self.loggedIn = True
                print(self.Cookies)
                with open('cookies.json', 'w') as f3:
                        self.json.dump(self.Cookies, f3) 
            
        if "gototime" in request.path:
            self.all_date[session_number] = False
            location_value = "0566245a-7ba1-4b5a-b03b-3dd33e051f46" if self.data[session_number][1] == "alger" else "8457a52e-98be-4860-88fc-2ce11b80a75e"
            appointmentFor_value = "Individual" if self.data[session_number][2] == "1" else "Family"
            
            request.path= "/DZA/blsAppointment/ManageAppointment/9df8?appointmentFor="+appointmentFor_value+"&applicantsNo="+self.data[session_number][2]+"&visaType=c805c157-7e8f-4932-89cf-d7ab69e1af96&visaSubType=b563f6e3-58c2-48c4-ab37-a00145bfce7c&appointmentCategory="+self.visa_category[session_number]+"&location="+location_value+"&missionId=&data="+str(self.captcha_data[session_number])
            print(request.path)
        if '/DZA/bls/vt8809/' in request.path and request.method == 'GET':
                # Extracting query parameters
            #request.path = "/DZA/bls/vt/30e10953"
            query_params = request.params

                # Check if 'data' parameter exists and modify its value
            if 'data' in query_params:
                    # Modify the value of 'data' parameter
                    #query_params['data'] = ['new_value']

                    # Update the params in the request
                    #request.params = query_params
                self.dataToTime=quote(query_params['data'], safe='')
                self.captcha_data[session_number] = quote(query_params['data'], safe='')
                print("data:", self.dataToTime)
           
        if request.method == 'POST' and (request.path=="/DZA/blsappointment/SubmitLivenessDetection" or request.path=="/LivenessDetection" or request.path== "/DZA/blsappointment/livenessdetection" or "liveness".upper() in request.path.upper()) and self.selfieFill:
            print(str(self.current_user)+ '  from intercept')
            import re
                
            original_request_body = request.body
            matches = re.finditer(b' name="([^"]+)"', original_request_body)
            name_value_pairs = []
            content_type_header = request.headers.get('Content-Type')
            boundary_match = re.search(r'boundary=([^\s;]+)', content_type_header)
            boundary = boundary_match.group(1)
            request_body = b''
            for match in matches:
                pair = match.group(1)
                name_value_pairs.append(pair)

            print(name_value_pairs) 
            request_body += b'--' + boundary.encode('utf-8') + b'\r\n'  
            for name in name_value_pairs:
                            
                if name != b"image1" and name != b"image2":
                    value = re.search(rb'(?<="'+ re.escape(name) + rb'"\r\n\r\n).*?(?=\r\n-+)', original_request_body)  
                    if value:
                        request_body += b'Content-Disposition: form-data; name="'+re.escape(name)+b'"\r\n\r\n' 
                        request_body += value.group(0) + b'\r\n'
                            #if name == b"isMobile" :
                                #request_body += b'--' + boundary.encode('utf-8') + b'--\r\n'
                            #else:
                               # request_body += b'--' + boundary.encode('utf-8') + b'\r\n'
                        value = value.group(0).decode('utf-8')  # Convert matched bytes to a string
                        print(f"Value for {name}: {value}")
                    else:
                        print(f"No value found for {name}")
                if name == b"image1" :
                    request_body += b'Content-Disposition: form-data; name="image1"; filename="blob"\r\n'
                    request_body += b'Content-Type: image/png\r\n\r\n'
                    with open("./BlsImg/"+self.data[session_number][0]+"1.png", 'rb') as image1:
                        request_body += image1.read() + b'\r\n'
                            #request_body += b'--' + boundary.encode('utf-8') + b'\r\n'
                if name == b"image2" :
                    request_body += b'Content-Disposition: form-data; name="image2"; filename="blob"\r\n'
                    request_body += b'Content-Type: image/png\r\n\r\n'
                    with open("./BlsImg/"+ self.data[session_number][0] +"2.png", 'rb') as image2:
                        request_body += image2.read() + b'\r\n'
                            #request_body += b'--' + boundary.encode('utf-8') + b'\r\n'
                if name == name_value_pairs[-1] :
                    request_body += b'--' + boundary.encode('utf-8') + b'--\r\n'
                else:
                    request_body += b'--' + boundary.encode('utf-8') + b'\r\n'
                        
                    
                            
                            #if value:
                                #value = value.group(0).decode('utf-8')  # Convert matched bytes to a string
                                #print(f"Value for {name}: {value}")
                            #else:
                                #print(f"No value found for {name}")
            request.body = request_body
            #print(request.headers)
            #print(request.body)
            print ("imges passed for  :"  + self.data[session_number][0])
            del request.headers['Content-Length']
            request.headers['Content-Length'] = str(len(request.body))
    def __init__(self):
        
        
        with open('data.json','r') as f:
            self.data = self.json.loads(f.read())
        with open('cookies.json','r') as f2:
            self.Cookies = self.json.loads(f2.read())
        #if len(self.data)!=len(self.Cookies):
            #self.Cookies = [None] * len(self.data)
        #self.pygame.mixer.init()
        #self.alert_sound = self.pygame.mixer.Sound('mixkit-bell-notification-933.wav')
        self.current_user=-1
        self.key_date_list = None
        #self.Cookies = [None] * len(self.data)
        #print(self.data[0][0])
        self.captcha_solved =False
        self.loggedIn =False
        
        self.cookie = None
        self.session_opened = False
        self.available_date = False
        self.session_finish = False
        self.current_apointment_category = "Normal"
        self.available_apointment_category = ""
        self.opened_session = 1
        self.step =0
        self.token1=''
        self.token2=''
        self.decoded_token1=""
        self.captcha_list=[]
        self.captcha_list_old=[]
        self.session_chrome={}
        self.session_lock = threading.RLock()
        self.captcha_data={}
        self.visa_category={}
        self.all_date = {}
        self.frame_parser = {}
        for key in self.data:
            self.session_chrome[key]=None
            self.captcha_data[key]=None
            self.visa_category[key]=None
            self.all_date[key]=False
            self.frame_parser[key]=None
            
        
        
        self.dataToTime=""
        self.fifo = self.queue.Queue(maxsize=10)
        
        self.stop_auto = False
        
        self.dateGetType = 0
        self.login_all = False
        self.all_manual = False
        self.selfieFill = True
        self.paymentFill = True
        self.fast_mode = False
        self.token_value = ""
        self.auto_captcha = False 
    
    def make_session(self, user_id: str, auto: bool) -> None:
        if not hasattr(self, 'session_chrome'):
            self.session_chrome = {}

        self.user_id = user_id

        chrome_options = self.ChromeOptions()
        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--disable-background-networking')
        chrome_options.add_argument('--disable-background-timer-throttling')
        chrome_options.add_argument('--disable-client-side-phishing-detection')
        chrome_options.add_argument('--disable-default-apps')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--disable-translate')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

        with self.session_lock:
            if user_id not in self.session_chrome:
                self.session_chrome[user_id] = None

            if self.session_chrome[user_id] is None:
                self.session_chrome[user_id] = self.webdriver.Chrome(options=chrome_options)

            browser = self.session_chrome[user_id]

            browser.request_interceptor = lambda request: self.interceptor(request, user_id)
            browser.response_interceptor = lambda request, response: self.modify_response(request, response, user_id)

        self.open_bls(browser, user_id, auto)
    def go_auto(self):
        emails=[]
        for email in self.data:
            emails.append(email)
        current_session = 0
        self.stop_auto = False
        while  self.available_date  == False and self.stop_auto == False:
            
            if current_session==len(emails) or current_session == 3  :
                current_session=0
            
            try :
                self.make_session(emails[current_session],True)
                self.time.sleep(60)
            except:
                current_session -=1
            current_session +=1  
                
            
         
    def login(self,chrome,user):
        # common modules (http.client, re, subprocess, urllib.parse, threading)
        # are available at module level
        #self.chrome.get('https://algeria.blsspainglobal.com/DZA/account/login')
        max_retry_duration = 10
        start_time = self.time.time()
        while self.time.time() - start_time < max_retry_duration:
            try:
                # Your logic to open the session, for example:
                chrome.get('https://algeria.blsspainglobal.com/DZA/account/login')
                
                # Define the condition you want to wait for, e.g., a specific element to be present
                verify_selection = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.ID, "btnVerify")))
                
                # If the above line is reached, the session is open, and you can break out of the loop
                break
            except Exception as e:
                # Handle any exceptions, or you can simply wait and retry
                print("Session not opened yet. Retrying...")
                #raise MyCustomError("This is a custom error message")

        
        
        verify_selection.click()
        self.iframe = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,".k-widget.k-window")))

# Get the position (left and top) and size (width and height) of the element
        self.position = self.iframe.location
        self.size = self.iframe.size
            
        chrome.switch_to.frame(0)
        
        while len(self.captcha_list)!=10 or self.captcha_passed == False:
            if len(self.captcha_list)!=10 and len(self.captcha_list)!=0:
                repeat_captcha = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/div/div[2]/div[2]/div/form/div[2]/div[2]")))
                #/html/body/div/div[2]/div[2]/div/form/div[2]/div[2]/p
                repeat_captcha.click()
                
                print('in while')
            #self.which_part=1
            my_thread1 = threading.Thread(target=self.get_Captcha,args=(chrome,))
            my_thread1.start()
            my_thread1.join()
            print(self.captcha_list)
            if len(self.captcha_list)==10 :
                self.click_captcha(chrome,False)
                
        self.captcha_passed = False
        list_input = []
        hidden_xpath = "/html/body/main/main/div/div/div[2]/div[2]/form/div[7]/label"
        jjj=0               #/html/body/main/main/div/div/div[2]/form/div[35]/div/span
        for l in range(2, 22):
           
           hidden_modified = hidden_xpath.replace("[7]", f"[{l}]")
           if jjj==2 :
               break
            #print(modified_xpath)
           try:
               hidden_apointment_category = self.WebDriverWait(chrome, 0.01).until(self.EC.presence_of_element_located((self.By.XPATH,hidden_modified)))
               
               hidden_apointment_category.click()
               list_input.append(l)
               jjj+=1
               #print(l)
           except:
               pass
        
        hidden_modified = hidden_xpath.replace("[7]", f"[{list_input[0]}]")
        hidden_apointment_email=   self.WebDriverWait(chrome, 0.01).until(self.EC.presence_of_element_located((self.By.XPATH,hidden_modified)))
        hidden_apointment_email.click()
           
        #self.captcha_list=[]
        self.captcha_passed = False
        
        
        
        
        
        
        #/html/body/main/main/div/div/div[2]/div[2]/form/input[2]        
        captchaId_element = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/main/main/div/div/div[2]/div[2]/form/input[2]"))) 
        captchaId_value = captchaId_element.get_attribute("value")
        print("captchId = "+ captchaId_value)  
        
        captchaData_element= self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/main/main/div/div/div[2]/div[2]/form/input[4]"))) 
        captchaData_value = urllib.parse.quote(captchaData_element.get_attribute("value"), safe='/')  
        print("captchaData = "+ captchaData_value)  
        
        scriptData_element= self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/main/main/div/div/div[2]/div[2]/form/input[5]"))) 
        scriptData_value =  urllib.parse.quote(scriptData_element.get_attribute("value"), safe='/') 
        print("scriptData = "+ scriptData_value)  
        
        tokenVerification_element= self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/main/main/div/div/div[2]/div[2]/form/input[6]"))) 
        tokenVerification_value = urllib.parse.quote(tokenVerification_element.get_attribute("value"), safe='/') 
        print("tokenVerification = "+ tokenVerification_value) 
        print(list_input)
        list_input[0]-=1
        list_input[1]-=11
        
        print(list_input)  
        if self.login_all:
            for i in self.data:   		   
        	    self.update_cookies_for_user(i,chrome,list_input,
        	captchaId_value,captchaData_value,scriptData_value,tokenVerification_value)
        else:
            self.update_cookies_for_user(user,chrome,list_input,
        	captchaId_value,captchaData_value,scriptData_value,tokenVerification_value)
        #try:
        with open('cookies.json', 'w') as f3:
            self.json.dump(self.Cookies, f3)
        #except Exception as e:
                #print(f"An error occurred: {str(e)}")
        cookies = chrome.get_cookies()

        
        

    def update_cookies_for_user(self,user_idd,chrome,list_input,captchaId_value,captchaData_value,scriptData_value,tokenVerification_value):
        # http.client, re, subprocess are available at module level
        cookies = chrome.get_cookies()
        list_cookies=""
        for cookie_value in cookies:
            list_cookies+= f"{cookie_value['name']}={cookie_value['value']}; "
# Print the cookies
        user_id=self.data[user_idd][0]
        password = 'A2z0@I0z0'
        for cookie in cookies:
            print(f"Name: {cookie['name']}")
            print(f"Value: {cookie['value']}")
            #print(f"Domain: {cookie['domain']}") 
        
        user_password_id_list=""  
        #_list=""  
        ll =list_input[1]
        for n in range(1,11):
            if n==list_input[0]:
                the_added1= "UserId"+str(list_input[0])+f"={user_id}&"
            else :
                the_added1 = "UserId"+str(n)+"=&"
            
            user_password_id_list+=the_added1
            
        
        #print(ll)    
        for n in range(1,11):
            
            if n==list_input[1]:
                print(list_input[1])
                the_added2= "Password"+str(list_input[1])+f"={password}&"
            else :
                the_added2 = "Password"+str(n)+"=&"
            
            user_password_id_list+=the_added2
        formatted_string = f"{user_password_id_list}ReturnUrl=&CaptchaId={captchaId_value}&CaptchaParam=&CaptchaData={captchaData_value}&ScriptData={scriptData_value}&__RequestVerificationToken={tokenVerification_value}&X-Requested-With=XMLHttpRequest"
        the_post_str = str(formatted_string)
        
        print(formatted_string)
        curl_command = f"curl -X POST -H 'Cookie: {list_cookies}' -H 'User-Agent: Google Chrome/112.0.5615.49 Linux' -H 'Origin: https://algeria.blsspainglobal.com'-H 'Referer: https://algeria.blsspainglobal.com/DZA/Account/LogIn?ReturnUrl=%2FDZA%2Fbls%2FVisaTypeVerification' -d '{formatted_string}' https://algeria.blsspainglobal.com/DZA/account/loGinPost -i"
        print(curl_command)
        # io and subprocess are available at module level
        
        result = subprocess.run(curl_command, shell=True, check=True, text=True, capture_output=True)
        
        set_cookie_headers = re.findall(r'set-cookie:(.*?)\n', result.stdout, re.I)
        print('set_cookie_headers')
        print(result.stdout)
        #print(set_cookie_headers)

        cookies = {}
        for header in set_cookie_headers:
            header = header.strip()
            cookie_attributes = header.split(';')

            # Split the first attribute into name and value
            name, value = cookie_attributes[0].strip().split('=', 1) if '=' in cookie_attributes[0] else (cookie_attributes[0].strip(), '')

            # Add the cookie to the dictionary
            cookies[name] = value
            
        for cookie_name, cookie_value in cookies.items():
            print(f"Cookie Name: {cookie_name}")
            print(f"Cookie Value: {cookie_value}")
            try:
                self.cookie = {
        "name'": cookie_name,
        "value": cookie_value,
        "domain": "algeria.blsspainglobal.com" 
    }  
            except:
                pass
        try:
            self.Cookies[user_idd]= {
        "name": cookie_name,
        "value": cookie_value,
        "domain": "algeria.blsspainglobal.com"}
            self.loggedIn = True
        except:
            pass
        
            
        
        
        
        

        
        
    def open_bls(self,chrome,user,auto):
        # threading is available at module level
        is_open = False
        while not is_open:
            print(" 1 from open bls of "+ str(self.current_user))
            print( self.Cookies[user])
            
                 
            

            if not auto :
                is_open =True
            print(" 2 from open bls of "+ str(user))
            #self.time.sleep(2)
            if self.all_manual  :
                chrome.get('https://algeria.blsspainglobal.com/DZA/account/login')
                return
            if self.fast_mode:
                chrome.get('https://algeria.blsspainglobal.com/assets/images/logo.png')
                chrome.add_cookie(self.Cookies[user])
                chrome.get('https://algeria.blsspainglobal.com/DZA/bls/vtv9850')
            
            else:
                print("the first url is ",chrome.current_url)
                if chrome.current_url == "data:,":
                    chrome.get('https://algeria.blsspainglobal.com/assets/images/logo.png')
                    chrome.add_cookie(self.Cookies[user])
                    chrome.get('https://algeria.blsspainglobal.com/DZA/bls/vtv9850')
                else:
                    chrome.get('https://algeria.blsspainglobal.com/DZA/bls/vtv9850')
                
                #if chrome.current_url != "https://algeria.blsspainglobal.com/DZA/bls/VisaTypeVerification":
                   # if self.Cookies[user] != None :
                        #try:
                            #chrome.delete_cookie('.AspNetCore.Antiforgery.cyS7zUT4rj8')
                            #chrome.delete_cookie('.AspNetCore.Cookies')
                            #chrome.add_cookie(self.Cookies[user])
                            #print("all cookies deleted") 
                       # except:
                            #chrome.add_cookie(self.Cookies[user])
                      
                        #chrome.get('https://algeria.blsspainglobal.com/DZA/bls/VisaTypeVerification')
                
                print(" 3 from open bls of "+ str(user))
                if chrome.current_url != "https://algeria.blsspainglobal.com/DZA/bls/vtv9850" and auto :
                    self.login(chrome,user)
                    chrome.add_cookie(self.Cookies[user])
                    self.time.sleep(2)
                    chrome.get('https://algeria.blsspainglobal.com/DZA/bls/vtv9850') 
            current_u =  self.current_user   
            
            
            if auto :        
                self.current_user  = current_u 
            
            #while self.fifo.qsize() < 3:
            #chrome.get('https://algeria.blsspainglobal.com/DZA/bls/VisaTypeVerification') 
                try : 
                    verify_selection = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.ID, "btnVerify")))
                    verify_selection.click()
                    self.iframe = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,".k-widget.k-window")))

                    self.position = self.iframe.location
                    self.size = self.iframe.size
                    chrome.switch_to.frame(0)
                except:
                    try:
                        self.time.sleep(2)
                        verify_selection = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.ID, "btnVerify")))
                        verify_selection.click()
                        self.iframe = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,".k-widget.k-window")))

                        self.position = self.iframe.location
                        self.size = self.iframe.size
                        chrome.switch_to.frame(0)
                    except:
                        raise MyCustomError("This is a custom error message in verify click button")
                
                is_open= True
                self.captcha_list=[]
                self.captcha_passed = False
               
                print('before thread')
                
                print('before while')
                while len(self.captcha_list)!=10 or self.captcha_passed == False :
                    my_thread1 = threading.Thread(target=self.get_Captcha,args=(chrome,))
                    my_thread1.start()
                    my_thread1.join()
                    print(self.captcha_list)
                    if len(self.captcha_list)!=10 and len(self.captcha_list)!=0:
                        repeat_captcha = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/div/div[2]/div[2]/div/form/div[2]/div[2]")))
                    
                        repeat_captcha.click()
                    if set(self.captcha_list_old)==set(self.captcha_list):
                        
                        raise MyCustomError("This is a custom error message in get captcha")
                    
                    print('in while')
                #self.which_part=1
                    
                    if len(self.captcha_list)==10 :
                        self.click_captcha(chrome,True)
                    if self.captcha_passed==True :
                        try:
                            self.remplir_formulair(chrome,user)
                        except:
                            raise MyCustomError("This is a custom error message in remplir formulaire")  
                            self.available_date  = False
                
    def get_Captcha(self,chrome):
        # urllib.parse, subprocess, BeautifulSoup and re are available at module level
        
        curl_command = ""  # Initialize curl_command
        #part=self.which_part
        
        
        
        
        
        
        screenshot = chrome.get_screenshot_as_png()
        screenshot = self.Image.open(self.io.BytesIO(screenshot))
        screenshot1 = screenshot.crop((self.position['x'], self.position['y'], self.position['x'] + (self.size['width']/3), self.position['y'] + self.size['height']))
            #screenshot1.save("part1.png")
            
        screenshot2 = screenshot.crop((self.position['x']+ (self.size['width']/3), self.position['y'], self.position['x'] + 2*(self.size['width'])/3, self.position['y'] + self.size['height']))
            #screenshot2.save("part2.png")
            
        screenshot3 = screenshot.crop((self.position['x'] + 2*(self.size['width'])/3, self.position['y'], self.position['x'] + self.size['width'], self.position['y'] + self.size['height']))
            #screenshot3.save("part3.png")
            
        combined_image = self.Image.new("RGB", (self.size['width'], 3*self.size['height']))
        combined_image.paste(screenshot1, (0, 0))
        combined_image.paste(screenshot2, (0, self.size['height']))
        combined_image.paste(screenshot3, (0, 2*self.size['height']))
        combined_image.save("below.png")
        
        if self.decoded_token1 == '' :
            curl_command_init = f"curl 'https://www.imagetotext.cc/' -i"
            result = subprocess.run(curl_command_init, shell=True, check=True, text=True, capture_output=True)
            
            set_cookie_headers = re.findall(r'set-cookie:(.*?)\n', result.stdout, re.I)
            header = set_cookie_headers[0].strip()
            cookie_attributes = set_cookie_headers[0].split(';')
            name, self.token1 = cookie_attributes[0].strip().split('=', 1) if '=' in cookie_attributes[0] else (cookie_attributes[0].strip(), '')
            
            header = set_cookie_headers[1].strip()
            cookie_attributes = set_cookie_headers[1].split(';')

            name2, self.token2 = cookie_attributes[0].strip().split('=', 1) if '=' in cookie_attributes[0] else (cookie_attributes[0].strip(), '')
            
            self.decoded_token1 = urllib.parse.unquote(self.token1)
        
        
        
        
        
        
        #curl_command = "curl -X POST -F 'img=@below.png;type=image/png;filename=my_image.png' -F 'uploadurl=' -F 'submitType=1' -F 'url=' -F 'tool_submit=1' -F 'submit=1' https://www.editpad.org/tool/extract-text-from-image -i"
        curl_command= f"curl -X POST -H 'Cookie: XSRF-TOKEN={self.token1}; imagetotextcc_session={self.token2}' -F 'images=@below.png;type=image/png;filename=my_image.png' -H 'X-Xsrf-Token:{self.decoded_token1}'  https://www.imagetotext.cc/file-upload -i"
        # Execute the curl command using subprocess
        try:
            result = subprocess.run(curl_command, shell=True, check=True, text=True, capture_output=True)
            set_cookie_headers = re.findall(r'set-cookie:(.*?)\n', result.stdout, re.I)
            header = set_cookie_headers[0].strip()
            cookie_attributes = set_cookie_headers[0].split(';')
            name3, self.token1 = cookie_attributes[0].strip().split('=', 1) if '=' in cookie_attributes[0] else (cookie_attributes[0].strip(), '')
            #cookies[name3] = value3


            header = set_cookie_headers[1].strip()
            cookie_attributes = set_cookie_headers[1].split(';')

            name4, self.token2 = cookie_attributes[0].strip().split('=', 1) if '=' in cookie_attributes[0] else (cookie_attributes[0].strip(), '')
            self.decoded_token1 = urllib.parse.unquote(self.token1)
            
            parts = result.stdout.split('\n\n', 1)

# The second part contains the JSON response.
            json_response = parts[1]
            three_digit_numbers = re.findall(r'\d{3}', json_response)
            self.captch_list_old = self.captcha_list
            print("self.captch_list_old :")
            print(self.captch_list_old)
            
            self.captcha_list = three_digit_numbers
            
            print("self.captch_list")
            print(self.captcha_list)
            
        # Print the extracted three-digit numbers
                #print('part=  '+str(part))
            for number in three_digit_numbers:
                print(number)
            return three_digit_numbers
        except subprocess.CalledProcessError as e:
            print(f"Error executing curl: {e}")
            raise MyCustomError("This is a custom error message in get captcha")
            return []
            
            
    def click_captcha(self,chrome,loggedIn):
         import subprocess
         window_size = chrome.get_window_size()
         width = window_size["width"]
         
         height = window_size["height"]
         action = self.ActionChains(chrome)
         
         if self.captcha_list[6]==self.captcha_list[0] :
            action.move_by_offset((width/2)-100, (height/2)-130).click()
            action.move_by_offset((-width/2)+100, (-height/2)+130)
         if self.captcha_list[6]==self.captcha_list[1] :
            action.move_by_offset((width/2)-100, (height/2)).click()
            action.move_by_offset((-width/2)+100, (-height/2))
         if self.captcha_list[6]==self.captcha_list[2] :
            action.move_by_offset((width/2)-100, (height/2)+100).click()
            action.move_by_offset((-width/2)+100, (-height/2)-100)
         if self.captcha_list[6]==self.captcha_list[3] :
            action.move_by_offset((width/2), (height/2)-130).click()
            action.move_by_offset((-width/2), (-height/2)+130)
         if self.captcha_list[6]==self.captcha_list[4] :
            action.move_by_offset((width/2), (height/2)).click()
            action.move_by_offset((-width/2), (-height/2))
         if self.captcha_list[6]==self.captcha_list[5] :
            action.move_by_offset((width/2), (height/2)+100).click()
            action.move_by_offset((-width/2), (-height/2)-100)
         if self.captcha_list[6]==self.captcha_list[7] :
            action.move_by_offset((width/2)+100, (height/2)-130).click()
            action.move_by_offset((-width/2)-100, (-height/2)+130)
         if self.captcha_list[6]==self.captcha_list[8] :
            action.move_by_offset((width/2)+100, (height/2)).click()
            action.move_by_offset((-width/2)-100, (-height/2))
         if self.captcha_list[6]==self.captcha_list[9] :
            action.move_by_offset((width/2)+100, (height/2)+100).click()
            action.move_by_offset((-width/2)-100, (-height/2)-100)
         #action.move_by_offset(100,100).click()
         #action.move_by_offset(-200,-230).click()
         action.perform()
         submit_captcha = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.XPATH,"/html/body/div/div[2]/div[2]/div/form/div[2]/div[3]")))
         submit_captcha.click()
         self.time.sleep(1)
         
         try:
            alert = chrome.switch_to.alert
            alert.accept()
            self.alert_text = alert.text
            print(f"Alert Text: {alert_text}")
            self.captcha_passed = False
         
         except:
            self.time.sleep(2)
            try:
                 print('1.0 close captcha before')
                 #self.chrome.switch_to.frame(0)
                 print(chrome.current_url)
                 iframe = self.WebDriverWait(chrome, 1).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,".k-widget.k-window")))
                 self.captcha_passed = False
                 print('1 close captcha after')
            except:
                 try:
                     print('first line final try')
                     iframe = self.WebDriverWait(chrome, 1).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,".k-widget.k-window")))
                     self.captcha_passed = False
                     print('last line final try')
                 except:
                     chrome.switch_to.default_content()
                     self.captcha_passed = True
            
            
            
         if loggedIn == True and self.captcha_passed == True :
             try:
                #self.chrome.switch_to.default_content()
                
                submit = self.WebDriverWait(chrome, 1).until(self.EC.presence_of_element_located((self.By.XPATH,'/html/body/main/main/div/div/div[2]/form/div[2]/button[3]')))
                    
                submit.click()
                print('btn submit clicked')
                self.captcha_passed = True
                #self.remplir_formulair()
                 
             except  :
                try:
                    submit = self.WebDriverWait(chrome, 1).until(self.EC.presence_of_element_located((self.By.XPATH,'/html/body/main/main/div/div/div[2]/form/div[2]/button[2]')))
                    submit.click()
                    print('btn submit clicked')
                    self.captcha_passed = True
                    #self.remplir_formulair() 
                 
                except  :
                    try:
                        submit = self.WebDriverWait(chrome, 1).until(self.EC.presence_of_element_located((self.By.XPATH,'/html/body/main/main/div/div/div[2]/form/div[2]/button[1]')))
                        submit.click()
                        print('btn submit clicked')
                        self.captcha_passed = True
                        #self.remplir_formulair()
                    except  :
                        self.captcha_passed = False
                        chrome.switch_to.frame(0)
                        print('btn can not be clicked')
                        #raise MyCustomError("This is a custom error message in click captcha")
                       
         if loggedIn == False and self.captcha_passed == True:
             try:
                #self.chrome.switch_to.default_content()
                
                 submit = self.WebDriverWait(chrome, 1).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,'#btnSubmit')))
               
                 submit.click()
                 print('btn submit clicked')
                 self.captcha_passed = True
                #self.remplir_formulair()
                 
             except  :
                
                 self.captcha_passed = False
                 chrome.switch_to.frame(0)
                 print('btn can not be clicked') 
                 raise MyCustomError("This is a custom error message in remplir formulaire")
         #if self.captcha_passed==True and loggedIn == True  :
             #self.remplir_formulair(chrome,session_number)                
         
    def remplir_formulair(self,chrome,user):
        original_xpath = "/html/body/main/main/div/div/div[2]/form/div[11]/span"
        current_apointment_category=self.current_apointment_category
        #asyncio.run(self.send_telegram_message_with_screenshoot( current_apointment_category+"-----"+self.data[user][1]+" لا توجد مواعيد ",chrome))
        self.button_pos=[]


        j=0
        
       
        scrolled_down=False
        self.time.sleep(1)
        scroll_amount = 250
        chrome.execute_script(f"window.scrollBy(0, {scroll_amount});")
        scrolled_down=True
        for i in range(1, 100):
            
            modified_xpath = original_xpath.replace("[11]", f"[{i}]")
            if j==3 :
                break
                
            
            try:
                apointment_category = self.WebDriverWait(chrome, 0.09).until(self.EC.presence_of_element_located((self.By.XPATH,modified_xpath)))
                apointment_category.click()
                self.time.sleep(1)
                apointment_category.send_keys(self.Keys.ARROW_DOWN)
                self.time.sleep(1)
                apointment_category.send_keys(self.Keys.ENTER)
                if apointment_category.text == 'National Visa':
                    apointment_category.click()
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ARROW_DOWN)
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ENTER)
                
                if self.available_date ==True:
                    self.current_apointment_category = self.available_apointment_category
                   
                if self.current_apointment_category == 'Normal' and apointment_category.text == 'Normal':
                    #elf.available_apointment_category = self.current_apointment_category
                    self.current_apointment_category = 'Premium'
                
                elif self.current_apointment_category == 'Premium' and apointment_category.text == 'Normal':
                    apointment_category.click()
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ARROW_DOWN)
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ENTER)
                    self.available_apointment_category = self.current_apointment_category
                    self.current_apointment_category = 'Prime Time'
                
                elif self.current_apointment_category == 'Prime Time' and apointment_category.text == 'Normal':
                    apointment_category.click()
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ARROW_DOWN)
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ARROW_DOWN)
                    self.time.sleep(1)
                    apointment_category.send_keys(self.Keys.ENTER)
                    self.available_apointment_category = self.current_apointment_category
                    self.current_apointment_category = 'Normal'
                    
                j+=1
                self.button_pos+=i
                print(i)
                #self.time.sleep(2)
            except:
                #raise MyCustomError("This is a custom error message in remplir formulaire")
                pass
         
        hidden_xpath = "/html/body/main/main/div/div/div[2]/form/div[18]/div/span"
        jj=0               #/html/body/main/main/div/div/div[2]/form/div[35]/div/span
        for k in range(1, 100):
           
           hidden_modified = hidden_xpath.replace("[18]", f"[{k}]")
           if jj==1 :
               break
            #print(modified_xpath)
           try:
               hidden_apointment_category = self.WebDriverWait(chrome, 0.09).until(self.EC.presence_of_element_located((self.By.XPATH,hidden_modified)))
               if k not in self.button_pos:
                   hidden_apointment_category.click()
                   self.time.sleep(1)
                   hidden_apointment_category.send_keys(self.Keys.ARROW_DOWN)
                   self.time.sleep(1)
                   hidden_apointment_category.send_keys(self.Keys.ENTER)
                   if hidden_apointment_category.text == 'Algiers' and self.data[user][1] == 'oran' :
                        print(self.data[user][0])
                        hidden_apointment_category.click()
                        self.time.sleep(1)
                        hidden_apointment_category.send_keys(self.Keys.ARROW_DOWN)
                        self.time.sleep(1)
                        hidden_apointment_category.send_keys(self.Keys.ENTER)
                   
                   jj+=1
                   print(k)
           except:
               #raise MyCustomError("This is a custom error message in remplir formulaire")
               pass
        if j==3 and jj==1: 
            try:
                self.time.sleep(2)    
                print("in try first")  
                submit = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,'#btnSubmit')))
                #self.time.sleep(1)
                submit.click()
                print("in try last")
                self.session_finish=True
            except:
                try:
                    print("in except first")
                    scroll_amount = 250  # Adjust this value as needed
                    self.time.sleep(1)
                    chrome.execute_script(f"window.scrollBy(0, {scroll_amount});")
                    submit = self.WebDriverWait(chrome, 10).until(self.EC.presence_of_element_located((self.By.CSS_SELECTOR,'#btnSubmit')))
                    self.time.sleep(1)
                    submit.click()
                    self.session_finish=True
                    print("in except last")
                except:
                    raise MyCustomError("This is a custom error message in remplir formulaire")
        self.time.sleep(5)
        if self.session_finish==True:
            #print("from if before")
            print(f"the user {self.current_user}  , number of opened session {self.opened_session} , len of data {len(self.data)} ")
            
            print(self.Cookies)
            
            if self.available_date==False :
                
                try:
                    
                    self.send_telegram_message_with_screenshoot( current_apointment_category+"-----"+self.data[user][1]+" لا توجد مواعيد ",chrome)
                except:
                    pass
                
                print(chrome.current_url)
                
                
            else:
                #self.opened_session-=1
                
                for i in range(0,5):
                    try:
                        
                        self.send_telegram_message_with_screenshoot(current_apointment_category+"-----"+self.data[user][1]+" اسرع اسرع توجد مواعيد ",chrome)
                    except:
                        pass
              
                if self.current_apointment_category== 'Normal':
                    self.available_apointment_category = 'Prime Time'
                elif self.current_apointment_category== 'Premium':
                    self.available_apointment_category = 'Normal'
                elif self.current_apointment_category== 'Prime Time':
                    self.available_apointment_category = 'Premium'
                
                
            
            print("from if after")
            if self.available_date==False and self.user_id ==-1:
                self.session_finish=False
                
                chrome.delete_cookie('.AspNetCore.Antiforgery.cyS7zUT4rj8')
                chrome.delete_cookie('.AspNetCore.Cookies')
            else:
                self.session_finish = True
                #self.time.sleep(10)
         
        
    def send_telegram_message(self, the_message, background=True, wait=False):
        """Send a Telegram text message. By default runs in a background thread so it
        doesn't block Selenium/UI. Set `background=False` to run synchronously. If
        `wait=True` the caller will block until the send finishes.
        """
        import threading
        import asyncio
        
        bot_token = '6642510643:AAHaO5qNLpdEL1iD4Fhvc4gtqoSK-SAF3JA'
        chat_id = '-4001073822'
        
        async def _send():
            bot = Bot(token=bot_token)
            await bot.send_message(chat_id=chat_id, text=the_message)
        
        def _runner():
            try:
                asyncio.run(_send())
            except Exception as e:
                print(f"send_telegram_message error: {e}")
        
        if background:
            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            if wait:
                t.join()
        else:
            _runner()
    def send_telegram_message_with_screenshoot(self, the_message, chrome, background=True, wait=False):
        """Capture a screenshot from `chrome` and send it with a message. Runs in a
        background thread by default to avoid blocking Selenium. Use `background=False`
        to run synchronously and `wait=True` to wait for completion when background.
        """
        import threading
        import asyncio
        import io
        
        bot_token = '6642510643:AAHaO5qNLpdEL1iD4Fhvc4gtqoSK-SAF3JA'
        chat_id = '@bls_alerts_of'
        
        async def _send():
            bot = Bot(token=bot_token)
            try:
                # get_screenshot_as_png returns bytes
                screenshot_bytes = chrome.get_screenshot_as_png()
            except Exception as e:
                print(f"Failed to capture screenshot: {e}")
                screenshot_bytes = None
            
            try:
                await bot.send_message(chat_id=chat_id, text=the_message)
                if screenshot_bytes:
                    bio = io.BytesIO(screenshot_bytes)
                    bio.name = 'screenshot.png'
                    bio.seek(0)
                    await bot.send_photo(chat_id=chat_id, photo=InputFile(bio))
                
                if getattr(self, 'available_date', False):
                    await bot.send_message(chat_id='@bls_alerts_on', text=the_message)
                    if screenshot_bytes:
                        bio2 = io.BytesIO(screenshot_bytes)
                        bio2.name = 'screenshot.png'
                        bio2.seek(0)
                        await bot.send_photo(chat_id='@bls_alerts_on', photo=InputFile(bio2))
            except Exception as e:
                print(f"send_telegram_message_with_screenshoot error: {e}")
            
        def _runner():
            try:
                asyncio.run(_send())
            except Exception as e:
                print(f"telegram runner error: {e}")
        
        if background:
            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            if wait:
                t.join()
        else:
            _runner()
