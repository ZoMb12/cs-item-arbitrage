# CS:GO 饰品套利项目 — 完整交接文档

## 项目概述

Streamlit 网页应用，从 BUFF.163.com + Steam Community Market 抓取 CS:GO 饰品数据，
进行 4 步流水线处理，最终找出 BUFF 价格高于 Steam 价格的套利机会。

**版本:** 2026-05-26-v10  
**架构:** 一键获取 + 分步执行双层设计

---

## 目录结构

```
cs-test/
├── app.py                  # Streamlit 主入口，UI + 4 步流程编排（~1500 行）
├── config.py               # 全局配置常量
├── requirements.txt        # Python 依赖
├── core/
│   ├── buff_scraper.py     # BUFF.163.com 数据抓取（~1290 行）
│   └── steam_scraper.py    # Steam Market 数据抓取（~1240 行）
├── data/
│   ├── models.py           # ItemSnapshot / PriceRecord 数据类
│   └── database.py         # SQLite 持久层
├── utils/
│   └── helpers.py          # sleep_random() 工具
├── scripts/
│   ├── start.bat           # Windows 启动脚本
│   └── start.sh            # Linux/Mac 启动脚本
└── storage/                # 运行时生成（gitignore）
    ├── buff_arbitrage.db   # SQLite 数据库
    ├── buff_cookies.json   # BUFF 登录 Cookie
    └── steam_cookies.json  # Steam 登录 Cookie
```

---

## 启动方式

```bash
streamlit run app.py --server.port 8522
```

依赖安装：
```bash
pip install -r requirements.txt
playwright install chromium
```

首次使用需在侧边栏分别登录 BUFF 和 Steam（各打开一次浏览器手动登录，Cookie 自动保存到 `storage/`）。

---

## 4 步流水线

### Step 1 — BUFF 初步筛选 (`_execute_step1`)

**入口:** `app.py:221` | **核心函数:** `buff_scraper.get_items_on_date()` (line 513)

```
功能: Playwright 无头浏览器打开 BUFF 列表页 → 翻页提取饰品信息
```

**详细流程:**

1. 支持多品类：品类列表（如 `["hands", "rifle"]`）各品类独立翻页，目标数量均分
2. 每个品类循环翻页，从 `li.selling` DOM 元素提取：名称、价格、在售数量、商品 ID
3. 筛选条件：`volume > min_volume(100)` 且 `buff_price > min_price(20)`
4. 多品类结果按 `item_id` 去重（同一饰品可能出现在多个品类下）
5. 结果存入 `st.session_state.raw_items` 和 `st.session_state.filtered_items`
6. 数据库：`db.save_step1()` 写入 run_items 表（filtered 的 step_reached=2，纯 raw 的为 1）

**数据降级:** 无降级，必须 Playwright 浏览器

### Step 2 — 价格稳定性筛选 (`_execute_step2`)

**入口:** `app.py:238`

```
功能: 对每件饰品调 BUFF API 获取价格历史 → 计算波动率 → 筛除波动大的
```

**详细流程:**

1. 遍历 `filtered_items`，对每个饰品调 `buff_scraper.get_price_history()`
2. `get_price_history` 内部：
   - **方式 1（优先）:** 直接 `requests.get()` 调 `buff.163.com/api/market/goods/price_history/buff/v2`（需本地 Cookie），指定 days 参数覆盖到 start_date
   - **方式 2（降级）:** Playwright 浏览器远程打开 BUFF 详情页 → 点击"价格走势"标签 → 网络拦截 → ECharts 提取
3. 计算波动率：`(max_price - min_price) / avg_price`
4. 筛选：`is_price_stable(history, threshold)` → 波动率 ≤ threshold 则通过
5. 通过饰品的 `price_history` 保存到 `item.price_history` 列表
6. Debug 信息注入到 item 属性：`_debug_history_len`, `_debug_min_price`, `_debug_max_price`, `_debug_volatility`, `_debug_fail_reason`
7. 结果存入 `st.session_state.stable_items`
8. 带进度条显示：`st.progress((idx)/total, text=f"...")`

