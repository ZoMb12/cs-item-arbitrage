# CS:GO 饰品套利项目 — 交接文档

## 项目概述

Streamlit 网页应用，从 BUFF.163.com + Steam Community Market 抓取 CS:GO 饰品数据，
进行4步流水线处理，最终找出 BUFF 价格高于 Steam 价格的套利机会。

**最新提交:** `631d6cc` (v11) — Step 3 batch优化 + 错误日志增强 + 多品类去重  
**未提交改动:** Step 3 失败重试按钮 + Step 2 进度条

---

## 架构

```
app.py                  # Streamlit 主入口，UI + 4步流程编排
core/
  buff_scraper.py       # BUFF 抓取（商品列表、价格历史、XHR/fetch hook）
  steam_scraper.py      # Steam 抓取（SSR buckets、pricehistory API）
  filters.py            # 价格稳定性筛选
data/
  database.py           # SQLite 持久化（runs + run_items）
  models.py             # ItemSnapshot, PriceRecord dataclass
utils/
  helpers.py            # sleep_random()
config.py               # 品类配置、代理配置、默认参数
```

---

## 4步流水线

| 步骤 | 函数 | 说明 |
|------|------|------|
| Step 1 | `_execute_step1()` | Playwright 打开 BUFF 列表页 → hook 商品 API → 多品类抓取 + 去重 |
| Step 2 | `_execute_step2()` | 对每件饰品调 BUFF 价格历史 API → 计算波动率 → 筛稳定品 |
| Step 3 | `_execute_step3()` | 按皮肤名分组 → 每组开 1 次 BUFF+Steam 页面 → 匹配变体 → 调 pricehistory |
| Step 4 | `_execute_step4()` | BUFF价格[D] vs Steam价格[D-7]×汇率 → 差价>0 标记为目标 |

---

## 本次未提交改动

### 1. Step 3 失败饰品重试按钮（app.py + steam_scraper.py）

**动机:** Step 3 批处理中有些饰品因网络波动获取失败，需要独立的"重试"入口。

**改动:**
- `steam_scraper.py:752` — 新增 `retry_steam_failed_items(failed_items)` 函数
  - 入参: `[{"item_id", "buff_item_name", "target_dates", "base_skin_name"}]`
  - 内部逻辑: 按 base_skin_name 重新分组 → 对每组调 `get_steam_market_data_batch()`
  - 返回: `{item_id: steam_data_dict}`（与 batch 函数相同格式）
- `app.py:_execute_step3()` — 失败饰品自动保存到 `st.session_state.failed_step3_items`
- `app.py:997~1055` — Step 3 UI 中新增"🔄 重试失败饰品"展开区域
  - 每个失败饰品有独立"重试"按钮（调 `get_steam_market_data` 单次抓取）
  - 底部"重试全部失败饰品"按钮（调 `retry_steam_failed_items` 批量重试）
  - 重试成功后自动移除该饰品从失败列表，用 `st.rerun()` 刷新

**注意:** 这两个改动与 v11 commit 提交的改动是独立的，需要 commit 后推送到 GitHub。

### 2. Step 2 进度条（app.py）

**改动:**
- `app.py:_execute_step2()` — 循环开始前创建 `st.progress()`，每条处理完更新进度，完成后 `progress_bar.empty()`

---

## 已提交改动（v11, `631d6cc`）

- Step 3 批处理优化（按皮肤名分组，同组共用一次 Steam 页面）
- 结构化错误日志（`_last_error_context`），UI 中可展开查看
- Clash 代理配置 + 环境变量，修复 Steam GFW 阻断
- Playwright UA/Viewport/zh-CN locale 防检测
- 超时提升（BUFF 60s, Steam 90s, cc=us 45s）
- 2 次重试机制（间隔 5s）
- BUFF 页面状态检查（已下架/404 检测）
- `get_items_on_date` 多品类按 `item_id` 去重

---

## 未完成项

优先级从高到低:

1. **Steam 抓取可靠性** — 核心痛点，GFW 间歇性阻断，现有手段（代理+重试+检测）缓解但未根治
   - 失败时自动保存 Steam 页面截图到 `storage/`
   - 多代理轮换 / 代理健康检测

2. **实时汇率自动获取** — `conversion_rate` 现为手动输入（默认 5.0），可接免费汇率 API

3. **数据导出** — CSV/Excel 导出按钮

4. **运行历史记录页面** — `database.py` 完整保存了每次 run 的所有数据，但 UI 无历史回顾页面

5. **Steam cookie 过期体验** — 过期后只报错 login_redirect，不会自动弹窗提示重新登录

6. **批处理代表作下架时遍历组内其他变体** — `get_steam_market_data_batch` 中代表作 BUFF 页面不存在时整组跳过

7. **诊断截图存于 `storage/` 而非临时目录** — 截图现在在 `tempfile.mkdtemp`

---

## 数据流

```
BUFF API (商品列表) ─→ Step 1: raw_items → filtered_items
                              ↓
BUFF API (价格历史) ─→ Step 2: 波动率筛选 → stable_items
                              ↓
Playwright 浏览器   ─→ Step 3: 按皮肤名分组 → 每批 1×BUFF + 1×Steam
                              ↓
                     Step 4: 按 [Date] 配对 → 套利结果
                              ↓
                     SQLite (run + run_items)
                              ↓
                     Streamlit 展示
```

## 时间轴配对规则

- BUFF 价格日期 = D
- Steam 价格日期 = D - 7（`item.price_history 中每条记录日期 - 7天`）
- 套利条件: `BUFF[D] > Steam[D-7] × conversion_rate`
- 判定: 均价差 > 0 → 🎯 目标

---

## 关键函数入口

| 函数 | 文件:行号 |
|------|----------|
| 一键获取流程 | `app.py:602-725` |
| `_execute_step1` | `app.py:184` |
| `_execute_step2` | `app.py:201` |
| `_execute_step3` | `app.py:241` |
| `_execute_step4` | `app.py:313` |
| `get_items_on_date` | `buff_scraper.py:513` |
| `get_price_history` | `buff_scraper.py` |
| `get_steam_market_data_batch` | `steam_scraper.py:546` |
| `retry_steam_failed_items` **(新)** | `steam_scraper.py:752` |
| `group_by_skin_name` | `steam_scraper.py:240` |
| `diagnose_steam_extraction` | `steam_scraper.py:959` |
