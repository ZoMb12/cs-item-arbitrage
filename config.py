import os
from datetime import date

DEFAULT_TARGET_DATE = date.today()
DEFAULT_STABLE_DAYS = 24
DEFAULT_VOLATILITY_THRESHOLD = 0.05
DEFAULT_MIN_VOLUME = 100
DEFAULT_MIN_PRICE = 20.0
DEFAULT_TARGET_COUNT = 200

CATEGORY_OPTIONS = {
    "全部/不限": "",
    "匕首": "knife",
    "手套": "hands",
    "步枪": "rifle",
    "手枪": "pistol",
    "微型冲锋枪": "smg",
    "霰弹枪": "shotgun",
    "机枪": "machinegun",
    "印花": "sticker",
    "挂件": "csgo_tool_keychain_group",
    "探员": "type_customplayer",
    "其他": "other",
}

# 代理配置（Clash/V2Ray 等本地代理，用于访问被墙的 Steam）
PROXY_SERVER = "http://127.0.0.1:7890"
PROXY_BYPASS = "buff.163.com,.163.com,.qq.com,.aliyuncs.com,.cn"
PROXY_CONFIG = {"server": PROXY_SERVER, "bypass": PROXY_BYPASS}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(_BASE_DIR, "storage")
COOKIE_PATH = os.path.join(STORAGE_DIR, "buff_cookies.json")
STEAM_COOKIE_PATH = os.path.join(STORAGE_DIR, "steam_cookies.json")
