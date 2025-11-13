import os
import time
import base64
import threading   # ✅ Added this earlier
from io import BytesIO

import pandas as pd
import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from urllib.parse import urljoin


class SeleniumWorker:
    def __init__(self, job_id, excel_path, download_dir, update_callback, static_values):
        self.job_id = job_id
        self.excel_path = excel_path
        self.download_dir = download_dir
        self.update_callback = update_callback
        self.static = static_values

        self._captcha_event = None
        self._captcha_value = None
        self._otp_event = None
        self._otp_value = None

        self.driver = None

    def _update(self, **kwargs):
        self.update_callback(kwargs)

    def provide_captcha(self, value):
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

    def _start_driver(self):
        chrome_options = Options()
        chrome_options.add_argument("--start-maximized")
        prefs = {"download.default_directory": os.path.abspath(self.download_dir),
                 "plugins.always_open_pdf_externally": True}
        chrome_options.add_experimental_option("prefs", prefs)
        self.driver = webdriver.Chrome(options=chrome_options)
        self.wait = WebDriverWait(self.driver, 20)

    def _get_captcha_base64(self, img_elem):
            """
            Capture the CAPTCHA image *exactly* as seen by the browser using canvas.
            Avoids reloading a new CAPTCHA from server.
            """
            try:
            # Use JavaScript to draw the <img> into a canvas and get dataURL
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

    def _handle_captcha(self):
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
        time.sleep(1)  # ensure image fully loaded
        b64 = self._get_captcha_base64(img_elem)
        self._captcha_event = threading.Event()
        self._captcha_value = None
        self._update(captcha_b64=b64, awaiting_captcha=True, message="Awaiting captcha input...")
        self._captcha_event.wait(timeout=300)
        self._update(awaiting_captcha=False, captcha_b64=None)
        if not self._captcha_value:
            raise Exception("No captcha entered in time")
        cap_input = self.driver.find_element(By.ID, "captcha")
        cap_input.clear()
        cap_input.send_keys(self._captcha_value)

    def _send_otp(self):
        try:
            btn = self.driver.find_element(By.ID, "verifyUDINSendOTP")
            btn.click()
        except Exception:
            pass

    def _handle_otp(self):
        # Wait for both mobile and email OTPs
        self._otp_event_mobile = threading.Event()
        self._otp_event_email = threading.Event()
        self._otp_value_mobile = None
        self._otp_value_email = None

        self._update(awaiting_otp=True,
                     message="Waiting for both Mobile and Email OTPs (via MacroDroid or manual entry)...")

        # Wait up to 3 min for both OTPs
        end_time = time.time() + 180
        while time.time() < end_time:
            if self._otp_value_mobile and self._otp_value_email:
                break
            time.sleep(1)

        self._update(awaiting_otp=False)

        if not (self._otp_value_mobile and self._otp_value_email):
            raise Exception("Did not receive both Mobile and Email OTPs in time")

        try:
            # Fill Mobile OTP
            otp_field_mobile = self.driver.find_element(By.ID, "otpMobile")
            otp_field_mobile.clear()
            otp_field_mobile.send_keys(self._otp_value_mobile)
            self.driver.find_element(By.ID, "VerifyOTPBtnMobile").click()
        except Exception as e:
            self._update(message=f"Mobile OTP error: {e}")

        try:
            # Fill Email OTP
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
