# L2LLM 架构重构第一/第二/第三/第四/第五/第六阶段

## 第一阶段：兼容式拆分

当前先保留 `backend/main.py` 中的原有函数，避免一次性重构影响行情、AI判断、仓位、缓存等接口。
新增 `backend/analysis/` 作为本地分析模块层，API 入口仍然是 `/api/analyze`。

核心文件：

- `backend/analysis/base.py`：定义 `AnalysisContext` 和模块协议。
- `backend/analysis/registry.py`：按注册顺序执行分析模块。
- `backend/analysis/modules.py`：接入主图 MACD、秒级 MACD、DDE、威科夫。
- `backend/analysis/engine.py`：统一运行本地分析，并保持前端 API 输出兼容。
- `backend/analysis/wave.py`：预留波浪理论模块接口。

## 第二阶段：插件化分析模块

`/api/analyze` 已改为调用 `run_local_analysis()`。新引擎会先运行注册模块，再合并回旧的 `signals` 输出。
这保证前端当前字段不变，同时未来新增模块时，不需要改 FastAPI 路由。

当前注册模块：

- `mainChartMacd`：主图 candles 的 MACD 分析。
- `secondsMacd`：独立当日秒级/分时 MACD 分析。
- `ddeFlow`：DDE/大单净量资金流向分析。
- `wyckoff`：威科夫阶段 A-E、事件、三层过滤和买卖信号。

## 后续新增模块方式

以波浪理论为例：

1. 在 `backend/analysis/wave.py` 实现 `WaveModule.analyze()`。
2. 在 `backend/analysis/modules.py` 的 `DEFAULT_ANALYSIS_MODULES` 中注册 `WaveModule()`。
3. 如果前端需要展示，读取 `analysis.signals.wave` 即可。

## 当前边界

这是兼容式重构，不是一次性物理迁移。旧函数仍暂留在 `backend/main.py` 中作为稳定实现来源。

## 第三阶段：分析函数物理迁移

分析插件已不再直接调用 `backend/main.py` 中的旧分析函数，而是调用独立模块：

- `backend/analysis/utils.py`：公共数值工具、`compute_indicators`。
- `backend/analysis/macd.py`：主图 MACD、秒级 MACD、实时 tick 聚合 bar。
- `backend/analysis/dde.py`：DDE 估算、大单净量、DDE 信号。
- `backend/analysis/wyckoff.py`：威科夫阶段、事件、三层过滤和买卖信号。

`backend/analysis/modules.py` 只负责把模块注册到统一执行链，不再承载具体算法逻辑。

第三阶段后，单项算法已经迁移，最终评分编排器仍待拆分。

## 第四阶段：评分编排器迁移

新增 `backend/analysis/composer.py`，负责：

- 复合信号：主力吸筹、游资点火、诱多、封板概率、风险等级。
- K线识别：趋势、经典形态、支撑压力位置。
- 最终结论：`direction`、`confidence`、`score`、`summary`、`reasons`、`risks`。

`backend/analysis/engine.py` 已改为：

1. 构造 `AnalysisContext`。
2. 执行已注册分析模块。
3. 调用 `compose_analysis()` 生成最终本地 AI 判断。
4. 输出 `analysisArchitecture.engine = modular-composer`。

主分析链路已经不再回调 `backend.main.heuristic_analysis`。

第四阶段后的兼容边界：

- `backend/main.py` 中的旧分析函数暂不删除，避免旧路径、调试脚本或未迁移调用点失效。
- 后续第五阶段可以做删除旧函数、瘦身 `main.py`、并把数据源路由继续拆到 `backend/providers/`。

## 第五阶段：删除旧分析函数并瘦身 main.py

已从 `backend/main.py` 删除旧本地分析函数：

- `compute_indicators`
- `label_by_score`
- `estimate_dde_flow`
- `calculate_large_order_net_flow`
- `build_dde_signal`
- `build_realtime_bars`
- `build_seconds_macd_signal`
- `build_main_chart_macd_signal`
- `build_wyckoff_signal`
- `build_market_signals`
- `detect_kline_patterns`
- `heuristic_analysis`

保留在 `backend/main.py` 的职责：

- FastAPI 路由。
- A股/美股/港股数据源适配。
- 盘口、K线、实时行情、历史查询接口。
- 第三方 AI 请求调度和缓存控制。
- 调用 `run_local_analysis()` 获取本地 AI 判断。

当前主分析入口：

- `backend/analysis/engine.py::run_local_analysis`
- `backend/analysis/composer.py::compose_analysis`

第五阶段后，`backend/main.py` 行数已明显下降。下一阶段建议继续把数据源拆成 `backend/providers/`，让 `main.py` 进一步只保留路由编排。

## 第六阶段：Provider 公共层与本地缓存源拆分

新增 `backend/providers/`，开始把数据源相关能力从 `backend/main.py` 迁出。

已迁移：

- `backend/providers/common.py`
  - 市场时区：`CHINA_TZ`、`US_TZ`
  - 数值转换：`finite`
  - A股代码识别：`normalize_a_share_symbol`
  - 返回 symbol 规范化：`normalize_market_response_symbol`
  - range/interval 工具：`range_window_ms`、`normalize_interval`、`is_aggregated_interval`
  - K线重采样：`resample_candles`
  - DataFrame 转 records：`df_records`
  - Series 数值清洗：`numeric_series`
  - 免费源盘口估算：`synthesize_order_book`
- `backend/providers/local_cache.py`
  - `local_cached_market_payload`
  - `fallback_to_local_market`

`backend/main.py` 现在直接从 provider 公共层导入这些工具，不再自己定义重复函数。

当前边界：

- A股 iFinD/AKShare/Eastmoney/Sina、Moomoo、Yahoo、Twelve Data 的具体 provider 函数还在 `backend/main.py`。
- 第六阶段先拆低风险公共层和本地 fallback，避免一次性迁移数据源时产生循环导入。
- 后续第七阶段可以按 provider 文件继续迁移：
  - `backend/providers/a_share.py`
  - `backend/providers/moomoo.py`
  - `backend/providers/yahoo.py`
  - `backend/providers/twelvedata.py`
