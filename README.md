# CS饰品搬砖选品助手

基于 Python + Streamlit 的 CS:GO/CS2 饰品搬砖选品工具（BUFF → Steam 套利）。

## 功能概述

采用 **4 阶段流水线**：

1. **BUFF 初步筛选** — 从网易 BUFF 按品类抓取饰品列表，过滤在售数量和价格
2. **价格稳定性分析** — 调用 BUFF API 获取历史价格，筛掉波动超过阈值的
3. **Steam 市场数据** — Playwright 浏览器自动化跳转 Steam，匹配变体（磨损 + StatTrak），提取价格和销量（USD）
4. **套利对比** — BUFF vs Steam 按日期配对（D ↔ D-7），USD→CNY 卡价转换，均价差 > 0 为目标饰品

**UI 特性：**

- 一键获取选品（自动跑完 4 步，每步可展开查看进度）+ 分步执行（每步可独立重跑）
- 错误日志自动收集展示
- 逐步诊断工具（BUFF 价格提取 / Steam 数据提取）
- BUFF 价格走势独立查询 + Plotly 交互式图表
- 结果持久化至本地 SQLite 数据库

## 环境要求

- Python 3.8+
- Chrome 浏览器（直接调用已安装的，无需额外下载）

## 快速开始

```bash
pip install -r requirements.txt

# Windows
scripts\start.bat

# macOS / Linux
bash scripts/start.sh

# 或直接
streamlit run app.py --server.port 8501
```

### 首次使用

1. 点击侧边栏 **"登录 BUFF"** 和 **"登录 Steam 账号"**，在弹出的浏览器窗口中完成登录
2. 在侧边栏设置参数（品类、目标数量、波动阈值、卡价转换比等）
3. 点击 **"开始一键获取"** 自动执行全部流程，或展开"分步执行"逐步操作

---

## 侧边栏参数

| 参数 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| 目标日期 | 筛选基准日期 | 当天 | 任意 |
| 价格稳定考察天数 | 考察价格波动的天数 | 15 天 | 1~90 天 |
| 价格波动阈值 | 允许的最大价格波动幅度 | 5% | 1%~30% |
| USD→CNY 卡价转换比 | Steam 美元转人民币 | 5.0 | 0.1~20.0 |

**BUFF 筛选条件：**

| 参数 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| 饰品品类 | 多选，默认全部/不限；多品类时各品类独立抓取数量均分 | 全部/不限 | 匕首/手套/步枪/手枪/微冲/霰弹枪/机枪/印花/挂件/探员/其他 |
| 目标数量 | BUFF 端初步筛选目标，逐页抓取至达成或最后一页 | 200 件 | 10~2000 |
| 最低价格 | BUFF 在售最低价格过滤 | 20 元 | 0~10000 |
| 最低在售数量 | 在售数量下限过滤 | 100 件 | 1~100000 |

---

## 4 阶段流水线详解

### 第一阶段：BUFF 初步筛选

1. 从网易BUFF市场按品类逐页抓取饰品列表
2. 提取每个饰品的名称、价格、在售数量、成交额
3. 按用户设定的最低价格和最低在售数量过滤
4. 多品类时各品类独立抓取、合并结果

### 第二阶段：价格稳定性分析

1. 对通过初筛的每个饰品，调用 BUFF API 获取历史价格记录
2. 计算价格波动幅度：`(最高价 − 最低价) / 平均价`
3. 波动 ≤ 阈值的视为价格稳定，通过筛选

### 第三阶段：Steam 市场数据获取

1. Playwright 浏览器自动化：BUFF 详情页 → 点击"查看Steam市场" → Steam 社区市场
2. 根据 BUFF 饰品属性（磨损等级、StatTrak 状态）匹配 Steam 变体
3. 价格历史通过 Steam pricehistory API 获取
4. 日期配对规则：BUFF 日期 D ↔ Steam 日期 D-7
5. 货币强制为 USD（`?cc=us` + Steam 登录态）

### 第四阶段：套利对比

1. 日期节点配对，Steam 价格 × 卡价转换比 → 人民币
2. 目标判定：BUFF均价 > Steam转换均价 → 标记为目标饰品
3. 输出：BUFF均价、Steam均价($)、Steam均价(¥)、均价差、命中节点数
4. 每个饰品可展开日期节点明细和价格对比图表

---

## 项目结构

```
.
├── app.py                    # Streamlit 主应用（UI + 流程编排）
├── config.py                 # 全局配置与默认参数
├── core/
│   ├── buff_scraper.py       # BUFF 数据抓取（列表页、API、登录、诊断）
│   ├── steam_scraper.py      # Steam 数据提取（浏览器自动化、变体匹配、API）
│   └── filters.py            # 筛选逻辑（初步筛选 + 价格稳定性判定）
├── data/
│   ├── models.py             # 数据模型（ItemSnapshot、PriceRecord）
│   └── database.py           # SQLite 持久化（runs + run_items 表）
├── utils/
│   └── helpers.py            # 工具函数（随机延迟等）
├── scripts/
│   ├── start.bat             # Windows 启动脚本
│   └── start.sh              # macOS/Linux 启动脚本
├── storage/                  # Cookie、数据库（.gitignore 忽略）
├── .streamlit/
│   └── config.toml           # Streamlit 配置（禁用遥测、最小化工具栏）
├── requirements.txt          # Python 依赖
└── .gitignore
```

## 依赖

```
streamlit>=1.30.0
pandas>=2.0.0
playwright>=1.40.0
requests>=2.31.0
plotly>=5.18.0
```

---

## 当前版本功能清单

| 功能 | 状态 |
|------|------|
| BUFF 初步筛选（可配置品类/数量/价格/在售） | ✅ |
| 价格稳定性分析（波动阈值筛选） | ✅ |
| Steam 市场数据提取（浏览器自动化） | ✅ |
| 变体匹配（磨损 + StatTrak） | ✅ |
| USD→CNY 卡价转换 | ✅ |
| 一键获取选品（可展开进度明细） | ✅ |
| 分步执行（含重新执行按钮） | ✅ |
| 错误日志系统 | ✅ |
| 逐步诊断工具（BUFF + Steam） | ✅ |
| BUFF 价格走势独立查询 | ✅ |
| BUFF / Steam 登录态管理 | ✅ |
| BUFF vs Steam 价格对比图表（Plotly） | ✅ |
| 结果持久化至 SQLite 数据库 | ✅ |
| CSV 导出 | 未实现 |
| 历史记录查看 | 未实现 |

---

## 注意事项

1. BUFF 和 Steam 的页面结构可能随时变化，选择器和 API 路径需根据实际情况调整
2. BUFF 登录态可能过期，若提示未登录需重新登录
3. Steam 登录态决定页面货币，需确保已登录才能获取美元价格
4. Steam 市场访问过快可能触发限流，工具已内置随机延迟
5. 部分饰品在 Steam 上可能无对应变体，此时 fallback 到默认 bucket
6. 日期配对中，若 Steam 无对应日期数据则跳过该节点
