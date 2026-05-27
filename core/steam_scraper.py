"""Step 3: Steam 市场价格 & 销量数据提取。

从 BUFF 商品详情页点击"查看Steam市场"跳转到 Steam，
提取 SSR 数据中的 buckets，匹配变体后调用 pricehistory API 获取历史价格。
"""
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime
from typing import List, Optional, Set

# Windows GBK 编码兼容：Steam 数据含 ™ 等特殊字符
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 设置代理环境变量（确保 Playwright 内置 Chromium 能通过代理访问 Steam）
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
os.environ.setdefault("NO_PROXY", "buff.163.com,.163.com,.qq.com,.aliyuncs.com,.cn")

from playwright.sync_api import sync_playwright

# 最后一次错误的详情，供 app.py 错误日志使用
_last_error: str = ""
_last_error_context: dict = {}  # 结构化错误现场信息（URL、标题、截图路径等）
_batch_errors: dict = {}  # 批处理时按 item_id 记录的失败 {item_id: {"error": str, "context": dict}}

import config
from core.buff_scraper import (
    _load_cookies_from,
    _save_cookies_to,
    _open_authenticated_page,
)
from data.models import PriceRecord
from utils.helpers import sleep_random


# ═══════════════════════════════════════════════════════════════════
# 侧边栏 Steam 按钮
# ═══════════════════════════════════════════════════════════════════

def open_steam_market():
    _open_authenticated_page(
        "https://steamcommunity.com/market/", config.STEAM_COOKIE_PATH, "Steam 市场"
    )


def is_steam_logged_in() -> bool:
    cookies = _load_cookies_from(config.STEAM_COOKIE_PATH)
    if not cookies:
        return False
    return any(c.get("name") == "steamLoginSecure" for c in cookies)