**注意:** 无价格历史数据的饰品直接判为不稳定（`is_price_stable` 返回 False）

### Step 3 — Steam 市场数据获取 (`_execute_step3`)

**入口:** `app.py:288` | **核心函数:** `steam_scraper.get_steam_market_data_batch()` (line 553)

```
功能: 按皮肤名分组 → 每组打开 1 次 Steam 页面 → 匹配变体 → 调 pricehistory API
```

**详细流程:**

1. **分组:** `steam_scraper.group_by_skin_name(stable)` — 按基础皮肤名分组（去磨损后缀和 StatTrak 前缀），同组饰品共用一次 Steam 页面
2. **批量获取:** 每组调一次 `get_steam_market_data_batch()`：
   - **① 打开 BUFF 详情页:** Playwright → `buff.163.com/goods/{item_id}`，检查页面非 404
   - **② 查找 Steam 按钮:** `_find_steam_market_button()` — 7 种选择器依次匹配
   - **③ 跳转 Steam:** `_click_and_get_steam_page()` — **三级降级**:
     - 方式 1: 从按钮 href 直接导航到 Steam（最可靠，绕过验证码遮罩）
     - 方式 2: dispatchEvent("click") 打开新标签页
     - 方式 3: 当前标签页降级（BUFF 直接跳转）
   - **④ 强制美元:** URL 追加 `?cc=us` 参数确保美元计价
   - **⑤ SSR 数据提取:** `_extract_ssr_buckets()` — 遍历 `window.SSR.loaderData` 数组，找含 `buckets` 的条目
   - **⑥ 变体匹配:** `_match_bucket()` — 从 buckets 中找到 wear 相同 + StatTrak 状态相同的 bucket；失败时 fallback 到 `initialFallbackBucketID`
   - **⑦ 品质/磨损过滤:** 点击 Steam 页面上的普通/StatTrak™ 按钮和磨损等级标签
   - **⑧ Price History API:** 在 Steam 页面内用 `fetch()` 调 `steamcommunity.com/market/pricehistory/?appid=730&market_hash_name=...`，解析 `"Jul 15 2025 01: +0"` 格式日期
3. **校验结果:** `if data and data.get("steam_price_history"):` → 必须有实际价格记录才算成功（2026-05-28 修复：以前 `if data:` 对空 dict 也判成功）
4. 失败饰品记录到 `st.session_state.failed_step3_items`，可在 UI 中逐个或批量重试
5. 结果存入 `st.session_state.steam_data`（dict，key=item_id）

**批处理优化:** 同组所有变体共享 1 次 BUFF 页面 + 1 次 Steam 页面，逐个匹配 bucket + 调 pricehistory API，显著减少浏览器开销

**错误追踪:** `_batch_errors = {}` 按 item_id 独立记录每个变体的失败原因，`get_last_steam_error(item_id=...)` 支持按 item_id 查询

### Step 4 — 套利对比 (`_execute_step4`)

**入口:** `app.py:379`

```
功能: 对每个有 Steam 数据的饰品 → 配对 BUFF 日期 vs Steam 日期 → 计算差价
```

**核心配对规则:**

- BUFF 价格日期 = D
- Steam 价格日期 = D - 7（`item.price_history 中每条记录日期 - 7天`）
- 汇率转换：`steam_price_cny = steam_price_usd × conversion_rate`
- 套利条件: `average(BUFF[D]) > average(Steam[D-7] × rate)`
- 判定: `avg_diff > 0` → `is_target = True`

**详细流程:**

