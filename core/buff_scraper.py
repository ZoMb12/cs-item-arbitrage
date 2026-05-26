import json
import os
from datetime import date, datetime
from typing import List

import requests
from playwright.sync_api import sync_playwright, Page, BrowserContext

import config
from data.models import ItemSnapshot, PriceRecord
from utils.helpers import sleep_random

_NETWORK_HOOKS_JS = """
    window.__hookedAll = window.__hookedAll || [];
    window.__hookedByUrl = window.__hookedByUrl || {};

    function _recordHooked(entry) {
        const key = entry.type + '|' + entry.method + '|' + entry.url;
        if (window.__hookedByUrl[key]) return;
        window.__hookedByUrl[key] = true;
        window.__hookedAll.push(entry);
    }

    if (!window.__xhrHookedFlag) {
        window.__xhrHookedFlag = true;
        const _origOpen = XMLHttpRequest.prototype.open;
        const _origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url) {
            this.__xhrInfo = { method: method, url: (url instanceof URL ? url.href : url) };
            return _origOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function(body) {
            const info = this.__xhrInfo || {};
            const xhr = this;
            xhr.addEventListener('loadend', function() {
                let responseText = null;
                try { responseText = xhr.responseText; } catch(e) {}
                _recordHooked({
                    type: 'xhr',
                    method: info.method || 'GET',
                    url: info.url || '',
                    status: xhr.status,
                    responseText: responseText ? responseText.substring(0, 8000) : null
                });
            });
            return _origSend.call(this, body);
        };
    }

    if (!window.__fetchHookedFlag) {
        window.__fetchHookedFlag = true;
        const _origFetch = window.fetch;
        window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input :
                (input instanceof Request ? input.url : input.toString());
            const method = (init && init.method) || 'GET';
            return _origFetch.apply(window, arguments).then(function(response) {
                const cloned = response.clone();
                cloned.text().then(function(text) {
                    _recordHooked({
                        type: 'fetch',
                        method: method,
                        url: url,
                        status: cloned.status,
                        responseText: text ? text.substring(0, 8000) : null
                    });
                }).catch(function() {});
                return response;
            });
        };
    }

    if (!window.__jqHookedFlag) {
        window.__jqHookedFlag = true;
        function _hookJQuery() {
            if (!window.$ || !window.$.ajax || window.$.__ajaxHooked) return;
            window.$.__ajaxHooked = true;
            const _origAjax = window.$.ajax;
            window.$.ajax = function(url, options) {
                if (typeof url === 'object') { options = url; url = options.url; }
                const opts = options || {};
                const targetUrl = url || opts.url || '';
                const method = opts.method || opts.type || 'GET';
                const origSuccess = opts.success;
                const origError = opts.error;
                opts.success = function(data, status, xhr) {
                    _recordHooked({
                        type: 'jq_ajax',
                        method: method,
                        url: targetUrl,
                        status: xhr ? xhr.status : 200,
                        responseText: JSON.stringify(data).substring(0, 8000)
                    });
                    if (origSuccess) return origSuccess.apply(this, arguments);
                };
                opts.error = function(xhr, status, err) {
                    _recordHooked({
                        type: 'jq_ajax',
                        method: method,
                        url: targetUrl,
                        status: xhr ? xhr.status : 0,
                        error: String(err)
                    });
                    if (origError) return origError.apply(this, arguments);
                };
                return _origAjax.call(window.$, opts);
            };
        }
        _hookJQuery();
        setTimeout(_hookJQuery, 200);
        setTimeout(_hookJQuery, 1000);
        setTimeout(_hookJQuery, 3000);
    }
"""

_ECHARTS_EXTRACT_JS = """
    () => {
        let allResults = {};
        let ec = typeof echarts !== 'undefined' ? echarts : null;
        let containers = [];
        document.querySelectorAll('[class*="chart"]').forEach(el => containers.push(el));
        document.querySelectorAll('canvas').forEach(canvas => {
            let p = canvas.parentElement;
            if (p && !containers.includes(p)) containers.push(p);
        });
        if (ec && ec.getAllDom) {
            try { ec.getAllDom().forEach(d => {
                if (!containers.includes(d)) containers.push(d);
            }); } catch(e) {}
        }
        for (const el of containers) {
            let instance = null;
            if (ec) { try { instance = ec.getInstanceByDom(el); } catch(e) {} }
            if (!instance) {
                for (let key of Object.keys(el)) {
                    if (key.startsWith('_echarts') || key === '__echarts_instance__') {
                        instance = el[key]; break;
                    }
                }
            }
            if (!instance) continue;
            let option;
            try { option = instance.getOption(); } catch(e) { continue; }
            if (!option || !option.series || option.series.length === 0) continue;
            let series = option.series;
            for (let i = 0; i < series.length; i++) {
                let s = series[i];
                let name = s.name || ('series_' + i);
                let data = [];
                if (s.data && Array.isArray(s.data) && s.data.length > 0) {
                    data = s.data.map(pt => {
                        if (Array.isArray(pt) && pt.length >= 2) return [pt[0], pt[1]];
                        if (typeof pt === 'object' && pt !== null) {
                            if (pt.value && Array.isArray(pt.value)) return [pt.value[0], pt.value[1]];
                            return [pt.x || pt.date || pt[0], pt.y || pt.price || pt[1]];
                        }
                        return null;
                    }).filter(Boolean);
                }
                if (data.length === 0 && option.dataset && option.dataset.length > 0) {
                    let ds = option.dataset[0];
                    let source = ds.source;
                    if (!source) continue;
                    let header = null, rows = null;
                    if (Array.isArray(source) && source.length > 0) {
                        if (Array.isArray(source[0])) { header = source[0]; rows = source.slice(1); }
                        else if (typeof source[0] === 'object') { header = Object.keys(source[0]); rows = source; }
                    } else if (source.dimensions && source.source) {
                        header = source.dimensions; rows = source.source;
                    }
                    if (header && rows) {
                        let xIdx = 0, yIdx = -1;
                        if (s.encode) {
                            if (s.encode.x != null) xIdx = Array.isArray(s.encode.x) ? s.encode.x[0] : s.encode.x;
                            if (s.encode.y != null) yIdx = Array.isArray(s.encode.y) ? s.encode.y[0] : s.encode.y;
                        }
                        if (yIdx < 0) {
                            yIdx = header.findIndex(h => String(h).includes(name) || String(h) === name);
                        }
                        if (yIdx < 0) yIdx = i + 1;
                        if (yIdx >= header.length) yIdx = 1;
                        data = rows.map(row => [row[xIdx], row[yIdx]]);
                    }
                }
                if (data.length > 0) { allResults[name] = data; }
            }
            if (Object.keys(allResults).length > 0) break;
        }
        return Object.keys(allResults).length > 0 ? allResults : null;
    }
"""


