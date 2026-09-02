from utils import AES_Encrypt, generate_captcha_key, verify_param
import json
import requests
import re
import time
import logging
import datetime
from urllib3.exceptions import InsecureRequestWarning


class reserve:
    def __init__(
        self,
        sleep_time=0.2,
        max_attempt=50,
        enable_slider=False,
        reserve_next_day=False,
    ):
        self.login_page = (
            "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
        )
        self.url = (
            "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        )
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.seat_url = "https://office.chaoxing.com/data/apps/seat/getusedtimes"
        self.login_url = "https://passport2.chaoxing.com/fanyalogin"
        self.submit_msg = []
        self.requests = requests.session()
        self.headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        self.login_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "accept-encoding": "gzip, deflate, br, zstd",
            "cache-control": "no-cache",
            "Connection": "keep-alive",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 10_3_1 like Mac OS X) AppleWebKit/603.1.3 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1 wechatdevtools/1.05.2109131 MicroMessenger/8.0.5 Language/zh_CN webview/16364215743155638",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        self.sleep_time = sleep_time
        self.max_attempt = max_attempt
        self.enable_slider = enable_slider
        self.reserve_next_day = reserve_next_day
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    # login and page token
    def _get_page_token(self, url, require_value=False):
        response = self.requests.get(url=url, verify=False)
        html = response.content.decode("utf-8")
        # Attribute order on the page is not guaranteed; accept both.
        matches = (
            re.findall(r'<input[^>]*id="submit_enc"[^>]*value="([^"]*)"', html)
            or re.findall(r'<input[^>]*value="([^"]*)"[^>]*id="submit_enc"', html)
        )
        value_matches = None
        if require_value:
            if not matches:
                logging.error(f"Failed to get token from {url}")
                return "", ""
            # Anchor on the algorithm input itself; attribute order varies.
            algo_matches = (
                re.findall(r'<input[^>]*id="algorithm"[^>]*value="([^"]*)"', html)
                or re.findall(r'<input[^>]*value="([^"]*)"[^>]*id="algorithm"', html)
            )
            if algo_matches:
                value_matches = algo_matches
            else:
                # Some schools' page exposes exactly one submit_enc field that
                # doubles as the signing salt; anything else is too ambiguous
                # to guess (the first value= on a page is usually unrelated).
                all_values = re.findall(r'value="(.*?)"', html)
                if all_values and all(value == matches[0] for value in all_values):
                    value_matches = matches
            if not value_matches:
                logging.error(f"Failed to get submit value from {url}")
                return matches[0], ""
        return matches[0] if matches else "", value_matches[0] if value_matches else ""

    def get_login_status(self):
        # Per-request headers only: assigning to self.requests.headers would
        # leak the mobile login headers (and their Host) into every later
        # office.chaoxing.com request on the shared session.
        self.requests.get(url=self.login_page, headers=self.login_headers, verify=False)

    def login(self, username, password):
        encrypted_username = AES_Encrypt(username)
        password = AES_Encrypt(password)
        parm = {
            "fid": -1,
            "uname": encrypted_username,
            "password": password,
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": "true",
        }
        jsons = self.requests.post(url=self.login_url, params=parm, headers=self.login_headers, verify=False)
        try:
            obj = jsons.json()
        except ValueError:
            logging.error(f"User {username} login failed: response is not JSON")
            return (False, "登录响应不是 JSON")
        if obj.get("status"):
            logging.info(f"User {username} login successfully")
            return (True, "")
        else:
            logging.info(
                f"User {username} login failed. Please check you password and username! "
            )
            return (False, obj.get("msg2") or obj.get("msg") or "登录失败")

    # extra: get roomid
    def roomid(self, encode):
        url = f"https://office.chaoxing.com/data/apps/seat/room/list?cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&deptIdEnc={encode}"
        json_data = self.requests.get(url=url).content.decode("utf-8")
        ori_data = json.loads(json_data)
        for i in ori_data["data"]["seatRoomList"]:
            info = f'{i["firstLevelName"]}-{i["secondLevelName"]}-{i["thirdLevelName"]} id为：{i["id"]}'
            print(info)

    # solve captcha

    def resolve_captcha(self):
        logging.info(f"Start to resolve captcha token")
        captcha_token, bg, tp = self.get_slide_captcha_data()
        logging.info(f"Successfully get prepared captcha_token {captcha_token}")
        logging.info(f"Captcha Image URL-small {tp}, URL-big {bg}")
        x = self.x_distance(bg, tp)
        logging.info(f"Successfully calculate the captcha distance {x}")

        params = {
            "callback": "jQuery33109180509737430778_1716381333117",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "token": captcha_token,
            "textClickArr": json.dumps([{"x": x}]),
            "coordinate": json.dumps([]),
            "runEnv": "10",
            "version": "1.1.18",
            "_": int(time.time() * 1000),
        }
        response = self.requests.get(
            f"https://captcha.chaoxing.com/captcha/check/verification/result",
            params=params,
            headers=self.headers,
        )
        text = response.text.replace(
            "jQuery33109180509737430778_1716381333117(", ""
        ).replace(")", "")
        data = json.loads(text)
        logging.info(f"Successfully resolve the captcha token {data}")
        try:
            validate_val = json.loads(data["extraData"])["validate"]
            return validate_val
        except (KeyError, TypeError, ValueError):
            # KeyError: no validate field; TypeError: extraData is None;
            # ValueError: non-JSON payload (risk-control page etc.).
            logging.info("Can't load validate value. Maybe server return mistake.")
            return ""

    def get_slide_captcha_data(self):
        url = "https://captcha.chaoxing.com/captcha/get/verification/image"
        timestamp = int(time.time() * 1000)
        capture_key, token = generate_captcha_key(timestamp)
        referer = f"https://office.chaoxing.com/front/third/apps/seat/code?id=3993&seatNum=0199"
        params = {
            "callback": f"jQuery33107685004390294206_1716461324846",
            "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide",
            "version": "1.1.18",
            "captchaKey": capture_key,
            "token": token,
            "referer": referer,
            "_": timestamp,
            "d": "a",
            "b": "a",
        }
        response = self.requests.get(url=url, params=params, headers=self.headers)
        content = response.text

        data = content.replace(
            "jQuery33107685004390294206_1716461324846(", ")"
        ).replace(")", "")
        data = json.loads(data)
        captcha_token = data["token"]
        bg = data["imageVerificationVo"]["shadeImage"]
        tp = data["imageVerificationVo"]["cutoutImage"]
        return captcha_token, bg, tp

    def x_distance(self, bg, tp):
        import numpy as np
        import cv2

        def cut_slide(slide):
            slider_array = np.frombuffer(slide, np.uint8)
            slider_image = cv2.imdecode(slider_array, cv2.IMREAD_UNCHANGED)
            slider_part = slider_image[:, :, :3]
            # Alpha channel is optional: PNG cutouts carry it, JPEG does not.
            if slider_image.ndim == 3 and slider_image.shape[2] == 4:
                mask = slider_image[:, :, 3]
            else:
                mask = np.full(slider_image.shape[:2], 255, np.uint8)
            mask[mask != 0] = 255
            x, y, w, h = cv2.boundingRect(mask)
            cropped_image = slider_part[y : y + h, x : x + w]
            return cropped_image

        c_captcha_headers = {
            "Referer": "https://office.chaoxing.com/",
            "Host": "captcha-b.chaoxing.com",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
        bgc, tpc = self.requests.get(bg, headers=c_captcha_headers), self.requests.get(
            tp, headers=c_captcha_headers
        )
        bg, tp = bgc.content, tpc.content
        bg_img = cv2.imdecode(np.frombuffer(bg, np.uint8), cv2.IMREAD_COLOR)
        tp_img = cut_slide(tp)
        bg_edge = cv2.Canny(bg_img, 100, 200)
        tp_edge = cv2.Canny(tp_img, 100, 200)
        bg_pic = cv2.cvtColor(bg_edge, cv2.COLOR_GRAY2RGB)
        tp_pic = cv2.cvtColor(tp_edge, cv2.COLOR_GRAY2RGB)
        res = cv2.matchTemplate(bg_pic, tp_pic, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(res)
        tl = max_loc
        # NOTE: tl[0] is in source-image pixels. If the front end renders the
        # captcha scaled, the submitted x must be multiplied by that ratio —
        # verify once against a real page before relying on slider solving.
        return tl[0]

    def submit(self, times, roomid, seatid, action):
        if not seatid:
            logging.error("submit called with an empty seat list")
            return False
        suc = False
        for seat in seatid:
            # Per-seat attempt budget: the old shared self.max_attempt let the
            # first seat starve every later candidate to zero tries.
            attempts = self.max_attempt
            while not suc and attempts > 0:
                token, value = self._get_page_token(
                    self.url.format(roomid, seat), require_value=True
                )
                logging.info(f"Get token: {token}")
                captcha = self.resolve_captcha() if self.enable_slider else ""
                logging.info(f"Captcha token {captcha}")
                suc = self.get_submit(
                    self.submit_url,
                    times=times,
                    token=token,
                    roomid=roomid,
                    seatid=seat,
                    captcha=captcha,
                    action=action,
                    value=value,
                )
                if suc:
                    return suc
                time.sleep(self.sleep_time)
                attempts -= 1
        return suc

    def get_submit(
        self, url, times, token, roomid, seatid, captcha="", action=False, value=""
    ):
        delta_day = 1 if self.reserve_next_day else 0
        day = datetime.date.today() + datetime.timedelta(
            days=0 + delta_day
        )  # 预约今天，修改days=1表示预约明天
        if action:
            day = datetime.date.today() + datetime.timedelta(
                days=1 + delta_day
            )  # 由于action时区问题导致其早+8区一天
        parm = {
            "roomId": roomid,
            "startTime": times[0],
            "endTime": times[1],
            "day": str(day),
            "seatNum": seatid,
            "captcha": captcha,
            "token": token,
            "type": "1",
            "verifyData": "1",
        }
        logging.info(f"submit parameter {parm} ")
        parm["enc"] = verify_param(parm, value)
        html = self.requests.post(url=url, params=parm, verify=True).content.decode(
            "utf-8"
        )
        try:
            payload = json.loads(html)
            ok = bool(payload.get("success"))
            message = payload.get("msg") or payload.get("message") or ""
        except ValueError:
            # A non-JSON body (login-expiry redirect, risk-control page) must
            # fail this attempt instead of crashing the whole run.
            ok, message = False, "响应不是 JSON（可能登录失效或被风控）"
        self.submit_msg.append(times[0] + "~" + times[1] + ":  " + str(message or html[:200]))
        logging.info(f"submit result: ok={ok} {message or html[:200]}")
        return ok
