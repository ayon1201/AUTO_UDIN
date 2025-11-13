import os
import time
import base64
import threading
from io import BytesIO

import pandas as pd
import requests
from PIL import Image, ImageEnhance, ImageFilter
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from urllib.parse import urljoin

# You'll need to install these: pip install pytesseract easyocr
import pytesseract
# import easyocr  # Alternative OCR option


class CaptchaSolver:
    """Handles automatic CAPTCHA solving with multiple strategies"""
    
    def __init__(self):
        # Initialize EasyOCR reader (commented out - uncomment if using)
        # self.reader = easyocr.Reader(['en'], gpu=False)
        pass
    
    def preprocess_image(self, img):
        """Enhance image for better OCR accuracy"""
        # Convert to grayscale
        img = img.convert('L')
        
        # Increase contrast
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        # Apply threshold to get black and white
        img = img.point(lambda x: 0 if x < 128 else 255, '1')
        
        # Optional: Apply slight blur to reduce noise
        # img = img.filter(ImageFilter.MedianFilter(size=3))
        
        return img
    
    def solve_with_tesseract(self, img_b64):
        """Solve CAPTCHA using Tesseract OCR"""
        try:
            # Decode base64 image
            img_data = base64.b64decode(img_b64)
            img = Image.open(BytesIO(img_data))
            
            # Preprocess
            img = self.preprocess_image(img)
            
            # Configure Tesseract for CAPTCHA
            # --psm 7: Treat image as a single text line
            # --oem 3: Use default OCR Engine Mode
            custom_config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
            
            text = pytesseract.image_to_string(img, config=custom_config)
            
            # Clean the result
            text = ''.join(c for c in text if c.isalnum()).strip()
            
            return text
        except Exception as e:
            print(f"Tesseract error: {e}")
            return None
    
    def solve_with_easyocr(self, img_b64):
        """Solve CAPTCHA using EasyOCR (more accurate but slower)"""
        try:
            # Decode base64 image
            img_data = base64.b64decode(img_b64)
            img = Image.open(BytesIO(img_data))
            
            # Preprocess
            img = self.preprocess_image(img)
            
            # Convert PIL to numpy array for EasyOCR
            import numpy as np
            img_array = np.array(img)
            
            # Read text
            results = self.reader.readtext(img_array)
            
            if results:
                # Get the text with highest confidence
                text = results[0][1]
                text = ''.join(c for c in text if c.isalnum()).strip()
                return text
            
            return None
        except Exception as e:
            print(f"EasyOCR error: {e}")
            return None
    
    def solve_with_api(self, img_b64, api_key=None):
        """Solve CAPTCHA using 2Captcha or Anti-Captcha service"""
        # Example for 2Captcha
        if not api_key:
            return None
        
        try:
            # Submit CAPTCHA
            response = requests.post(
                'http://2captcha.com/in.php',
                data={
                    'key': api_key,
                    'method': 'base64',
                    'body': img_b64,
                    'json': 1
                }
            )
            
            result = response.json()
            if result['status'] != 1:
                return None
            
            captcha_id = result['request']
            
            # Poll for result
            for _ in range(20):
                time.sleep(5)
                check = requests.get(
                    f'http://2captcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1'
                )
                check_result = check.json()
                
                if check_result['status'] == 1:
                    return check_result['request']
            
            return None
        except Exception as e:
            print(f"API solver error: {e}")
            return None