def _load_cookies_from(path: str) -> List[dict]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_cookies_to(path: str, cookies: List[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f)


def _get_buff_price_history_direct(item_id: str, game: str = "csgo",
                                     days: int = 30) -> tuple:
    """直接调用 BUFF /api/market/goods/price_history/buff/v2 获取价格历史。
    无需浏览器，使用 requests + 本地 cookies。
    返回 (data_dict, error_str)。成功时 data 为 dict，error 为 None；失败时 data 为 None，error 为描述。
    """
    cookies = _load_cookies_from(config.COOKIE_PATH)
    if not cookies:
        return None, "无 cookies 文件或 cookies 为空"
    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"],
                           domain=c.get("domain", ".buff.163.com"))

    url = "https://buff.163.com/api/market/goods/price_history/buff/v2"
    params = {"game": game, "goods_id": item_id, "currency": "CNY", "days": days}
    headers = {"Referer": f"https://buff.163.com/goods/{item_id}"}

    try:
        r = session.get(url, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("code") != "OK":
            return None, f"API code={data.get('code')}, msg={data.get('msg', '?')}"
        return data["data"], None
    except Exception as e:
        return None, str(e)


def _api_days_for_range(days_needed: int) -> int:
    """将需要的天数映射到 BUFF API 支持的 days 参数值。"""
    for d in (7, 30, 90, 180, 365, 730):
        if days_needed <= d:
            return d
    return 730


def _parse_buff_api_lines(api_data: dict, start_date: date, end_date: date,
                          preferred_line: str = "sell_min_price_history"
                          ) -> List[PriceRecord]:
    """从 BUFF buff/v2 API 响应中提取指定日期范围的 PriceRecord。
    优先取 preferred_line（在售最低），fallback 取第一个有数据的 line。
    """
    if not api_data:
        return []
    lines = api_data.get("lines", [])
    if not lines:
        return []

    target = None
    for line in lines:
        if line.get("key") == preferred_line:
            target = line
            break
    if not target:
        for line in lines:
            if line.get("points"):
                target = line
                break
    if not target:
        return []

    points = target.get("points", [])
    records = []
    for pt in points:
        try:
            if not isinstance(pt, list) or len(pt) < 2:
                continue
            ts, price = pt[0], float(pt[1])
            if isinstance(ts, (int, float)):
                pt_date = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts).date()
            else:
                pt_date = date.fromisoformat(str(ts).split(" ")[0].split("T")[0])
            if start_date <= pt_date <= end_date:
                records.append(PriceRecord(date=pt_date, price=price))
        except Exception:
            continue
    return records


