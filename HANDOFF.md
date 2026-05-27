# CS:GO 饰品套利项目 — 交接文档

## 项目概述

Streamlit 网页应用，从 BUFF.163.com + Steam Community Market 抓取 CS:GO 饰品数据，
进行4步流水线处理，最终找出 BUFF 价格高于 Steam 价格的套利机会。

**最新提交:** `6ea1aab` — 批处理错误逐饰品追踪 + 结果展示汇率固化  
**上提交:** `92dffad` — 数据导出Excel + finish_run补调 + Step2进度条修复 + 分步模式参数变更检测

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

## 版本历史

| 提交 | 说明 |
|------|------|
| `6ea1aab` | 批处理错误逐饰品追踪 + 结果展示汇率固化 |
| `92dffad` | 数据导出Excel + finish_run补调 + Step2进度条修复 + 分步模式参数变更检测 |
| `bf6fe79` | Step 2 进度条 + Step 3 失败重试按钮 |
| `631d6cc` | Step 3 batch优化 + 错误日志增强 + 多品类去重 |
| `b3f9bd5` | v10版初同步 |

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

## 重要功能说明

### 分步模式参数变更检测（`app.py`）
- `_snapshot_params()` 在步骤执行开始时保存所有 sidebar 参数快照到 `st.session_state._param_snapshot`
- 分步模式下，sidebar 底部自动比对当前参数与执行时快照，检测到变更时 `st.warning()` 提示影响范围
- 不影响一键获取模式

### 批处理错误追踪（`steam_scraper.py`）
- `_batch_errors = {}` 按 `item_id` 记录每组失败变体的独立错误上下文
- `get_last_steam_error(item_id=...)` 和 `get_last_steam_error_context(item_id=...)` 支持按 item_id 查询
- 解决批量处理时只保留最后一次失败信息的问题

### 结果展示汇率固化（`app.py`）
- Step 4 执行时将汇率存入 `st.session_state._run_conversion_rate`
- 一键完成展示区使用固化汇率而非当前 widget 值，避免修改后图表/描述不一致

### Step 3 失败饰品重试
- 失败饰品自动保存到 `st.session_state.failed_step3_items`
- UI 中支持"单个重试"（调 `get_steam_market_data`）和"重试全部"（调 `retry_steam_failed_items`）

---

## 关键函数入口

| 函数 | 文件:行号 |
|------|----------|
| 一键获取流程 | `app.py:602-725` |
| `_execute_step1` | `app.py:185` |
| `_execute_step2` | `app.py:202` |
| `_execute_step3` | `app.py:245` |
| `_execute_step4` | `app.py:364` |
| `_snapshot_params` | `app.py:185` |
| `get_items_on_date` | `buff_scraper.py:513` |
| `get_price_history` | `buff_scraper.py` |
| `get_steam_market_data_batch` | `steam_scraper.py:546` |
| `retry_steam_failed_items` | `steam_scraper.py:752` |
| `group_by_skin_name` | `steam_scraper.py:240` |
| `get_last_steam_error` | `steam_scraper.py:256` |
| `get_last_steam_error_context` | `steam_scraper.py:263` |
| `diagnose_steam_extraction` | `steam_scraper.py:991` |

---

## 已知远期风险

1. **Steam SSR 数据索引** — `_extract_ssr_buckets` 中硬编码 `loaderData[3]`，Steam 前端 SSR 结构调整可能导致 buckets 出现在不同索引。有遍历 + try/except 基本容错，标记为远期风险。
2. **Steam 抓取可靠性** — GFW 间歇性阻断，现有手段（代理+重试+检测）缓解但未根治。
3. **`save_step3`/`save_step4` 都用 `step_reached=4`** — 数据库无法区分"Steam 数据完成"和"全部 4 步完成"，但可通过 `runs.target_count IS NOT NULL` 间接判断。
