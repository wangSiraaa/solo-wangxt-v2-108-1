# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Svelte 4 + Vite + ECharts 5 |
| API | FastAPI（Pydantic 校验） |
| 计算 | NumPy：温升率、插值、阶段指标，全部为纯函数 |
| 存储 | PostgreSQL（原始采样、下豆点、人工标记），SQLAlchemy ORM |
| 测试 | pytest，同一套用例在 SQLite 与 PostgreSQL 上运行 |

## 数据与口径（重要）

### 温升率 RoR —— 窗口必须说明
采样间隔不均（1–5 s 抖动），因此不用相邻点差分。在每个**实测**时刻 t，取居中时间窗
`[t−W/2, t+W/2]`（默认 W=30 s）内的实测豆温点做普通最小二乘直线拟合，取斜率换算 °C/min。
- 至少 4 个实测点、时间跨度 ≥10 s 才给出 RoR，否则为 null（不编造）；
- **插值点不参与拟合**；探针失联的宽缺口处 RoR 直接断档；
- 序列边缘窗口被截断，返回值带 `ror_edge=true` 标记；
- 前端另有一个“显示平滑”参数（居中均值），只作用于展示曲线，窗口本身随接口参数和图表标题一起返回。

### 缺测与插值 —— 插值段不冒充实测
- `samples` 表**只存实测**：探针失联时豆温为 NULL，绝不回写；
- 查询时对 ≤`max_gap_fill_s`（默认 45 s）的内缺口做**相邻实测点线性插值**，
  逐点带 `is_interpolated=true`，图上为**虚线+空心菱形**，图例单列“插值段（非实测）”；
- 超过桥接上限的缺口与端点缺测**不填充**，曲线断档；缺测段在“缺测与插值审计”表逐条列出（通道、时长、处理方式）。

### 事件 —— 人工修正并保留来源
- 事件为只追加（append-only）。人工提交同类型事件时，旧行置 `superseded=true` 并记录
  `superseded_by_id`，不删除；自动建议记 `source=auto`，人工记 `source=manual`+`created_by`；
- 风门变化允许多条并存（离散操作点），金色虚线标出。

### 校准账本 —— 校正只是派生视图，原始采样永不可改写
探针在维护前后可能出现零点/比例漂移。校准记录保存在本地账本（`calibrations` 表）中：

- **每条记录**：通道（bean/env）、有效时间段（自下豆秒数，闭区间）、校正公式与参数
  （`linear: corrected = gain × raw + offset`）、创建人、版本、当前状态；
- **生命周期**：草稿 → 启用 → 撤回 / 被新版本取代。记录只增不改、永不删除，
  每次状态变迁追加到 `calibration_history`（含操作人与时间）；「新版本」把旧记录置
  `superseded` 并生成 version+1 的**草稿**，需再次显式启用；
- **不改原始数据**：校正只在查询时派生 `*_cal_*` 字段（校正温度、校正 RoR、校正引导线），
  原始读数、缺测段、人工事件完全不变；缺测在校正后仍是缺测，插值点仍标记非实测；
- **重叠冲突不静默挑选**：同一通道上有效段相交的已启用记录会让分析与导出返回
  **409 冲突**（带冲突范围与候选版本），必须用 `?cal_ids=` 显式裁决，或撤回其一；
- **全程带口径**：曲线、RoR、阶段指标（含锚点温度 原始→校正）、导出与 `/api/recompute`
  都携带所选校准版本；导出内嵌校准记录快照，独立重算无需数据库即可逐点复现；
- 图上同屏审阅：校正实线 + 原始口径灰虚线 + 校准有效段金色底纹（标注 id/版本）。

### 发展时间比 —— 明确区间
| 指标 | 区间 |
|---|---|
| 脱水期 drying | 下豆 charge → 回温点 turning_point |
| 梅纳期 maillard | 回温点 → 一爆开始 first_crack_start |
| 发展期 development | 一爆开始 → 出锅 drop |
| 一爆持续 | 一爆开始 → 一爆结束 |
| 总时长 total | 下豆 → 出锅 |
| **发展时间比 DTR** | development / total |

边界事件缺失时指标为 `null`（不猜测），并返回每个锚点的来源以便审计。

### 双批次对比 —— 不宣称因果
两批次按开火/下豆时刻对齐叠加；风门变化前后的形态变化仅供观察，接口和界面都附带声明：
无对照、无重复、无统计检验，**不构成因果结论**。

## 快速开始

### 方式一：本地

    # 终端 1 —— API（需要先有 PostgreSQL，或用 SQLite 做本地演示）
    cd backend
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # 默认连接 postgresql+psycopg2://roast:roast@localhost:5432/roast
    # 仅本地无 PG 时：export DATABASE_URL="sqlite:///./dev.db"
    uvicorn app.main:app --reload --port 8000

    # 终端 2 —— 前端
    cd frontend
    npm install
    npm run dev        # http://localhost:5173 （/api 已代理到 8000）

打开页面后点 **① 生成两个合成批次**：A 批在 300 s 有关一次风门（70%→40%），B 批无风门变化，
两批均含测量噪声、不均采样、一次短失联（5 s，插值桥接）和一次长失联（56 s，断档不桥接）。

### 方式二：docker compose

    docker compose up --build
    # web: http://localhost:5173  api: http://localhost:8000/docs

## 验证（对应需求中的验收项）

    pytest                       # SQLite
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

界面“缺测与插值审计 · 导出可复现”面板一键完成：
1. 导出 JSON（原始采样 + 全量事件含已取代行 + 参数 + 阶段指标 + 生效校准记录快照）；
2. 调 `/api/recompute` 从原始数据独立重算，逐指标比对（脱水/梅纳/发展/一爆/总时长/DTR），
   并用同一校准版本逐点比对校正曲线/校正 RoR；
3. 再用翻倍窗口、不同平滑重取曲线，逐点比对原始豆温/环温**完全不变**。

校准验收（tests/test_calibration.py，一一对应需求验收项）：
1. 启用非重叠校准 → 校正曲线/RoR/锚点温度按新口径变化，逐条原始温度完全不变；
2. 两条相交启用校准 → 分析与导出 409 阻断并显示冲突范围，`cal_ids` 裁决后只用明确版本；
3. 长失联区与插值标记在校正后仍保持断档与非实测身份；
4. 撤回 / 新版本取代后当前视图按规则回退，旧版本及其历史结果仍可审计复现；
5. 导出载荷在独立重算中使用同一校准版本得到逐点相同结果。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches` | 批次列表 |
| POST | `/api/seed` | 生成两个合成批次 |
| GET | `/api/batches/{id}/series?window_s&display_smooth_s&max_gap_fill_s&cal_ids` | 曲线+RoR+指标（含校正视图；重叠冲突→409） |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加） |
| GET/POST | `/api/batches/{id}/calibrations` | 校准账本列表（含变迁历史）/ 录入草稿 |
| POST | `/api/calibrations/{id}/activate` `/withdraw` `/supersede` | 校准状态变迁（启用/撤回/新版本取代，均留痕） |
| GET | `/api/compare?a=&b=&cal_ids` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export?cal_ids` | 自包含导出（内嵌生效校准记录快照） |
| POST | `/api/recompute` | 从导出载荷独立重算全部派生指标（无需数据库） |

## 目录

    backend/app/  config.py models.py analysis.py synth.py schemas.py main.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_calibration.py（双后端同一套用例）