def ensure_steam_login():
    cookies = _load_cookies_from(config.STEAM_COOKIE_PATH)
    if cookies:
        if any(c.get("name") == "steamLoginSecure" for c in cookies):
            print("已检测到有效 Steam 登录态（steamLoginSecure），跳过登录。")
            return

    print("正在打开 Chromium 浏览器，请在窗口中完成 Steam 登录...")
    print("登录完成后，关闭浏览器窗口即可，系统将自动保存登录态。")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        login_url = (
            "https://store.steampowered.com/login/"
            "?redir=https%3A%2F%2Fsteamcommunity.com%2F"
        )
        try:
            page.goto(login_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
        except Exception as e:
            print(f"导航到 Steam 登录页失败: {e}")

        print("请在弹出的浏览器窗口中完成 Steam 登录。")
        print("登录完成后**关闭浏览器窗口**即可，系统会自动保存登录态。")

        import time
        start_time = time.time()
        saved = False
        last_names = set()

        while time.time() - start_time < 600:
            try:
                cur = context.cookies()
                cur_names = {c.get("name") for c in cur if c.get("name")}
                if cur_names != last_names:
                    print(f"[Steam 检测] 当前 cookie: {cur_names}")
                    last_names = cur_names
                if any(c.get("name") == "steamLoginSecure" for c in cur):
                    print("检测到 steamLoginSecure，登录成功！保存中...")
                    _save_cookies_to(config.STEAM_COOKIE_PATH, cur)
                    saved = True
                    print(f"Steam 登录态保存成功（{len(cur)} 个 cookie），可以关闭浏览器了。")
                    break
            except Exception as e:
                print(f"获取 cookie 异常: {e}")
            try:
                if not browser.is_connected():
                    print("浏览器已断开。")
                    break
            except Exception:
                break
            time.sleep(0.5)

        if not saved:
            try:
                cur = context.cookies()
                _save_cookies_to(config.STEAM_COOKIE_PATH, cur)
                if any(c.get("name") == "steamLoginSecure" for c in cur):
                    print(f"Steam 登录态保存成功，共 {len(cur)} 个 cookie。")
                else:
                    names = [c.get("name") for c in cur]
                    print(f"未检测到 steamLoginSecure，已保存 {len(cur)} 个 cookie（名称: {names}）。")
            except Exception as e:
                print(f"浏览器断开后无法获取 cookie: {e}")
        try:
            browser.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# 变体匹配
# ═══════════════════════════════════════════════════════════════════

_WEAR_CN_TO_EN = {
    "崭新出厂": "Factory New",
    "略有磨损": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn",
    "战痕累累": "Battle-Scarred",
}
_WEAR_EN_KEYWORDS = [
    "Factory New", "Minimal Wear", "Field-Tested",
    "Well-Worn", "Battle-Scarred",
]


def _parse_buff_item_properties(name: str) -> dict:
    """从 BUFF 饰品名称中提取属性（StatTrak、磨损）。"""
    stattrak = "StatTrak" in name or "stattrak" in name.lower()
    wear = None
    for cn, en in _WEAR_CN_TO_EN.items():
        if cn in name:
            wear = en
            break
    if not wear:
        for wk in _WEAR_EN_KEYWORDS:
            if wk in name:
                wear = wk
                break
    return {"stattrak": stattrak, "wear": wear}


def _match_bucket(buckets: list, props: dict) -> dict | None:
    """从 buckets 中找到与 BUFF 饰品属性匹配的 bucket。

    匹配规则：磨损等级相同 且 StatTrak 状态相同。
    bucket_name 示例: "Galil AR | Eye of Horus (Field-Tested)"
                      "StatTrak™ Galil AR | Eye of Horus (Field-Tested)"
    """
    target_wear = props.get("wear")
    target_st = props.get("stattrak", False)

    for bk in buckets:
        name = bk.get("localized_name", "")
        is_st = "StatTrak" in name
        if is_st != target_st:
            continue
        if target_wear and target_wear not in name:
            continue
        return bk
    return None


def _click_quality_button(page, is_stattrak: bool):
    """点击 Steam 页面品质过滤按钮（普通 / StatTrak™）。"""
    target = "StatTrak™" if is_stattrak else "普通"
    try:
        btn = page.query_selector(f'button:has-text("{target}")')
        if not btn:
            return
        accent = btn.get_attribute("data-accent-color")
        if accent == "accent":
            return
        btn.click()
        sleep_random(1.0, 2.0)
    except Exception:
        pass


def _click_wear_button(page, wear: str | None):
    """点击 Steam 页面的磨损等级标签（如 '略有磨损' / 'Minimal Wear'）。"""
    if not wear:
        return
    # 先找英文，再找中文
    candidates = [wear]
    for cn, en in _WEAR_CN_TO_EN.items():
        if en == wear:
            candidates.append(cn)
            break
    for name in candidates:
        try:
            el = page.query_selector(f'[data-selected]:has-text("{name}")')
            if el:
                selected = el.get_attribute("data-selected")
                if selected == "false":
                    print(f"[Steam] 点击磨损标签: {name}")
                    el.click()
                    sleep_random(1.5, 2.5)
                return
        except Exception:
            continue


# ═══════════════════════════════════════════════════════════════════
# 按基础皮肤名分组（Step 3 批处理优化）
# ═══════════════════════════════════════════════════════════════════

def _get_base_skin_name(name: str) -> str:
    """从完整饰品名中提取基础皮肤名（去磨损后缀和 StatTrak 前缀）。"""
    base = re.sub(r'^StatTrak™\s*', '', name).strip()
    wear_keywords = '|'.join([
        '崭新出厂', '略有磨损', '久经沙场', '破损不堪', '战痕累累',
        'Factory New', 'Minimal Wear', 'Field-Tested', 'Well-Worn',
        'Battle-Scarred',
    ])
    base = re.sub(rf'\s*\((?:{wear_keywords})\)\s*$', '', base).strip()
    return base


def group_by_skin_name(items: list) -> dict:
    """将饰品列表按基础皮肤名分组，同组饰品共用一个 Steam listing 页面。

    返回: {base_skin_name: [item1, item2, ...]}
    """
    groups = {}
    for item in items:
        base = _get_base_skin_name(item.name)
        groups.setdefault(base, []).append(item)
    return groups


# ═══════════════════════════════════════════════════════════════════
# Step 3 核心
# ═══════════════════════════════════════════════════════════════════

def get_last_steam_error(item_id=None) -> str:
    """返回最后一次 Steam 数据获取的失败原因，供调用方记录错误日志。
    传入 item_id 时优先返回批处理中该饰品的独立错误。"""
    if item_id and item_id in _batch_errors:
        return _batch_errors[item_id]["error"]
    return _last_error


def get_last_steam_error_context(item_id=None) -> dict:
    """返回最后一次 Steam 数据获取的结构化错误现场信息。
    传入 item_id 时优先返回批处理中该饰品的独立上下文。"""
    if item_id and item_id in _batch_errors:
        return _batch_errors[item_id]["context"]
    return _last_error_context


def _try_add_cookies(context, cookies: list, label: str):
    """安全地添加 cookies 到浏览器上下文，记录被拒绝的 cookie 而不是静默忽略。"""
    if not cookies:
        print(f"[Steam] {label}: 无 cookies")
        return
    rejected = 0
    for c in cookies:
        # 修复 sameSite 兼容性：Playwright 要求 exact 字符串
        if "sameSite" in c and c["sameSite"] not in ("Strict", "Lax", "None"):
            c["sameSite"] = "Lax"
        try:
            context.add_cookies([c])
        except Exception as ex:
            rejected += 1
    if rejected:
        print(f"[Steam] {label}: {len(cookies)} cookies 中 {rejected} 个被拒绝")


def get_steam_market_data(item_id: str, target_dates: List[date],
                           buff_item_name: str = "") -> Optional[dict]:
    """主函数：打开 BUFF 详情页 → 跳转 Steam → pricehistory API 提取价格历史。

    返回 dict: {steam_url, market_hash_name, steam_sold_count, steam_price_history}
    """
    global _last_error
    goods_url = f"https://buff.163.com/goods/{item_id}"
    target_set = set(target_dates)

    for attempt in range(2):
        result = _try_get_steam_data(item_id, goods_url, target_set,
                                     buff_item_name, attempt)
        if result is not None:
            return result
        if attempt == 0:
            print(f"[Steam] 第1次失败，等待后重试...（错误: {_last_error}）")
            import time
            time.sleep(5)

    return None


def _try_get_steam_data(item_id: str, goods_url: str,
                         target_set: set, buff_item_name: str,
                         attempt: int) -> Optional[dict]:
    """单次尝试获取 Steam 数据，支持尝试次数追踪。"""
    global _last_error, _last_error_context
    result = None
    # 重置上下文
    _last_error_context = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=config.PROXY_CONFIG)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )

        # 加载 cookies（Steam 优先，决定货币为 USD）
        _try_add_cookies(context,
                         _load_cookies_from(config.STEAM_COOKIE_PATH), "Steam")
        _try_add_cookies(context,
                         _load_cookies_from(config.COOKIE_PATH), "BUFF")

        page = context.new_page()

        try:
            page.goto(goods_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            sleep_random(1.0, 2.0)

            # ── 检查 BUFF 页面是否正常（防止已下架/404） ──
            buff_title = page.title()
            buff_url_current = page.url
            if any(kw in buff_title.lower()
                   for kw in ["页面不存在", "出错", "404", "not found", "error"]):
                _last_error = "BUFF 详情页异常（页面不存在或已下架）"
                _last_error_context = {
                    "type": "buff_page_error",
                    "buff_url": goods_url,
                    "current_url": buff_url_current,
                    "page_title": buff_title,
                    "detail": "该饰品在 BUFF 上可能已下架或链接失效",
                }
                print(f"[Steam] {_last_error} | title={buff_title}")
                return None

            btn = _find_steam_market_button(page)
            if not btn:
                _last_error = "未找到 '查看Steam市场' 按钮"
                _last_error_context = {
                    "type": "button_not_found",
                    "buff_url": goods_url,
                    "current_url": buff_url_current,
                    "page_title": buff_title,
                    "detail": "BUFF页面未加载完整或该饰品没有关联的Steam市场页面",
                }
                print(f"[Steam] {_last_error}")
                return None

            # ── 点击 Steam 按钮，处理新标签页 / 当前标签页两种情形 ──
            steam_page = _click_and_get_steam_page(page, context, btn)
            if steam_page is None:
                current_url = page.url[:100]
                _last_error = f"无法打开 Steam 页面（当前 URL: {current_url}）"
                _last_error_context = {
                    "type": "steam_page_not_opened",
                    "buff_url": goods_url,
                    "current_url": current_url,
                    "page_title": buff_title,
                    "detail": "href导航/dispatchEvent/当前标签页降级 三种方式均失败",
                }
                print(f"[Steam] {_last_error}")
                return None

            steam_url = steam_page.url
            market_hash_name = _extract_market_hash_name(steam_url) or ""

            # 等待 Steam 页面加载，检测是否显示错误或登录页
            try:
                steam_page.wait_for_load_state("networkidle", timeout=90000)
            except Exception as e:
                print(f"[Steam] 等待页面加载超时: {e}")

            # 检查 Steam 页面是否正常（不是错误页）
            steam_title = steam_page.title()
            if any(word in steam_title.lower()
                   for word in ["sorry", "error", "not found"]):
                _last_error = f"Steam 返回了错误页面（title: {steam_title}）"
                _last_error_context = {
                    "type": "steam_error_page",
                    "buff_url": goods_url,
                    "steam_url": steam_url[:100],
                    "page_title": steam_title,
                    "detail": "Steam 返回了 'Sorry' / 'Error' 类错误页",
                }
                print(f"[Steam] {_last_error}")
                return None

            # 检查是否被重定向到 Steam 登录页
            if "login" in steam_page.url.lower() and "market" not in steam_page.url.lower():
                _last_error = f"Steam 重定向到登录页: {steam_page.url[:100]}"
                _last_error_context = {
                    "type": "steam_login_redirect",
                    "buff_url": goods_url,
                    "redirect_url": steam_page.url[:100],
                    "page_title": steam_title,
                    "detail": "Steam cookie 可能已过期，需重新登录",
                }
                print(f"[Steam] {_last_error}")
                return None

            # 强制美元：重新导航到同 URL 追加 ?cc=us 参数
            if "?cc=us" not in steam_page.url:
                usd_url = steam_url.split("?")[0] + "?cc=us"
                try:
                    steam_page.goto(usd_url, timeout=45000)
                    steam_page.wait_for_load_state("networkidle", timeout=45000)
                except Exception as e:
                    print(f"[Steam] ?cc=us 导航超时: {e}")
                if "?cc=us" not in steam_page.url:
                    print(f"[Steam] 警告: ?cc=us 重定向后 URL 不包含参数: {steam_page.url[:100]}")
            steam_url = steam_page.url
            sleep_random(1.5, 2.5)

            # 解析 BUFF 饰品属性
            props = _parse_buff_item_properties(buff_item_name)
            print(f"[Steam] BUFF属性: wear={props['wear']}, StatTrak={props['stattrak']}")

            # 点击品质过滤按钮
            _click_quality_button(steam_page, props["stattrak"])
            # 点击磨损等级标签
            _click_wear_button(steam_page, props["wear"])

            # 等待 React 重渲染
            sleep_random(1.5, 2.5)

            # 提取 SSR 数据中的 buckets
            ssr_data = _extract_ssr_buckets(steam_page)
            if not ssr_data:
                _last_error = "无法提取 Steam SSR buckets 数据"
                _last_error_context = {
                    "type": "ssr_extraction_failed",
                    "steam_url": steam_url[:100],
                    "page_title": steam_title,
                    "detail": "window.SSR.loaderData 不存在或格式不符",
                }
                print(f"[Steam] {_last_error}")
                return None

            buckets = ssr_data.get("buckets", [])
            initial_fallback_id = ssr_data.get("initialFallbackBucketID")

            print(f"[Steam] 获取到 {len(buckets)} 个 bucket")
            for bk in buckets[:5]:
                print(f"  - {bk.get('localized_name', '?')}: {bk.get('strPrice', '?')}")

            # 匹配 bucket
            matched = _match_bucket(buckets, props)
            if not matched and initial_fallback_id:
                for bk in buckets:
                    if bk.get("bucket_id") == initial_fallback_id:
                        matched = bk
                        print(f"[Steam] 使用 fallback bucket: {bk.get('localized_name')}")
                        break

            if not matched:
                _last_error = (
                    f"未找到匹配的 Steam bucket"
                    f"（BUFF属性: wear={props.get('wear')}, "
                    f"StatTrak={props.get('stattrak')}）"
                )
                _last_error_context = {
                    "type": "bucket_match_failed",
                    "steam_url": steam_url[:100],
                    "buff_props": props,
                    "available_buckets": [
                        b.get("localized_name") for b in buckets[:20]
                    ],
                    "detail": "Steam listing 页中没有与 BUFF 变体属性匹配的 bucket",
                }
                print(f"[Steam] {_last_error}")
                return None

            bucket_name = matched.get("localized_name", "")
            bucket_min_price = matched.get("min_price")
            print(f"[Steam] 匹配 bucket: {bucket_name} 价格: {matched.get('strPrice')}")

            # 调用 pricehistory API
            price_history = _fetch_price_history(steam_page, bucket_name, target_set)

            steam_sold_count = sum(r.volume for r in price_history)

            # 当前价格：从 bucket min_price（美分）转换，fallback 到 price history 均价
            if bucket_min_price is not None:
                try:
                    steam_current_price = float(bucket_min_price) / 100.0
                except (ValueError, TypeError):
                    steam_current_price = None
            else:
                steam_current_price = None
            if steam_current_price is None and price_history:
                steam_current_price = sum(r.price for r in price_history) / len(price_history)
            print(f"[Steam] 当前价格: ${steam_current_price:.2f}" if steam_current_price else "[Steam] 无法确定当前价格")

            steam_page.close()

            result = {
                "steam_url": steam_url,
                "market_hash_name": bucket_name,
                "steam_price": steam_current_price,
                "steam_sold_count": steam_sold_count,
                "steam_price_history": price_history,
                "date_records": [
                    {"date": r.date.isoformat(), "steam_price": r.price,
                     "steam_volume": r.volume}
                    for r in price_history
                ],
            }
        except Exception as e:
            import traceback
            _last_error = f"Steam 浏览器异常: {e}"
            _last_error_context = {
                "type": "exception",
                "buff_url": goods_url,
                "exception": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
            print(f"[Steam] {_last_error}")
            traceback.print_exc()
        finally:
            browser.close()

        return result


def get_steam_market_data_batch(
    representative_item_id: str,
    group_members: List[dict],
) -> dict:
    """批量获取同一基础名下多个变体的 Steam 数据，只打开一次 Steam 页面。

    Args:
        representative_item_id: 组内任意一个 item_id，用于导航到 Steam listing。
        group_members: [{"item_id": str, "buff_item_name": str,
                         "target_dates": List[date]}, ...]

    Returns:
        {item_id: {steam_url, market_hash_name, steam_price,
                   steam_sold_count, steam_price_history, date_records},
         ...}
    """
    global _last_error, _last_error_context, _batch_errors
    _last_error_context = {}
    _batch_errors.clear()
    goods_url = f"https://buff.163.com/goods/{representative_item_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=config.PROXY_CONFIG)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        _try_add_cookies(context,
                         _load_cookies_from(config.STEAM_COOKIE_PATH), "Steam")
        _try_add_cookies(context,
                         _load_cookies_from(config.COOKIE_PATH), "BUFF")
        page = context.new_page()

        results = {}
        steam_page = None

        try:
            # ── ① 打开 BUFF 详情页 ──
            buff_title = ""
            page.goto(goods_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            sleep_random(1.0, 2.0)
            buff_title = page.title()

            # ── ② 检查 BUFF 页面是否正常 ──
            if any(kw in buff_title.lower()
                   for kw in ["页面不存在", "出错", "404", "not found", "error"]):
                _last_error = "BUFF 详情页异常（页面不存在或已下架）"
                _last_error_context = {
                    "type": "buff_page_error",
                    "group_items": len(group_members),
                    "buff_url": goods_url,
                    "page_title": buff_title,
                    "detail": "代表饰品在 BUFF 上可能已下架，整组跳过",
                }
                print(f"[Steam][批] {_last_error} | title={buff_title}")
                return results

            # ── ③ 查找 Steam 按钮 ──
            btn = _find_steam_market_button(page)
            if not btn:
                _last_error = "未找到 '查看Steam市场' 按钮"
                _last_error_context = {
                    "type": "button_not_found",
                    "group_items": len(group_members),
                    "buff_url": goods_url,
                    "page_title": buff_title,
                    "detail": "BUFF页面未加载完整或该组饰品均无Steam市场链接",
                }
                print(f"[Steam][批] {_last_error}")
                return results

            # ── ④ 跳转 Steam ──
            steam_page = _click_and_get_steam_page(page, context, btn)
            if steam_page is None:
                _last_error = "无法打开 Steam 页面"
                _last_error_context = {
                    "type": "steam_page_not_opened",
                    "group_items": len(group_members),
                    "buff_url": goods_url,
                    "page_title": buff_title,
                    "detail": "href/dispatchEvent/当前标签页降级 三种方式均失败",
                }
                print(f"[Steam][批] {_last_error}")
                return results

            steam_url = steam_page.url
            try:
                steam_page.wait_for_load_state("networkidle", timeout=90000)
            except Exception:
                pass
            if "?cc=us" not in steam_page.url:
                usd_url = steam_url.split("?")[0] + "?cc=us"
                try:
                    steam_page.goto(usd_url, timeout=45000)
                    steam_page.wait_for_load_state("networkidle", timeout=45000)
                except Exception:
                    pass
            steam_url = steam_page.url
            sleep_random(1.5, 2.5)

            # ── ⑤ 提取 SSR buckets（一次提取，多次匹配） ──
            ssr_data = _extract_ssr_buckets(steam_page)
            if not ssr_data:
                _last_error = "无法提取 Steam SSR buckets 数据"
                _last_error_context = {
                    "type": "ssr_extraction_failed",
                    "group_items": len(group_members),
                    "steam_url": steam_url[:100],
                    "detail": "window.SSR.loaderData 不存在或格式不符",
                }
                print(f"[Steam][批] {_last_error}")
                return results

            buckets = ssr_data.get("buckets", [])
            initial_fallback_id = ssr_data.get("initialFallbackBucketID")
            print(f"[Steam][批] 获取到 {len(buckets)} 个 bucket，"
                  f"处理 {len(group_members)} 个变体")

            # ── ⑥ 逐个匹配变体 + 调用 pricehistory API ──
            for member in group_members:
                item_id = member["item_id"]
                buff_item_name = member["buff_item_name"]
                target_set = set(member["target_dates"])

                props = _parse_buff_item_properties(buff_item_name)
                matched = _match_bucket(buckets, props)
                if not matched and initial_fallback_id:
                    for bk in buckets:
                        if bk.get("bucket_id") == initial_fallback_id:
                            matched = bk
                            break

                if not matched:
                    _batch_errors[item_id] = {
                        "error": f"未匹配到Steam bucket (wear={props.get('wear')}, StatTrak={props.get('stattrak')})",
                        "context": {
                            "type": "batch_bucket_match_failed",
                            "item_id": item_id,
                            "buff_item_name": buff_item_name,
                            "buff_props": props,
                            "available_buckets": [
                                b.get("localized_name") for b in buckets[:20]
                            ],
                            "detail": "该变体在 Steam listing 中无对应 bucket",
                        },
                    }
                    print(f"[Steam][批] ❌ 未匹配: {buff_item_name}")
                    continue

                bucket_name = matched.get("localized_name", "")
                bucket_min_price = matched.get("min_price")
                print(f"[Steam][批] ✅ {buff_item_name} → {bucket_name}")

                price_history = _fetch_price_history(
                    steam_page, bucket_name, target_set)

                steam_sold_count = sum(r.volume for r in price_history)

                if bucket_min_price is not None:
                    try:
                        steam_current_price = float(bucket_min_price) / 100.0
                    except (ValueError, TypeError):
                        steam_current_price = None
                else:
                    steam_current_price = None
                if steam_current_price is None and price_history:
                    steam_current_price = (
                        sum(r.price for r in price_history) / len(price_history))

                results[item_id] = {
                    "steam_url": steam_url,
                    "market_hash_name": bucket_name,
                    "steam_price": steam_current_price,
                    "steam_sold_count": steam_sold_count,
                    "steam_price_history": price_history,
                    "date_records": [
                        {"date": r.date.isoformat(), "steam_price": r.price,
                         "steam_volume": r.volume}
                        for r in price_history
                    ],
                }

            if steam_page:
                try:
                    steam_page.close()
                except Exception:
                    pass

        except Exception as e:
            import traceback
            _last_error = f"Steam 批处理异常: {e}"
            _last_error_context = {
                "type": "batch_exception",
                "exception": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
            print(f"[Steam][批] {_last_error}")
            traceback.print_exc()
        finally:
            browser.close()

    return results


def retry_steam_failed_items(failed_items: List[dict]) -> dict:
    """独立重试之前 Step 3 失败的饰品，按皮肤名重新分组批处理。

    Args:
        failed_items: [{"item_id", "buff_item_name", "target_dates"}] 列表。

    Returns:
        {item_id: steam_data_dict, ...}（同 get_steam_market_data_batch）
    """
    if not failed_items:
        return {}

    # 按皮肤名重新分组
    name_groups = {}
    for fi in failed_items:
        base_name = fi.get("base_skin_name") or _get_base_skin_name(
            fi.get("buff_item_name", ""))
        name_groups.setdefault(base_name, []).append(fi)

    all_results = {}
    total = len(name_groups)
    for idx, (base_name, members) in enumerate(name_groups.items()):
        print(f"[Steam][重试] [{idx+1}/{total}] 组: {base_name} ({len(members)}个变体)")
        batch_results = get_steam_market_data_batch(
            representative_item_id=members[0]["item_id"],
            group_members=members,
        )
        all_results.update(batch_results)

    return all_results


def _click_and_get_steam_page(page, context, btn):
    """打开 Steam 页面，支持 3 种方式（依次尝试）：
    1. 从按钮 href 直接导航（绕过验证码遮罩层）
    2. dispatchEvent 点击（绕过验证码遮罩层，但可能被弹窗拦截）
    3. 当前标签页降级（BUFF 在当前标签页跳转）
    """
    # ── 方式1：从按钮 href 直接导航到 Steam（最可靠，绕过所有遮罩层） ──
    try:
        href = btn.get_attribute("href")
        if href:
            if not href.startswith("http"):
                href = "https:" + href if href.startswith("//") else "https://buff.163.com" + href
            if "steamcommunity.com/market" in href or "steampowered.com/market" in href:
                print(f"[Steam] 方式1: 直接导航到 Steam URL")
                sp = context.new_page()
                sp.goto(href, timeout=60000)
                return sp
    except Exception as e:
        print(f"[Steam] 方式1 失败: {e}")

    # ── 方式2：dispatchEvent 点击（绕过验证码遮罩层） ──
    try:
        with context.expect_page(timeout=15000) as new_page_event:
            btn.dispatchEvent("click")
        sp = new_page_event.value
        try:
            sp.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass
        if "steamcommunity.com/market" in sp.url.lower() or "steampowered.com/market" in sp.url.lower():
            print(f"[Steam] 方式2: dispatchEvent 打开新标签页")
            return sp
        try:
            sp.close()
        except Exception:
            pass
    except Exception as e:
        print(f"[Steam] 方式2 未生效 ({type(e).__name__})，尝试当前标签页降级...")

    # ── 方式3：当前标签页降级 ──
    sleep_random(2.0, 3.0)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    current_url = page.url
    if "steamcommunity.com/market" in current_url or "steampowered.com/market" in current_url:
        print(f"[Steam] 方式3: 当前标签页已跳转到 Steam")
        return page

    print(f"[Steam] ❌ 三种方式均无法打开 Steam 页面（当前 URL: {current_url[:100]}）")
    return None


# ═══════════════════════════════════════════════════════════════════
# SSR 数据提取
# ═══════════════════════════════════════════════════════════════════

def _extract_ssr_buckets(page) -> dict | None:
    """从 Steam 页面的 SSR.loaderData 中提取 item listing 数据（含 buckets）。"""
    try:
        raw = page.evaluate("""() => {
            if (!window.SSR || !window.SSR.loaderData) return null;
            // loaderData[3] 通常是 item listing 数据
            // ⚠️ 远期风险: Steam 前端 SSR 结构调整可能导致 buckets 出现在不同索引甚至变换数据格式。
            //   到时需更新此处遍历逻辑或改用其它字段定位。当前遍历 + try/except 有基本容错。
            for (var i = 0; i < window.SSR.loaderData.length; i++) {
                try {
                    var d = JSON.parse(window.SSR.loaderData[i]);
                    if (d.buckets && Array.isArray(d.buckets)) {
                        return JSON.stringify(d);
                    }
                } catch(e) {}
            }
            return null;
        }""")
        if raw:
            return json.loads(raw)
    except Exception as e:
        print(f"[Steam] SSR 提取失败: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════
# Price History API
# ═══════════════════════════════════════════════════════════════════

def _fetch_price_history(page, market_hash_name: str,
                          target_dates: Set[date]) -> List[PriceRecord]:
    """调用 Steam pricehistory API 获取历史价格，按 target_dates 过滤。"""
    encoded = urllib.parse.quote(market_hash_name, safe='')
    api_url = (
        f"https://steamcommunity.com/market/pricehistory/"
        f"?appid=730&market_hash_name={encoded}"
    )

    try:
        raw = page.evaluate(f"""async () => {{
            try {{
                var resp = await fetch('{api_url}');
                var text = await resp.text();
                return {{status: resp.status, body: text}};
            }} catch(e) {{
                return {{error: e.message}};
            }}
        }}""")
    except Exception as e:
        print(f"[Steam] API 调用失败: {e}")
        return []

    if not raw or raw.get("status") != 200:
        print(f"[Steam] API 返回非 200: {raw}")
        return []

    try:
        data = json.loads(raw["body"])
    except json.JSONDecodeError as e:
        print(f"[Steam] API JSON 解析失败: {e}")
        return []

    if not data.get("success"):
        print(f"[Steam] API success=false")
        return []

    prices = data.get("prices", [])
    print(f"[Steam] API 返回 {len(prices)} 条价格记录")

    records = []
    seen_dates = set()

    for entry in prices:
        try:
            date_str = entry[0]  # "Jul 15 2025 01: +0"
            price = float(entry[1])
            volume = int(entry[2]) if len(entry) > 2 else 0

            pt_date = _parse_api_date(date_str)
            if pt_date is None:
                continue
            if target_dates and pt_date not in target_dates:
                continue
            if pt_date in seen_dates:
                continue
            seen_dates.add(pt_date)

            records.append(PriceRecord(date=pt_date, price=price, volume=volume))
        except (ValueError, IndexError, TypeError):
            continue

    print(f"[Steam] 匹配到 {len(records)} 条目标日期记录")
    for r in records:
        print(f"  {r.date}: ${r.price:.2f}, volume={r.volume}")

    return records


def _parse_api_date(date_str: str) -> date | None:
    """解析 pricehistory API 返回的日期字符串，如 'Jul 15 2025 01: +0'。"""
    try:
        cleaned = date_str.split(":")[0].strip()  # "Jul 15 2025 01"
        parts = cleaned.rsplit(" ", 1)
        if len(parts) == 2:
            dt = datetime.strptime(parts[0], "%b %d %Y")
            return dt.date()
        return None
    except (ValueError, AttributeError):
        return None


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════

def _find_steam_market_button(page):
    selectors = [
        'text="查看Steam市场"',
        'a:has-text("Steam市场")',
        'a:has-text("查看Steam")',
        'button:has-text("Steam市场")',
        'span:has-text("查看Steam市场")',
        'a[href*="steamcommunity.com"]',
        'a[href*="steampowered.com"]',
        '[class*="steam"]',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _extract_market_hash_name(steam_url: str) -> Optional[str]:
    try:
        path = urllib.parse.urlparse(steam_url).path
        m = re.search(r'/listings/\d+/(.+)', path)
        if m:
            return urllib.parse.unquote(m.group(1))
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
# 逐步诊断
# ═══════════════════════════════════════════════════════════════════

def diagnose_steam_extraction(item_id: str, target_dates: List[date],
                               buff_item_name: str = "") -> dict:
    """逐步诊断 Steam 数据提取流程，在同一次浏览器会话中逐步执行并记录结果。

    返回: {
        "steps": [{"name": "...", "ok": True/False, "detail": "..."}, ...],
        "price_history": [...],
        "steam_url": "...",
        "market_hash_name": "...",
    }
    """
    import tempfile, os as _os
    steps = []
    tmp_dir = tempfile.mkdtemp(prefix="steam_diag_")
    target_set = set(target_dates)
    price_history = []
    steam_url = ""
    market_hash_name = ""

    steps.append({"name": "配置信息", "ok": True,
                  "detail": f"Item ID: {item_id}\n名称: {buff_item_name}\n"
                            f"目标日期 (Step2日期-7天): {sorted(target_dates)}"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=config.PROXY_CONFIG)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )

        # 加载 cookies（与主函数保持一致）
        cookie_details = []
        steam_cookies = _load_cookies_from(config.STEAM_COOKIE_PATH)
        label_steam = config.STEAM_COOKIE_PATH.split("/")[-1].split("\\")[-1]
        if steam_cookies:
            _try_add_cookies(context, steam_cookies, "Steam(diag)")
            names = [c.get("name") for c in steam_cookies if c.get("name")]
            cookie_details.append(f"{label_steam}: {len(steam_cookies)} cookies ({', '.join(names[:5])}...)")
        else:
            cookie_details.append(f"{label_steam}: 无 cookie 文件")

        buff_cookies = _load_cookies_from(config.COOKIE_PATH)
        label_buff = config.COOKIE_PATH.split("/")[-1].split("\\")[-1]
        if buff_cookies:
            _try_add_cookies(context, buff_cookies, "BUFF(diag)")
            names = [c.get("name") for c in buff_cookies if c.get("name")]
            cookie_details.append(f"{label_buff}: {len(buff_cookies)} cookies ({', '.join(names[:5])}...)")
        else:
            cookie_details.append(f"{label_buff}: 无 cookie 文件")

        steps.append({"name": "① 加载 Cookies", "ok": True,
                      "detail": "\n".join(cookie_details)})

        page = context.new_page()

        try:
            # —— 步骤2：打开 BUFF 详情页 ——
            goods_url = f"https://buff.163.com/goods/{item_id}"
            page.goto(goods_url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            sleep_random(1.0, 2.0)
            ss1 = _os.path.join(tmp_dir, "01_buff_detail.png")
            page.screenshot(path=ss1)
            steps.append({"name": "② 打开BUFF详情页", "ok": True,
                          "detail": f"URL: {goods_url}", "screenshot": ss1})

            # —— 步骤3：查找 Steam 按钮 ——
            btn = _find_steam_market_button(page)
            if not btn:
                # 列出页面上所有 Steam 相关链接
                steam_links = page.evaluate("""() => {
                    var links = [];
                    document.querySelectorAll('a[href*="steam"]').forEach(function(a) {
                        links.push({href: a.getAttribute('href'), text: a.textContent.trim().substring(0,60)});
                    });
                    return links;
                }""")
                steps.append({"name": "③ 查找Steam按钮", "ok": False,
                              "detail": f"未找到 '查看Steam市场' 按钮\n"
                                        f"页面中 Steam 链接: {json.dumps(steam_links, ensure_ascii=False)[:500]}",
                              "data": steam_links})
                browser.close()
                return {"steps": steps, "price_history": [], "steam_url": "",
                        "market_hash_name": "", "tmp_dir": tmp_dir}

            btn_text = btn.text_content() or "(no text)"
            steps.append({"name": "③ 查找Steam按钮", "ok": True,
                          "detail": f"找到按钮: '{btn_text.strip()}'"})

            # —— 步骤4：点击 Steam 按钮（支持新标签页/当前标签页） ——
            steam_page = _click_and_get_steam_page(page, context, btn)
            if steam_page is None:
                steps.append({"name": "④ 打开Steam页面", "ok": False,
                              "detail": f"无法打开 Steam 页面（当前 URL: {page.url[:100]}）"})
                browser.close()
                return {"steps": steps, "price_history": [], "steam_url": "",
                        "market_hash_name": "", "tmp_dir": tmp_dir}

            steam_url = steam_page.url
            market_hash_name = _extract_market_hash_name(steam_url) or ""

            steam_page.wait_for_load_state("networkidle", timeout=60000)
            # 强制美元
            if "?cc=us" not in steam_page.url:
                usd_url = steam_url.split("?")[0] + "?cc=us"
                steam_page.goto(usd_url, timeout=30000)
                steam_page.wait_for_load_state("networkidle", timeout=30000)
            steam_url = steam_page.url
            sleep_random(1.5, 2.5)
            ss2 = _os.path.join(tmp_dir, "02_steam_page.png")
            steam_page.screenshot(path=ss2)
            steps.append({"name": "④ 打开Steam页面", "ok": True,
                          "detail": f"Steam URL: {steam_url[:120]}\n"
                                    f"URL 中 hash name: {market_hash_name}",
                          "screenshot": ss2})

            # —— 步骤5：解析 BUFF 属性 ——
            props = _parse_buff_item_properties(buff_item_name)
            steps.append({"name": "⑤ 解析BUFF饰品属性", "ok": True,
                          "detail": f"名称: {buff_item_name}\n"
                                    f"StatTrak: {props['stattrak']}\n"
                                    f"磨损: {props['wear']}"})

            # —— 步骤6：点击品质/磨损按钮 ——
            quality_ok = True
            wear_ok = True
            try:
                _click_quality_button(steam_page, props["stattrak"])
            except Exception as e:
                quality_ok = False
            try:
                _click_wear_button(steam_page, props["wear"])
            except Exception as e:
                wear_ok = False
            sleep_random(1.5, 2.5)
            steps.append({"name": "⑥ 点击变体过滤按钮", "ok": quality_ok and wear_ok,
                          "detail": f"品质按钮: {'✓' if quality_ok else '✗'} "
                                    f"(目标: {'StatTrak' if props['stattrak'] else '普通'})\n"
                                    f"磨损按钮: {'✓' if wear_ok else '✗'} "
                                    f"(目标: {props['wear']})"})

            # —— 步骤7：提取 SSR buckets ——
            ssr_data = _extract_ssr_buckets(steam_page)
            if not ssr_data:
                steps.append({"name": "⑦ 提取SSR数据", "ok": False,
                              "detail": "无法提取 SSR buckets（SSR.loaderData 可能不存在）"})
                steam_page.close()
                browser.close()
                return {"steps": steps, "price_history": [], "steam_url": steam_url,
                        "market_hash_name": market_hash_name, "tmp_dir": tmp_dir}

            buckets = ssr_data.get("buckets", [])
            bucket_list = "\n".join(
                f"  [{bk.get('bucket_id')}] {bk.get('localized_name', '?')}: {bk.get('strPrice', '?')}"
                for bk in buckets[:10]
            )
            steps.append({"name": "⑦ 提取SSR数据", "ok": True,
                          "detail": f"buckets 数量: {len(buckets)}\n"
                                    f"initialFallbackBucketID: {ssr_data.get('initialFallbackBucketID')}\n"
                                    f"前10个 bucket:\n{bucket_list}"})

            # —— 步骤8：匹配 bucket ——
            matched = _match_bucket(buckets, props)
            fallback_used = False
            if not matched:
                fallback_id = ssr_data.get("initialFallbackBucketID")
                for bk in buckets:
                    if bk.get("bucket_id") == fallback_id:
                        matched = bk
                        fallback_used = True
                        break

            if not matched:
                steps.append({"name": "⑧ 匹配变体Bucket", "ok": False,
                              "detail": f"未找到匹配 wear={props['wear']} "
                                        f"StatTrak={props['stattrak']} 的 bucket"})
                steam_page.close()
                browser.close()
                return {"steps": steps, "price_history": [], "steam_url": steam_url,
                        "market_hash_name": market_hash_name, "tmp_dir": tmp_dir}

            bucket_name = matched.get("localized_name", "")
            bucket_min_price = matched.get("min_price")
            steps.append({"name": "⑧ 匹配变体Bucket", "ok": True,
                          "detail": f"匹配: {bucket_name}\n"
                                    f"strPrice: {matched.get('strPrice')}\n"
                                    f"min_price: {bucket_min_price} (美分)\n"
                                    f"{'⚠️ 使用了 fallback' if fallback_used else ''}"})

            # —— 步骤9：调用 pricehistory API ——
            all_api_records = _fetch_price_history(steam_page, bucket_name, set())
            steps.append({"name": "⑨ 调用 pricehistory API", "ok": len(all_api_records) > 0,
                          "detail": f"API 返回 {len(all_api_records)} 条总记录\n"
                                    f"日期范围: {all_api_records[0].date if all_api_records else 'N/A'} "
                                    f"~ {all_api_records[-1].date if all_api_records else 'N/A'}"})

            # —— 步骤10：按 target_dates 过滤 ——
            price_history = [r for r in all_api_records if r.date in target_set]
            matched_dates = "\n".join(
                f"  {r.date}: ${r.price:.2f}, volume={r.volume}"
                for r in price_history
            ) if price_history else "(未匹配到任何目标日期)"

            missing = target_set - {r.date for r in all_api_records}
            detail_10 = f"目标日期: {sorted(target_set)}\n匹配结果:\n{matched_dates}"
            if missing:
                detail_10 += f"\n⚠️ 未在API数据中找到的日期: {sorted(missing)}"

            steps.append({"name": "⑩ 按目标日期过滤", "ok": len(price_history) > 0,
                          "detail": detail_10})

            steam_page.close()

        except Exception as e:
            import traceback
            steps.append({"name": "异常", "ok": False,
                          "detail": f"{e}\n{traceback.format_exc()}"})
        finally:
            browser.close()

    return {
        "steps": steps,
        "price_history": price_history,
        "steam_url": steam_url,
        "market_hash_name": market_hash_name,
        "tmp_dir": tmp_dir,
        "item_id": item_id,
        "target_dates": sorted(target_dates),
    }