1. 遍历 `stable_items`，从 `steam_data` 取出对应数据
2. 对每组 BUFF 和 Steam 价格历史，按日期配对
3. 计算每个日期节点的差价和是否为目标
4. 计算均价差、目标节点数
5. 结果存入 `st.session_state.arbitrage_results`
6. **汇率固化:** 执行时汇率写入 `st.session_state._run_conversion_rate`，结果页使用固化汇率而非当前 widget 值
7. 数据库：`db.save_step4()` + `db.finish_run()`

---

## 数据模型

### ItemSnapshot (data/models.py:14)

```python
@dataclass
class ItemSnapshot:
    item_id: str           # BUFF 商品 ID
    name: str              # 完整饰品名称（含磨损/StatTrak）
    buff_price: float      # BUFF 当前在售最低价
    volume: int            # 在售数量
    turnover: float        # 自动计算 = buff_price * volume
    price_history: List[PriceRecord]      # Step 2 填充
    steam_url: Optional[str]              # Step 3 填充
    steam_price: Optional[float]          # Step 3 填充
    steam_sold_count: int                 # Step 3 填充
    steam_price_history: List[PriceRecord] # Step 3 填充
```

### PriceRecord (data/models.py:7)

```python
@dataclass
class PriceRecord:
    date: date
    price: float
    volume: int = 0
```

---

## Session State 架构

所有运行状态存储在 `st.session_state`，关键 key 及含义：

| Key | 类型 | 含义 |
|-----|------|------|
| `stage1_done` | bool | Step 1 是否完成 |
| `stage2_done` | bool | Step 2 是否完成 |
| `stage3_done` | bool | Step 3 是否完成 |
| `stage4_done` | bool | Step 4 是否完成 |
| `raw_items` | List[ItemSnapshot] | BUFF 原始获取列表 |
| `filtered_items` | List[ItemSnapshot] | 初步过滤后列表 |
| `stable_items` | List[ItemSnapshot] | 价格稳定饰品 |
| `steam_data` | dict[item_id → dict] | Steam 市场数据 |
| `arbitrage_results` | dict[item_id → dict] | 套利对比结果 |
| `one_click_mode` | "running"\|"done"\|None | 一键获取模式状态 |
| `current_run_id` | int\|None | 当前运行数据库 ID |
| `error_log` | list[dict] | 错误日志 |
| `failed_step3_items` | list[dict] | Step 3 失败饰品（可重试） |
| `_param_snapshot` | dict\|None | 参数快照（分步模式变更检测） |
| `_run_conversion_rate` | float\|None | 固化汇率 |

---

## UI 结构 (app.py)

### 侧边栏 (line 39-140)

```
┌─ 参数配置 ─────────────────┐
│  目标日期                    │
│  价格稳定考察天数 (24)        │
│  波动阈值 (5%)               │
│  汇率 (USD→CNY)              │
│  品类选择 (多选)              │
│  目标数量 (200)              │
│  最低价格 (¥20)              │
│  最低在售 (100)              │
├─ 登录区 ────────────────────┤
│  [登录 BUFF] / [查看 BUFF]   │
│  [登录 Steam] / [查看 Steam] │
├─ 参数变更检测 ───────────────┤
│  分步模式下自动比对快照        │
└─────────────────────────────┘
```

### 主 Tab 1: 选品流程

**模式 A — 一键获取完成页 (`one_click_mode == "done"`)**
```
┌─ 成功提示 ────────────────────┐
│  X → X → X → X               │
├─ 4 列状态卡 ──────────────────┤
│  Step 1  Step 2  Step 3  Step 4 │
│  [重执行] [重执行] [重执行] [重执行]│
├─ 失败饰品重试（如有）──────────┤
│  饰品1 [重试]  饰品2 [重试]    │
│  [重试全部]                    │
├─ 🎯 目标饰品列表（含 Steam 链接）┤
├─ 主结果表格（含 Steam 链接列）───┤
├─ 📋 查看执行细则（折叠）────────┤
│  各步详情 + 图表               │
├─ ⚠️ 错误日志（展开）────────────┤
│  每步错误 + 展开上下文          │
│  [📋 导出诊断信息]              │
└─────────────────────────────────┘
```