def ensure_login():
    """检查BUFF登录态，若无有效Cookie则打开浏览器让用户手动登录。"""
    cookies = _load_cookies_from(config.COOKIE_PATH)
    if cookies:
        session_cookie_names = {"session", "buff_ss", "ntes_token"}
        if any(c.get("name") in session_cookie_names for c in cookies):
            print("已检测到有效登录态，跳过登录。")
            return

    print("正在打开Chrome浏览器，请在窗口中完成BUFF登录...")
    print("登录完成后，请关闭Chrome窗口，系统将自动保存登录态。")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto("https://buff.163.com/market/?game=csgo", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
        except Exception as e:
            print(f"导航到 BUFF 页面失败: {e}")

        print("请在弹出的Chrome窗口中完成BUFF登录。")
        print("登录完成后**关闭Chrome窗口**即可，系统会自动保存登录态。")

        import time
        start_time = time.time()
        session_cookie_names = {"session", "buff_ss", "ntes_token"}
        saved = False

        # 每0.5秒主动检测一次：发现登录态cookie → 立即保存；浏览器断开 → 最后尝试保存
        while time.time() - start_time < 300:
            try:
                cookies = context.cookies()
            except Exception:
                break

            if any(c.get("name") in session_cookie_names for c in cookies):
                print("检测到登录态，保存中...")
                _save_cookies_to(config.COOKIE_PATH, cookies)
                saved = True
                print(f"登录态保存成功，共 {len(cookies)} 个cookie。")
                time.sleep(2)
                break

            if not browser.is_connected():
                if not saved:
                    try:
                        cookies = context.cookies()
                        _save_cookies_to(config.COOKIE_PATH, cookies)
                        if any(c.get("name") in session_cookie_names for c in cookies):
                            print(f"登录态保存成功，共 {len(cookies)} 个cookie。")
                            saved = True
                    except Exception:
                        pass
                break
            time.sleep(0.5)

        if not saved and browser.is_connected():
            try:
                cookies = context.cookies()
                _save_cookies_to(config.COOKIE_PATH, cookies)
                if any(c.get("name") in session_cookie_names for c in cookies):
                    print(f"登录态保存成功，共 {len(cookies)} 个cookie。")
                else:
                    print(f"警告：未检测到登录态cookie。已保存 {len(cookies)} 个cookie。")
            except Exception:
                print("警告：无法获取 cookie，登录可能未完成。")

        try:
            browser.close()
        except Exception:
            pass


def is_logged_in() -> bool:
    """检查本地是否保存了有效的BUFF登录Cookie。"""
    cookies = _load_cookies_from(config.COOKIE_PATH)
    if not cookies:
        return False
    session_cookie_names = {"session", "buff_ss", "ntes_token"}
    return any(c.get("name") in session_cookie_names for c in cookies)


def _open_authenticated_page(url: str, cookie_path: str, label: str):
    """使用Playwright打开已认证页面，不自动关闭，供用户自行查看。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = _create_context_with_cookies(browser, cookie_path)
        page = context.new_page()
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            print(f"已打开{label}页面，请自行查看。关闭浏览器窗口即可退出。")
            page.wait_for_event("close", timeout=600_000)
        except Exception:
            pass
        finally:
            try:
                browser.close()
            except Exception:
                pass


def open_buff_page():
    _open_authenticated_page("https://buff.163.com/market/?game=csgo", config.COOKIE_PATH, "BUFF")


def _create_context_with_cookies(browser, cookie_path: str) -> BrowserContext:
    context = browser.new_context()
    cookies = _load_cookies_from(cookie_path)
    if cookies:
        context.add_cookies(cookies)
    return context


def _extract_price(text: str) -> float:
    """从价格文本中提取数字，如 '¥ 123.45' -> 123.45"""
    if not text:
        return 0.0
    cleaned = text.replace("¥", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _extract_volume(text: str) -> int:
    """从在售数量文本中提取数字，如 '在售: 30' -> 30"""
    if not text:
        return 0
    digits = "".join(c for c in text if c.isdigit())
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _extract_stock_from_card(card_text: str) -> int:
    """从卡片整体文本中匹配在售数量，如'在售 123件' -> 123。"""
    if not card_text:
        return 0
    import re
    # 匹配多种在售/库存表述
    patterns = [
        r"(?:在售|库存|stock|sale)[^\d]*(\d+)",
        r"(\d+)[^\d]*(?:件|个|把)",
    ]
    for pat in patterns:
        m = re.search(pat, card_text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def _fetch_category_items(page, category_value: str, target_count: int,
                          min_price: float, min_volume: int) -> list:
    """抓取单个品类的饰品列表，累计达到 target_count 或翻到最后一页为止。"""
    items = []
    page_num = 1

    while len(items) < target_count:
        if category_value:
            url = (f"https://buff.163.com/market/csgo"
                   f"#game=csgo&page_num={page_num}&category_group={category_value}&tab=selling")
        else:
            url = (f"https://buff.163.com/market/csgo"
                   f"#game=csgo&page_num={page_num}&tab=selling&min_price={int(min_price)}")

        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        sleep_random(1.0, 3.0)

        cards = page.query_selector_all("li.selling")
        if not cards:
            cards = page.query_selector_all("[class*='market_listing_row']")
        if not cards:
            cards = page.query_selector_all("[class*='goods_item']")
        if not cards:
            cards = page.query_selector_all("li")

        new_in_page = 0
        for card in cards:
            try:
                name_el = card.query_selector("[class*='goods_name'], .name, h3, a[title]")
                price_el = card.query_selector("[class*='price'], .cost, strong")
                link_el = card.query_selector("a[href*='/goods/']")

                name = name_el.get_attribute("title") or name_el.inner_text() if name_el else ""
                price = _extract_price(price_el.inner_text()) if price_el else 0.0
                href = link_el.get_attribute("href") if link_el else ""
                item_id = href.split("/goods/")[-1].split("?")[0] if "/goods/" in href else name

                volume = 0
                volume_el = card.query_selector(
                    "[class*='stock'], [class*='on_sale'], [class*='sale-num'], "
                    "[class*='num'], [class*='count'], .tag, .label"
                )
                if volume_el:
                    volume = _extract_volume(volume_el.inner_text())
                if volume == 0:
                    volume = _extract_stock_from_card(card.inner_text())

                if name and price > min_price and volume > min_volume:
                    items.append(ItemSnapshot(
                        item_id=item_id,
                        name=name.strip(),
                        buff_price=price,
                        volume=volume,
                    ))
                    new_in_page += 1
                    if len(items) >= target_count:
                        break
            except Exception:
                continue

        # 最后一页检测
        next_btn = page.query_selector("a.next:not(.disabled), [class*='pagination-next']")
        if not next_btn:
            break
        page_num += 1

    return items


def get_items_on_date(target_date, target_count: int = 200,
                      categories: list = None, min_price: float = 20.0,
                      min_volume: int = 100):
    """获取目标日期的饰品列表。

    categories: 品类 value 列表，如 ["hands", "type_customplayer"]。
                None 或包含空字符串表示"全部"（不加品类筛选）。
    多品类时各品类独立抓取，数量均分。
    """
    if not categories or any(c in ("", "全部/不限") for c in categories):
        categories = [""]

    per_category = max(1, target_count // len(categories))

    all_items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = _create_context_with_cookies(browser, config.COOKIE_PATH)
        page = context.new_page()

        for i, cat in enumerate(categories):
            cat_label = cat or "全部"
            cat_items = _fetch_category_items(page, cat, per_category, min_price, min_volume)
            all_items.extend(cat_items)

            remain = target_count - len(all_items)
            # 如果某个品类抓不够，把差额匀给后续品类
            if i < len(categories) - 1 and len(cat_items) < per_category:
                shortfall = per_category - len(cat_items)
                remaining_cats = len(categories) - i - 1
                per_category = max(1, per_category + shortfall // remaining_cats)

        browser.close()

    return all_items


def _choose_time_range(days_needed: int, start_date: date = None) -> str:
    """根据考察起始日期和跨度，返回BUFF价格走势页面应点击的时间范围文本。

    BUFF的时间范围按钮控制"从今天往前看多少天"。如果 start_date 距今较远
    （比如要考察去年8月的数据），必须选足够大的范围才能覆盖目标区间。
    """
    if start_date:
        days_back = (date.today() - start_date).days + days_needed
        needed = max(days_needed, days_back)
    else:
        needed = days_needed

    if needed <= 7:
        return "7天"
    elif needed <= 30:
        return "1个月"
    elif needed <= 90:
        return "3个月"
    elif needed <= 180:
        return "6个月"
    elif needed <= 365:
        return "1年"
    else:
        return "最近2年"


def _choose_time_range_by_label(label: str) -> str:
    """将用户选择的时间范围标签映射到BUFF页面上的按钮文本。"""
    mapping = {
        "最近3个月": "3个月",
        "最近6个月": "6个月",
        "最近1年": "1年",
    }
    return mapping.get(label, "3个月")


def _select_time_range(page, range_text: str) -> bool:
    """在 BUFF 价格走势页面上选择时间范围。

    BUFF 使用自定义下拉组件 #price-history-days（class="w-Select"），
    内部是 <ul> + <li data-value="..."> 结构。点击下拉框 → 展开选项 → 点击目标 <li>。
    返回 True 表示选择成功。
    """
    try:
        # 等待图表区域加载完成（#price-history-days 是图表工具栏的一部分）
        dropdown = page.query_selector("#price-history-days")
        if not dropdown:
            # 可能图表还没渲染完，等待一下再试
            sleep_random(2.0, 3.0)
            dropdown = page.query_selector("#price-history-days")
        if not dropdown:
            # 降级：按 class 和 name 属性找
            dropdown = page.query_selector(".w-Select[name=\"days\"]")
        if not dropdown:
            # 再降级：直接找包含时间范围文本的可见 li 元素
            for li in page.query_selector_all("li"):
                if li.inner_text().strip() == range_text and li.is_visible():
                    li.click()
                    sleep_random(1.0, 2.0)
                    page.wait_for_load_state("networkidle", timeout=60000)
                    return True
            return False

        # 点击下拉框展开选项
        dropdown.click()
        sleep_random(0.5, 1.0)
        # 在下拉框中找匹配文本的 <li>，精确匹配
        li = dropdown.query_selector(f"li:has-text(\"{range_text}\")")
        if not li:
            return False
        li.click()
        sleep_random(1.0, 2.0)
        page.wait_for_load_state("networkidle", timeout=60000)
        return True
    except Exception:
        return False


# ---- 网络拦截：捕获 BUFF API 价格数据，比 ECharts JS 提取更可靠 ----
def _setup_network_capture(page):
    """设置网络响应拦截，捕获 ALL 响应（不仅 JSON），返回 dict:
    {"responses": [...], "request_urls": [...]}
    """
    result = {"responses": [], "request_urls": []}

    def on_request(request):
        result["request_urls"].append({
            "url": request.url,
            "method": request.method,
            "resource_type": request.resource_type,
        })

    def on_response(response):
        entry = {
            "url": response.url,
            "status": response.status,
            "content_type": response.headers.get("content-type", ""),
        }
        ct = entry["content_type"]
        # 尝试解析 JSON body
        if ct and "json" in ct.lower():
            try:
                entry["body"] = response.json()
            except Exception:
                pass
        else:
            # 对非 JSON 响应也尝试解析，但保留原始 text（截断）
            try:
                text = response.text()
                entry["text"] = text[:3000] if len(text) > 3000 else text
                # 尝试 JSON 解析
                try:
                    entry["body"] = json.loads(text)
                    entry["content_type"] = "application/json (detected)"
                except Exception:
                    pass
            except Exception:
                pass
        result["responses"].append(entry)

    page.on("request", on_request)
    page.on("response", on_response)
    return result


def _inject_network_hooks(page):
    """注入 JS 层级的 XHR/fetch/jQuery 拦截到 window.__hookedAll。"""
    page.add_init_script(_NETWORK_HOOKS_JS)


def _collect_hooked_data(page):
    """安全地从页面收集 JS 层级的 hook 数据。
    处理 Execution context was destroyed 错误，返回空列表失败而不是抛异常。
    """
    try:
        return page.evaluate("() => window.__hookedAll || []")
    except Exception:
        return []


def _extract_from_captured(captured, start_date, end_date):
    """从拦截到的网络响应中提取指定日期范围内的 PriceRecord 列表。
    优先识别 /buff/v2 API 响应，其次检查 sell_order 等。
    captured 可以是旧 list 或新 dict{"responses": [...], "request_urls": [...]}。
    """
    responses = captured["responses"] if isinstance(captured, dict) else captured
    for resp in responses:
        body = resp.get("body")
        if not body:
            continue
        url = resp.get("url", "")
        # 优先处理 /buff/v2 响应：data.lines[].points 格式
        if "buff/v2" in url and isinstance(body, dict):
            lines = body.get("data", {}).get("lines", []) if isinstance(body.get("data"), dict) else body.get("lines", [])
            for line in lines:
                pts = line.get("points", [])
                if pts:
                    records = _parse_points(pts, start_date, end_date)
                    if records:
                        return records
        # 通用递归搜索
        records = _search_price_data(body, start_date, end_date)
        if records:
            return records
        # sell_order 特殊提取：goods_infos 可能包含历史价格
        if isinstance(body, dict) and body.get("data"):
            data = body["data"]
            if isinstance(data, dict):
                goods_infos = data.get("goods_infos") or {}
                if isinstance(goods_infos, dict):
                    for key in ("price_history", "history", "prices", "price_trend", "history_prices"):
                        if key in goods_infos:
                            records = _search_price_data(goods_infos[key], start_date, end_date)
                            if records:
                                return records
                    # goods_infos 本身可能包含 price/min_price 等字段
                    for key, val in goods_infos.items():
                        if isinstance(val, list) and len(val) > 0:
                            records = _search_price_data(val, start_date, end_date)
                            if records:
                                return records
    return []


def _search_price_data(obj, start_date, end_date, depth=0):
    """递归搜索 JSON，寻找 [[ts, price], ...] 或 [{date, price}, ...] 格式的价格数据。"""
    if depth > 8:
        return []
    if isinstance(obj, list) and len(obj) > 0:
        first = obj[0]
        if isinstance(first, list) and len(first) >= 2:
            return _parse_points(obj, start_date, end_date)
        if isinstance(first, dict):
            keys = {str(k).lower() for k in first.keys()}
            if any(k in keys for k in ("price", "p", "value", "y", "v")):
                return _parse_points(obj, start_date, end_date)
            for item in obj[:10]:
                result = _search_price_data(item, start_date, end_date, depth + 1)
                if result:
                    return result
    if isinstance(obj, dict):
        for key in ("data", "price_history", "prices", "history", "list",
                     "result", "records", "items", "points", "series"):
            if key in obj:
                result = _search_price_data(obj[key], start_date, end_date, depth + 1)
                if result:
                    return result
        for key, val in obj.items():
            result = _search_price_data(val, start_date, end_date, depth + 1)
            if result:
                return result
    return []


def _parse_points(points, start_date, end_date):
    """将 [[ts, price], ...] 或 [{date, price}, ...] 解析为 PriceRecord 列表。"""
    records = []
    for pt in points:
        try:
            ts = None
            val = None
            if isinstance(pt, list) and len(pt) >= 2:
                ts, val = pt[0], float(pt[1])
            elif isinstance(pt, dict):
                ts = (pt.get("date") or pt.get("x") or pt.get("t")
                      or pt.get("time") or pt.get("timestamp"))
                val_raw = (pt.get("price") or pt.get("y") or pt.get("v")
                           or pt.get("value"))
                if ts is None or val_raw is None:
                    continue
                val = float(val_raw)
            else:
                continue
            if isinstance(ts, (int, float)):
                pt_date = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts).date()
            elif isinstance(ts, str):
                pt_date_str = str(ts).split(" ")[0].split("T")[0]
                pt_date = date.fromisoformat(pt_date_str)
            else:
                continue
            if start_date <= pt_date <= end_date:
                records.append(PriceRecord(date=pt_date, price=val))
        except Exception:
            continue
    return records


def _extract_echarts_data(page):
    """从页面 ECharts 实例提取所有 series 数据，返回 {series_name: [[ts, price], ...]} 或 None。"""
    return page.evaluate(_ECHARTS_EXTRACT_JS)


def get_full_price_history(item_id: str, time_label: str = "最近3个月") -> dict:
    """获取饰品在指定时间范围内的完整历史价格数据（所有曲线）。
    优先直接调 BUFF API（快速），失败降级到 Playwright。
    返回 {series_name: [(date, value), ...]}
    """
    # 将 time_label 映射到天数
    label_to_days = {"最近3个月": 90, "最近6个月": 180, "最近1年": 365}
    days = label_to_days.get(time_label, 90)

    # ---- 方式1：直接 API ----
    api_data, _ = _get_buff_price_history_direct(item_id, game="csgo", days=days)
    if api_data:
        result = {}
        for line in api_data.get("lines", []):
            name = line.get("name", line.get("key", "?"))
            points = line.get("points", [])
            if not points:
                continue
            clean_points = []
            for pt in points:
                try:
                    if isinstance(pt, list) and len(pt) >= 2:
                        ts, val = pt[0], float(pt[1])
                        if isinstance(ts, (int, float)):
                            d = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts).date()
                        else:
                            d = date.fromisoformat(str(ts).split(" ")[0].split("T")[0])
                        clean_points.append((d, val))
                except Exception:
                    continue
            if clean_points:
                result[name] = clean_points
        if result:
            return result

    # ---- 方式2：Playwright 浏览器自动化（无头） ----
    result = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = _create_context_with_cookies(browser, config.COOKIE_PATH)
        page = context.new_page()

        _inject_network_hooks(page)
        captured = _setup_network_capture(page)

        url = f"https://buff.163.com/goods/{item_id}"
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        sleep_random(1.0, 2.0)

        # ---- 点击"价格走势" ----
        try:
            tab_texts = ['"价格走势"', '"走势"', '"价格趋势"', '"价格图表"']
            for tt in tab_texts:
                try:
                    btn = page.query_selector(f'text={tt}')
                    if not btn:
                        btn = page.query_selector(f'[class*="tab"]:has-text({tt})')
                    if not btn:
                        btn = page.query_selector(f'li:has-text({tt})')
                    if not btn:
                        btn = page.query_selector(f'button:has-text({tt})')
                    if btn:
                        btn.click()
                        sleep_random(1.5, 3.0)
                        page.wait_for_load_state("networkidle", timeout=60000)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # ---- 选择时间范围 ----
        try:
            range_text = _choose_time_range_by_label(time_label)
            range_selectors = [
                f'text="{range_text}"',
                f'button:has-text("{range_text}")',
                f'a:has-text("{range_text}")',
                f'span:has-text("{range_text}")',
                f'label:has-text("{range_text}")',
                f'[class*="range"]:has-text("{range_text}")',
            ]
            for sel in range_selectors:
                range_btn = page.query_selector(sel)
                if range_btn:
                    range_btn.click()
                    sleep_random(1.5, 3.0)
                    page.wait_for_load_state("networkidle", timeout=60000)
                    break
        except Exception:
            pass

        # ---- 方式1：从拦截的网络请求中提取 ----
        for resp in captured["responses"]:
            try:
                extracted = _extract_all_series_from_json(resp["body"])
                if extracted:
                    result = extracted
                    browser.close()
                    return result
            except Exception:
                continue

        # ---- 方式2：ECharts 提取 ----
        echarts_data = _extract_echarts_data(page)
        if echarts_data and isinstance(echarts_data, dict):
            for name, points in echarts_data.items():
                clean_points = []
                for pt in points:
                    try:
                        if isinstance(pt, list) and len(pt) >= 2:
                            ts, val = pt[0], float(pt[1]) if pt[1] is not None else 0
                            if isinstance(ts, (int, float)):
                                pt_date = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts).date()
                            else:
                                pt_date_str = str(ts).split(" ")[0].split("T")[0]
                                pt_date = date.fromisoformat(pt_date_str)
                            clean_points.append((pt_date, val))
                    except Exception:
                        continue
                if clean_points:
                    result[name] = clean_points

        browser.close()

    return result


def _extract_all_series_from_json(obj, depth=0):
    """从 JSON 中提取所有价格曲线数据，返回 {series_name: [(date, price), ...]}。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        # BUFF API 常见结构: {code: "OK", data: {series: [{name, data}, ...]}}
        inner = obj.get("data") or obj
        if isinstance(inner, dict):
            series_list = inner.get("series") or inner.get("price_series") or []
            if series_list and isinstance(series_list, list):
                result = {}
                for s in series_list:
                    if isinstance(s, dict):
                        name = s.get("name") or s.get("label") or "未命名"
                        points = s.get("data") or s.get("prices") or s.get("points") or []
                        if points:
                            clean = []
                            for pt in points:
                                try:
                                    if isinstance(pt, list) and len(pt) >= 2:
                                        ts, val = pt[0], float(pt[1])
                                    elif isinstance(pt, dict):
                                        ts = pt.get("date") or pt.get("x") or pt.get("t")
                                        val = float(pt.get("price") or pt.get("y") or pt.get("v") or 0)
                                        if ts is None:
                                            continue
                                    else:
                                        continue
                                    if isinstance(ts, (int, float)):
                                        d = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts).date()
                                    else:
                                        d = date.fromisoformat(str(ts).split(" ")[0].split("T")[0])
                                    clean.append((d, val))
                                except Exception:
                                    continue
                            if clean:
                                result[name] = clean
                if result:
                    return result
        # 递归搜索
        for key, val in inner.items() if isinstance(inner, dict) else []:
            r = _extract_all_series_from_json(val, depth + 1)
            if r:
                return r
    return None


