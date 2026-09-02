import json
import time
import argparse
import os
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# NOTE: everything below until the ``__main__`` block is the legacy
# command-line reserve script (python main.py -m reserve/debug/room), kept for
# backwards compatibility. The local web service only uses ``-m serve`` and
# deliberately does not import this legacy stack (numpy/opencv heavy) — see
# _legacy_runtime().


def _legacy_runtime():
    """Import the legacy reserve stack lazily, only for CLI modes."""
    from utils import reserve, get_user_credentials

    return reserve, get_user_credentials

get_current_time = lambda action: (
    time.strftime("%H:%M:%S", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%H:%M:%S", time.localtime(time.time()))
)
get_current_dayofweek = lambda action: (
    time.strftime("%A", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%A", time.localtime(time.time()))
)


SLEEPTIME = 0.2  # 每次抢座的间隔
ENDTIME = "07:01:00"  # 根据学校的预约座位时间+1min即可

ENABLE_SLIDER = False  # 是否有滑块验证（需要额外安装 numpy 和 opencv-python）
MAX_ATTEMPT = 5  # 最大尝试次数
RESERVE_NEXT_DAY = False  # 预约明天而不是今天的


def login_and_reserve(users, usernames, passwords, action, success_list=None):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    if action and len(usernames.split(",")) != len(users):
        raise Exception("user number should match the number of config")
    if success_list is None:
        success_list = [False] * len(users)
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        if not success_list[index]:
            logging.info(
                f"----------- {username} -- {times} -- {seatid} try -----------"
            )
            reserve, _ = _legacy_runtime()
            s = reserve(
                sleep_time=SLEEPTIME,
                max_attempt=MAX_ATTEMPT,
                enable_slider=ENABLE_SLIDER,
                reserve_next_day=RESERVE_NEXT_DAY,
            )
            s.get_login_status()
            s.login(username, password)
            s.requests.headers.update({"Host": "office.chaoxing.com"})
            suc = s.submit(times, roomid, seatid, action)
            success_list[index] = suc
    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"start time {current_time}, action {'on' if action else 'off'}")
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        _, get_user_credentials = _legacy_runtime()
        usernames, passwords = get_user_credentials(action)
    success_list = None
    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )
    while current_time < ENDTIME:
        attempt_times += 1
        # try:
        success_list = login_and_reserve(
            users, usernames, passwords, action, success_list
        )
        # except Exception as e:
        #     print(f"An error occurred: {e}")
        print(
            f"attempt time {attempt_times}, time now {current_time}, success list {success_list}"
        )
        current_time = get_current_time(action)
        if sum(success_list) == today_reservation_num:
            print(f"reserved successfully!")
            return


def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
    reserve, get_user_credentials = _legacy_runtime()
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        suc = s.submit(times, roomid, seatid, action)
        if suc:
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    reserve, _ = _legacy_runtime()
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room", "serve"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    if args.method == "serve":
        import uvicorn
        from app.single_instance import acquire_service_mutex, release_service_mutex

        if not acquire_service_mutex():
            message = "本地预约服务已经在运行（互斥量被占用，本进程退出）：请打开 http://127.0.0.1:8787/"
            print(message, flush=True)
            logging.warning(message)
            raise SystemExit(0)
        try:
            logging.info("service instance mutex acquired; starting uvicorn on 127.0.0.1:8787")
            # The watchdog probes /health regularly. Suppress only Uvicorn's
            # per-request access lines so that those probes do not grow the
            # service log indefinitely; application warnings/errors remain.
            uvicorn.run("app.web:app", host="127.0.0.1", port=8787, reload=False, access_log=False)
        finally:
            release_service_mutex()
    else:
        with open(args.user, "r", encoding="utf-8") as data:
            usersdata = json.load(data)["reserve"]
        func_dict[args.method](usersdata, args.action)