**模式 B — 未完成/未开始**
```
┌─ 🚀 [开始一键获取] ──────────┐
├─ ⚙️ 分步执行（折叠）──────────┤
│  4 步独立执行/重新执行按钮     │
│  Step 3 失败饰品重试           │
└───────────────────────────────┘
```

### 主 Tab 2: 价格走势查询
单饰品价格历史查询工具，支持时间范围选择。

---

## 数据库结构 (data/database.py)

### runs 表
| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 |
| started_at | TEXT | 开始时间 |
| finished_at | TEXT | 完成时间 |
| target_date | TEXT | 目标日期 ISO |
| stable_days | INTEGER | 考察天数 |
| volatility_threshold | REAL | 波动阈值 |
| conversion_rate | REAL | 汇率 |
| max_buff_pages | INTEGER | 目标数量 |
| status | TEXT | running/completed |
| raw_count / filtered_count / stable_count / steam_count / target_count | INTEGER | 各步统计 |

### run_items 表
| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 |
| run_id | INTEGER FK → runs.id | 关联运行 |
| item_id | TEXT | 饰品 ID |
| name | TEXT | 饰品名 |
| buff_price / volume / turnover | ... | BUFF 数据 |
| step_reached | INTEGER | 1~4，标记处理到哪步 |
| steam_url / steam_price / steam_sold_count | ... | Steam 数据 |
| buff_price_history | TEXT(JSON) | 价格历史序列化 |
| steam_price_history | TEXT(JSON) | Steam 历史序列化 |
| avg_buff_price / avg_steam_usd / avg_steam_cny / avg_diff | REAL | 套利计算 |
| is_target | INTEGER | 0/1 |
| target_count | INTEGER | 命中节点数 |
| date_pairs | TEXT(JSON) | 日期配对详情 |
| volatility | REAL | 波动率 |
| fail_reason | TEXT | 失败原因 |
| debug_info | TEXT(JSON) | 调试信息 |

**注意:** `save_step3` 和 `save_step4` 都用 `step_reached=4`，通过 `runs.target_count IS NOT NULL` 间接判断完成度。

---

## 降级策略汇总

### BUFF 价格历史 (buff_scraper.py)
1. **直接 API** — `requests.get()` 带 Cookie，最快
2. **Playwright 网络拦截** — 注入 JS XHR/fetch/jQuery 三层钩子 + Playwright 层监听，递归搜索 JSON 中的价格数据
3. **ECharts 提取** — JS 注入获取 ECharts 实例的所有 series 数据

### Steam 按钮点击 (steam_scraper.py)
1. **href 直接导航** — 从按钮元素取 href 属性，`context.new_page().goto(href)`
2. **dispatchEvent 点击** — 绕过验证码遮罩层
3. **当前标签页降级** — BUFF 页面直接跳转 Steam

### Steam 数据提取
1. **SSR loaderData** — 遍历 `window.SSR.loaderData` 找 `buckets`
2. 无法 SSR → 诊断函数记录上下文供分析

---

## 关键 Bug 修复记录

### 2026-05-28 — Step 3 空数据误判

**问题:** `_execute_step3` 用 `if data:` 判断成功，但 `get_steam_market_data_batch()` 返回的 dict 即使含空 `steam_price_history` 也是 truthy，导致 pricehistory API 无数据时仍计为成功，不进入 `failed_step3_items`，不显示重试按钮。

**修复:** `if data:` → `if data and data.get("steam_price_history"):`（`app.py:328`）

**相关记录:** 错误日志中 Step 4 会出现"无Steam价格历史"，但 Step 3 无错误