def get_price_history(item_id: str, start_date: date, end_date: date) -> List[PriceRecord]:
    """获取饰品在指定日期范围内的"在售最低"历史价格。直接调用 BUFF buff/v2 API。"""
    # API 的 days 参数=从今天往回多少天。需覆盖到 start_date（最早的日期）。
    days_needed = (date.today() - start_date).days + 1
    api_days = _api_days_for_range(days_needed)

    # ---- 方式1：直接 API 调用（快速，不弹浏览器） ----
    api_data, api_err = _get_buff_price_history_direct(item_id, game="csgo", days=api_days)
    if api_data:
        records = _parse_buff_api_lines(api_data, start_date, end_date)
        if records:
            return records

    return []


def diagnose_price_extraction(item_id: str, start_date: date, end_date: date) -> dict:
    """逐步诊断价格提取流程，返回每一步的详细结果。
    在同一次浏览器会话中依次执行所有子步骤，记录每个步骤的成功/失败状态和细节。
    返回: {
        "steps": [
            {"name": "...", "ok": True/False, "detail": "...", "data": ...},
            ...
        ],
        "records": [...],
        "screenshot_paths": {...}
    }
    """
    import tempfile, os as _os
    steps = []
    steps.append({"name": "🆕 代码版本: 2026-05-19-v8 (Steam价格+销量提取)", "ok": True,
                  "detail": "修复: 直接API days从today回退计算，覆盖历史目标日期。Playwright降级保底。"})
    span_days = (end_date - start_date).days + 1
    range_text = _choose_time_range(span_days, start_date)
    records = []
    current_price = None
    tmp_dir = tempfile.mkdtemp(prefix="buff_diag_")

    # ---- 步骤0：尝试直接 API 调用（最快路径） ----
    api_days = _api_days_for_range((date.today() - start_date).days + 1)
    api_data, api_error = _get_buff_price_history_direct(item_id, game="csgo", days=api_days)
    if api_data:
        api_records = _parse_buff_api_lines(api_data, start_date, end_date)
        lines_info = ", ".join(f"{l.get('name','?')}({len(l.get('points',[]))}pts)"
                               for l in api_data.get("lines", [])[:6])
        steps.append({"name": "⓪ 直接API调用", "ok": True,
                      "detail": f"API返回成功: days={api_days}, lines=[{lines_info}]\n"
                                f"提取到 {len(api_records)} 条价格记录"})
        if api_records:
            records = api_records
            # 直接返回 — 不需要打开浏览器
            return {
                "steps": steps,
                "records": records,
                "tmp_dir": tmp_dir,
                "item_id": item_id,
                "start_date": start_date,
                "end_date": end_date,
                "range_text": range_text,
            }
    else:
        steps.append({"name": "⓪ 直接API调用", "ok": False,
                      "detail": f"直接 API 调用失败: {api_error}\n降级到 Playwright 浏览器提取"})

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = _create_context_with_cookies(browser, config.COOKIE_PATH)
        page = context.new_page()
        _inject_network_hooks(page)       # 注入 JS 层 XHR/fetch/jQuery 拦截
        captured = _setup_network_capture(page)  # Playwright 层拦截（存活于导航）

        # ---- 步骤1：打开BUFF详情页 ----
        url = f"https://buff.163.com/goods/{item_id}"
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            sleep_random(1.0, 2.0)
            ss1 = _os.path.join(tmp_dir, "01_page_loaded.png")
            page.screenshot(path=ss1)
            steps.append({"name": "① 打开BUFF详情页", "ok": True,
                          "detail": f"URL: {url}", "screenshot": ss1})
        except Exception as e:
            steps.append({"name": "① 打开BUFF详情页", "ok": False,
                          "detail": str(e), "screenshot": None})

        # ---- 步骤2：检查页面初始网络数据 ----
        js_hooked_initial = _collect_hooked_data(page)
        # 特别关注 sell_order 和其他 API 响应
        sell_order_info = ""
        responses = captured["responses"] if isinstance(captured, dict) else captured
        for resp in responses:
            body = resp.get("body")
            if not body or not isinstance(body, dict):
                continue
            url = resp.get("url", "")
            data = body.get("data")
            if "sell_order" in url and data:
                sell_order_info += f"\nsell_order data keys: {list(data.keys())[:10]}"
                if isinstance(data, dict):
                    goods_infos = data.get("goods_infos", {})
                    if isinstance(goods_infos, dict):
                        sell_order_info += f"\ngoods_infos keys: {list(goods_infos.keys())[:15]}"
                        # 检查是否包含价格历史相关字段
                        price_keys = [k for k in goods_infos.keys() if any(
                            kw in str(k).lower() for kw in ('price', 'history', 'trend', 'chart'))]
                        if price_keys:
                            sell_order_info += f"\n⚠️ 含价格相关字段: {price_keys}"
            if "price_history" in url:
                sell_order_info += f"\nprice_history API: keys={list(body.keys())[:10]}"
                if data:
                    sell_order_info += f", data keys={list(data.keys())[:10] if isinstance(data, dict) else type(data)}"
            if "goods_tab" in url and data:
                sell_order_info += f"\ngoods_tab data type={type(data)}, len={len(data) if isinstance(data, list) else 'N/A'}"
        if not sell_order_info:
            sell_order_info = " (未发现 sell_order 或 price_history 响应)"

        if captured["responses"]:
            req_info = ""
            req_urls = captured["request_urls"]
            if req_urls:
                api_urls = [r for r in req_urls if '/api/' in r.get('url','')]
                req_info = f"\n其中 API 请求: {len(api_urls)} 个\n"
                for r in api_urls[:10]:
                    req_info += f"  [{r['method']}] {r['url'][:150]}\n"
            detail = (f"页面加载后 Playwright 拦截 {len(captured['responses'])} 个响应"
                      f"{req_info}"
                      f"{sell_order_info}\n"
                      f"JS 层 hook 捕获: {len(js_hooked_initial)} 条")
            steps.append({"name": "② 网络拦截（页面加载后）", "ok": True,
                          "detail": detail, "data": captured["responses"][:],
                          "js_hooked": js_hooked_initial})
        else:
            steps.append({"name": "② 网络拦截（页面加载后）", "ok": False,
                          "detail": "未拦截到任何响应" + sell_order_info, "data": [],
                          "js_hooked": js_hooked_initial})

        # ---- 步骤3：获取当前价格 ----
        try:
            price_el = page.query_selector(
                "[class*='price'], .cost, strong, .wear-value, [class*='Price'], [class*='amount']")
            if price_el:
                current_price = _extract_price(price_el.inner_text())
                steps.append({"name": "③ 提取当前页面价格", "ok": True,
                              "detail": f"当前价格: ¥{current_price}"})
            else:
                steps.append({"name": "③ 提取当前页面价格", "ok": False,
                              "detail": "未找到价格元素"})
        except Exception as e:
            steps.append({"name": "③ 提取当前页面价格", "ok": False,
                          "detail": str(e)})

        # ---- 步骤4：查找并点击"价格走势"标签 ----
        tab_found = False
        tab_clicked_text = ""
        ss2 = None
        tab_selectors = [
            'text="价格走势"', 'text="走势"', 'text="价格趋势"',
            'a:has-text("价格走势")', 'a:has-text("走势")',
            '[class*="tab"]:has-text("走势")', '[class*="tab"]:has-text("价格")',
            'li:has-text("走势")', 'li:has-text("价格")',
            'button:has-text("走势")', 'button:has-text("价格")',
        ]
        for sel in tab_selectors:
            try:
                btn = page.query_selector(sel)
                if btn:
                    tab_clicked_text = btn.inner_text()[:40]
                    btn.click()
                    sleep_random(1.5, 3.0)
                    page.wait_for_load_state("networkidle", timeout=60000)
                    # 等待图表区域出现
                    try:
                        page.wait_for_selector("#price-history-days", timeout=10000)
                    except Exception:
                        pass
                    ss2 = _os.path.join(tmp_dir, "02_after_tab_click.png")
                    page.screenshot(path=ss2)
                    tab_found = True
                    break
            except Exception:
                continue

        if tab_found:
            chart_loaded = page.query_selector("#price-history-days") is not None
            steps.append({"name": f"④ 点击'价格走势'标签", "ok": True,
                          "detail": f"匹配选择器: {sel}, 标签文本: '{tab_clicked_text}', "
                                    f"图表加载: {'✅' if chart_loaded else '❌ #price-history-days 未出现'}",
                          "screenshot": ss2})
        else:
            steps.append({"name": "④ 点击'价格走势'标签", "ok": False,
                          "detail": f"尝试了 {len(tab_selectors)} 个选择器，均未匹配到标签元素",
                          "screenshot": None})

        # ---- 步骤5：标签点击后检查网络数据 ----
        new_count = len(captured["responses"])
        js_hooked_after_tab = _collect_hooked_data(page)
        if tab_found:
            detail = (f"当前共拦截 {new_count} 个 Playwright 响应"
                      f"\nJS 层 hook 捕获: {len(js_hooked_after_tab)} 条")
            steps.append({"name": "⑤ 网络拦截（标签点击后）", "ok": True,
                          "detail": detail,
                          "js_hooked": js_hooked_after_tab})
        else:
            steps.append({"name": "⑤ 网络拦截（标签点击后）", "ok": False,
                          "detail": "上一步标签未找到，跳过此步",
                          "js_hooked": []})

        # ---- 步骤6：选择时间范围 ----
        range_found = False
        ss3 = None
        if tab_found:
            range_found = _select_time_range(page, range_text)
            if range_found:
                ss3 = _os.path.join(tmp_dir, "03_after_range.png")
                page.screenshot(path=ss3)

        if range_found:
            steps.append({"name": f"⑥ 选择时间范围({range_text})", "ok": True,
                          "detail": "通过 #price-history-days 下拉框选择成功",
                          "screenshot": ss3})
        elif tab_found:
            # 收集调试信息：页面上有什么相关元素
            debug_info = ""
            dd = page.query_selector("#price-history-days")
            ws = page.query_selector(".w-Select")
            canvas_el = page.query_selector("canvas")
            lis = page.query_selector_all("li")
            range_lis = [li for li in lis if li.inner_text().strip() in ("7天","1个月","3个月","6个月","1年","最近2年") and li.is_visible()]
            debug_info = (f"#price-history-days: {'存在' if dd else '不存在'}, "
                         f".w-Select: {'存在' if ws else '不存在'}, "
                         f"canvas: {'存在' if canvas_el else '不存在'}, "
                         f"可见范围li: {len(range_lis)}个")
            steps.append({"name": f"⑥ 选择时间范围({range_text})", "ok": False,
                          "detail": debug_info,
                          "screenshot": None})
        else:
            steps.append({"name": f"⑥ 选择时间范围({range_text})", "ok": False,
                          "detail": "因标签未找到，跳过此步", "screenshot": None})

        # ---- 步骤7：网络拦截最终检查 ----
        final_count = len(captured["responses"])
        all_hooked = _collect_hooked_data(page)
        responses = captured["responses"] if isinstance(captured, dict) else captured
        detail = f"最终 Playwright 拦截 {final_count} 个响应，JS hook 捕获 {len(all_hooked)} 条"
        # 尝试从拦截数据中提取价格
        net_records = _extract_from_captured(captured, start_date, end_date)
        if net_records:
            detail += f"\n✅ 成功从网络响应中提取到 {len(net_records)} 条价格记录！"
        else:
            detail += "\n⚠️ 通用提取未找到价格数据，尝试直接匹配 /buff/v2 响应..."
            for resp in responses:
                if "buff/v2" in resp.get("url", ""):
                    body = resp.get("body")
                    if body and isinstance(body, dict):
                        lines = body.get("data", {}).get("lines", []) if isinstance(body.get("data"), dict) else body.get("lines", [])
                        for line in lines:
                            pts = line.get("points", [])
                            if pts:
                                parsed = _parse_points(pts, start_date, end_date)
                                if parsed:
                                    net_records = parsed
                                    detail += f"\n✅ 从 /buff/v2 响应中提取到 {len(net_records)} 条 ({line.get('name', '?')})"
                                    break
                    if not net_records:
                        detail += f"\n⚠️ 找到 /buff/v2 响应但无法解析: body_type={type(body).__name__}"
                    break
            if not net_records:
                detail += "\n❌ 未找到 /buff/v2 响应"
        # 列出 JS hook 捕获的所有 URL
        if all_hooked:
            detail += "\n\nJS hook 捕获的请求 URL："
            seen = set()
            for entry in all_hooked:
                url = entry.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    detail += f"\n  [{entry.get('type','?')}] {entry.get('method','?')} {url[:150]}"
        steps.append({"name": "⑦ 网络拦截（最终汇总）", "ok": bool(net_records),
                      "detail": detail, "records_from_network": len(net_records),
                      "all_hooked": all_hooked})

        # ---- 收集最终价格记录 ----
        if net_records:
            records = net_records

        # Fallback
        if not records and current_price and current_price > 0:
            today = date.today()
            if start_date <= today <= end_date:
                records.append(PriceRecord(date=today, price=current_price))

        browser.close()

    return {
        "steps": steps,
        "records": records,
        "tmp_dir": tmp_dir,
        "item_id": item_id,
        "start_date": start_date,
        "end_date": end_date,
        "range_text": range_text,
    }