class SeleniumWorker:
    def __init__(self, job_id, excel_path, download_dir, update_callback, static_values, 
                 auto_captcha=True, captcha_api_key=None):
        self.job_id = job_id
        self.excel_path = excel_path
        self.download_dir = download_dir
        self.update_callback = update_callback
        self.static = static_values
        
        # CAPTCHA settings
        self.auto_captcha = auto_captcha
        self.captcha_api_key = captcha_api_key
        self.captcha_solver = CaptchaSolver() if auto_captcha else None

        self._captcha_event = None
        self._captcha_value = None
        self._otp_event = None
        self._otp_value = None

        self.driver = None

    def _update(self, **kwargs):
        self.update_callback(kwargs)

    def provide_captcha(self, value):
        """Manual CAPTCHA override"""
        self._captcha_value = value
        if self._captcha_event:
            self._captcha_event.set()

    def provide_otp(self, value):
        self._otp_value = value
        if self._otp_event:
            self._otp_event.set()

    def is_waiting_for_otp(self):
        return self._otp_event is not None and not self._otp_event.is_set()

    def run(self):
        try:
            udins = self._read_udins()
            total = len(udins)
            self._update(status="running", total=total, progress=0, message=f"Starting job {self.job_id}")
            self._start_driver()

            for idx, udin in enumerate(udins, start=1):
                self._update(current=udin, progress=idx - 1, message=f"Processing {udin}")
                ok = self._process_one(udin)
                self._update(progress=idx, message=f"{udin} {'completed' if ok else 'failed'}")
                time.sleep(1)

            self._update(status="done", message="All UDINs processed.")
        except Exception as e:
            self._update(status="error", message=str(e))
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass

    def _read_udins(self):
        df = pd.read_excel(self.excel_path, engine="openpyxl")
        if "UDIN" not in df.columns:
            raise ValueError("Excel must have 'UDIN' column")
        return df["UDIN"].dropna().astype(str).tolist()

    # def _start_driver(self):
    #     chrome_options = Options()
    #     chrome_options.add_argument("--start-maximized")
    #     prefs = {"download.default_directory": os.path.abspath(self.download_dir),
    #              "plugins.always_open_pdf_externally": True}
    #     chrome_options.add_experimental_option("prefs", prefs)
    #     self.driver = webdriver.Chrome(options=chrome_options)
    #     self.wait = WebDriverWait(self.driver, 20)
    def _start_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--headless")  # Required for cloud
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        
        # For Render/Linux environments
        chrome_options.binary_location = "/usr/bin/chromium"
        
        prefs = {
            "download.default_directory": os.path.abspath(self.download_dir),
            "plugins.always_open_pdf_externally": True
        }
        chrome_options.add_experimental_option("prefs", prefs)
        
        self.driver = webdriver.Chrome(options=chrome_options)
        self.wait = WebDriverWait(self.driver, 20)

    def _get_captcha_base64(self, img_elem):
        """Capture the CAPTCHA image exactly as seen by the browser"""
        try:
            b64 = self.driver.execute_script("""
                var img = arguments[0];
                var canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth;
                canvas.height = img.naturalHeight;
                var ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0);
                return canvas.toDataURL('image/png').split(',')[1];
            """, img_elem)
            return b64
        except Exception as e:
            self._update(message=f"Canvas extraction failed: {e}")
            return None

    def _solve_captcha_automatically(self, img_b64, max_attempts=3):
        """Try to solve CAPTCHA automatically with multiple attempts"""
        if not self.auto_captcha or not img_b64:
            return None
        
        self._update(message="Attempting automatic CAPTCHA solving...")
        
        for attempt in range(max_attempts):
            # Try Tesseract first (fast)
            result = self.captcha_solver.solve_with_tesseract(img_b64)
            
            if result and len(result) >= 4:  # Assuming CAPTCHA is at least 4 chars
                self._update(message=f"CAPTCHA solved (attempt {attempt + 1}): {result}")
                return result
            
            # If Tesseract fails and we have API key, try API service
            if self.captcha_api_key:
                result = self.captcha_solver.solve_with_api(img_b64, self.captcha_api_key)
                if result:
                    self._update(message=f"CAPTCHA solved via API: {result}")
                    return result
        
        self._update(message="Auto-solve failed, falling back to manual entry")
        return None

    def _handle_captcha(self):
        """Handle CAPTCHA with automatic solving and manual fallback"""
        img_elem = None
        try:
            img_elem = self.wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "img[alt='captcha'], img.captcha, img#captchaImg")))
        except Exception:
            imgs = self.driver.find_elements(By.TAG_NAME, "img")
            for im in imgs:
                s = (im.get_attribute("src") or "").lower()
                if "cap" in s:
                    img_elem = im
                    break

        if not img_elem:
            self._update(message="No captcha found; continuing...")
            return
        
        time.sleep(1)  # Ensure image fully loaded
        b64 = self._get_captcha_base64(img_elem)
        
        if not b64:
            self._update(message="Could not extract CAPTCHA image")
            return
        
        # Try automatic solving first
        captcha_text = None
        if self.auto_captcha:
            captcha_text = self._solve_captcha_automatically(b64)
        
        # If auto-solve failed, fall back to manual entry
        if not captcha_text:
            self._captcha_event = threading.Event()
            self._captcha_value = None
            self._update(captcha_b64=b64, awaiting_captcha=True, 
                        message="Auto-solve failed. Please enter CAPTCHA manually...")
            self._captcha_event.wait(timeout=300)
            self._update(awaiting_captcha=False, captcha_b64=None)
            
            if not self._captcha_value:
                raise Exception("No captcha entered in time")
            captcha_text = self._captcha_value
        
        # Enter the CAPTCHA
        cap_input = self.driver.find_element(By.ID, "captcha")
        cap_input.clear()
        cap_input.send_keys(captcha_text)

    def _process_one(self, udin):
        site = "https://udin.icai.org/search-udin"
        try:
            self.driver.get(site)
            time.sleep(1)
            self._fill_static_fields()
            self._fill_udin(udin)
            self._handle_captcha()
            self._send_otp()
            self._handle_otp()
            pdf_path = self._wait_for_pdf(otud=udin, timeout=30)
            if pdf_path:
                self._update(last_pdf=os.path.basename(pdf_path), message=f"Downloaded PDF for {udin}")
            else:
                self._update(message=f"No PDF found for {udin}")
            return True
        except Exception as e:
            self._update(message=f"Error processing {udin}: {e}")
            return False

    def _fill_static_fields(self):
        try:
            auth_elem = self.wait.until(EC.presence_of_element_located((By.ID, "AuthorityType")))
            sel = Select(auth_elem)
            try:
                sel.select_by_visible_text(self.static.get("authority_type", "Others"))
            except Exception:
                for o in auth_elem.find_elements(By.TAG_NAME, "option"):
                    if o.get_attribute("value"):
                        sel.select_by_value(o.get_attribute("value"))
                        break
        except Exception:
            pass
        for field, fid in [
            ("authority_name", "AuthorityName"),
            ("mobile", "Mobile"),
            ("email", "Email")
        ]:
            try:
                e = self.driver.find_element(By.ID, fid)
                e.clear(); e.send_keys(self.static.get(field, ""))
            except Exception:
                pass

    def _fill_udin(self, udin):
        udin_input = self.wait.until(EC.presence_of_element_located((By.ID, "Udin")))
        udin_input.clear(); udin_input.send_keys(udin)
        try:
            chk = self.driver.find_element(By.ID, "chkDisclaimer")
            if not chk.is_selected():
                chk.click()
        except Exception:
            pass

    def _send_otp(self):
        try:
            btn = self.driver.find_element(By.ID, "verifyUDINSendOTP")
            btn.click()
        except Exception:
            pass

    def _handle_otp(self):
        self._otp_event_mobile = threading.Event()
        self._otp_event_email = threading.Event()
        self._otp_value_mobile = None
        self._otp_value_email = None

        self._update(awaiting_otp=True,
                     message="Waiting for both Mobile and Email OTPs...")

        end_time = time.time() + 180
        while time.time() < end_time:
            if self._otp_value_mobile and self._otp_value_email:
                break
            time.sleep(1)

        self._update(awaiting_otp=False)

        if not (self._otp_value_mobile and self._otp_value_email):
            raise Exception("Did not receive both Mobile and Email OTPs in time")

        try:
            otp_field_mobile = self.driver.find_element(By.ID, "otpMobile")
            otp_field_mobile.clear()
            otp_field_mobile.send_keys(self._otp_value_mobile)
            self.driver.find_element(By.ID, "VerifyOTPBtnMobile").click()
        except Exception as e:
            self._update(message=f"Mobile OTP error: {e}")

        try:
            otp_field_email = self.driver.find_element(By.ID, "otpEmail")
            otp_field_email.clear()
            otp_field_email.send_keys(self._otp_value_email)
            self.driver.find_element(By.ID, "VerifyOTPBtnEmail").click()
        except Exception as e:
            self._update(message=f"Email OTP error: {e}")

        time.sleep(2)

    def provide_mobile_otp(self, value):
        self._otp_value_mobile = value
        if hasattr(self, "_otp_event_mobile") and self._otp_event_mobile:
            self._otp_event_mobile.set()

    def provide_email_otp(self, value):
        self._otp_value_email = value
        if hasattr(self, "_otp_event_email") and self._otp_event_email:
            self._otp_event_email.set()

    def _wait_for_pdf(self, otud, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            files = [f for f in os.listdir(self.download_dir) if f.lower().endswith(".pdf")]
            if files:
                files.sort(key=lambda f: os.path.getmtime(os.path.join(self.download_dir, f)), reverse=True)
                src = os.path.join(self.download_dir, files[0])
                dst = os.path.join(self.download_dir, f"{otud}.pdf")
                try:
                    os.replace(src, dst)
                except Exception:
                    dst = src
                return dst
            time.sleep(1)
        return None