### 已知远期风险
1. **Steam SSR 数据索引** — `_extract_ssr_buckets` 原硬编码 `loaderData[3]`，已改为遍历所有索引 + try/except 容错；Steam 前端 SSR 结构调整仍可能导致 buckets 格式变化
2. **Steam 抓取可靠性** — GFW 间歇性阻断，现有手段（代理 7890 + 重试 + 检测）缓解但未根治
3. **变体匹配偶发失败** — 某些饰品 SSR `localized_name` 可能为中文，导致英文磨损关键词匹配失败（可通过错误日志的 `available_buckets` 字段确认）

---

## 关键函数索引

| 函数 | 文件:行号 | 说明 |
|------|----------|------|
| `_execute_step1` | `app.py:221` | BUFF 列表抓取 + 初步过滤 |
| `_execute_step2` | `app.py:238` | 价格波动率计算 + 稳定性筛选 |
| `_execute_step3` | `app.py:288` | Steam 数据批量获取 |
| `_execute_step4` | `app.py:379` | 套利对比计算 |
| `_show_error_log` | `app.py:176` | 错误日志 UI + 诊断导出 |
| `_snapshot_params` | `app.py:207` | 参数快照（变更检测） |
| `_clear_downstream_steps` | `app.py:159` | 清空下游步骤结果 |
| `_log_error` | `app.py:143` | 格式化的错误记录 |
| `get_items_on_date` | `buff_scraper.py:513` | 多品类 BUFF 列表抓取 |
| `get_price_history` | `buff_scraper.py:990` | BUFF 价格历史（直接 API → 降级） |
| `get_full_price_history` | `buff_scraper.py:813` | 完整多曲线价格历史 |
| `diagnose_price_extraction` | `buff_scraper.py:1006` | 7 步逐步诊断 BUFF 价格提取 |
| `get_steam_market_data` | `steam_scraper.py:291` | 单饰品 Steam 数据获取 |
| `get_steam_market_data_batch` | `steam_scraper.py:553` | 批量 Steam 数据获取（共享页面） |
| `retry_steam_failed_items` | `steam_scraper.py:763` | 失败饰品重新分组批处理 |
| `group_by_skin_name` | `steam_scraper.py:241` | 按基础皮肤名分组 |
| `_match_bucket` | `steam_scraper.py:164` | 变体匹配逻辑（wear + StatTrak） |
| `_fetch_price_history` | `steam_scraper.py:882` | Steam pricehistory API 调用 |
| `diagnose_steam_extraction` | `steam_scraper.py:1004` | 10 步逐步诊断 Steam 提取 |
| `save_step1/2/3/4` | `database.py:120-234` | 4 步数据库持久化 |
| `create_run` / `finish_run` | `database.py:90-113` | 运行生命周期 |

---

## 代理配置 (config.py:27-29)

```python
PROXY_SERVER = "http://127.0.0.1:7890"       # Clash/V2Ray 本地代理
PROXY_BYPASS = "buff.163.com,.163.com,.qq.com,.aliyuncs.com,.cn"  # 绕过国内域名
```

Steam 抓取需代理翻墙；BUFF 抓取不走代理（国内直连）。
Playwright 浏览器创建时传入 `proxy=config.PROXY_CONFIG`，同时通过环境变量 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY` 设置。

---

## 定制化改造指南

### 新增数据源
- 在 `core/` 下新建 scraper，遵循 `requests → Playwright` 降级模式
- 在 `data/models.py` 扩展现有 dataclass 或新增
- 在 `database.py` 扩展 run_items 表或新建表
- 在 `app.py` 新增 Step 或在现有 Step 中插入

### 修改匹配逻辑
- 变体匹配 → 修改 `steam_scraper.py:_match_bucket()`
- 价格配对 → 修改 `app.py:_execute_step4()` 中的时间偏移
- 稳定性计算 → 修改 `filters.py:is_price_stable()`

### 新增 UI 功能
- 遵循 Streamlit session state 模式，新功能在 `defaults` dict 中注册初始值
- 错误日志遵循 `_log_error(step, item_id, name, error, context)` 接口
- 结果展示使用固化汇率 `_run_conversion_rate` 而非 widget 值
