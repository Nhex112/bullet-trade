# StockDB 接入实现记录

本文档记录 StockDB 本地数据源接入 BulletTrade 的完整提交历史、验证结果与性能演进，
便于回溯与审计。

## 背景与目标

- 目标：将 stockdb（本地 LevelDB 行情服务，默认 127.0.0.1:7899）作为
  `DEFAULT_DATA_PROVIDER=stockdb` 接入 BulletTrade，实现聚宽兼容的本地回测。
- 范围：股票/ETF/基金日线与分钟线、交易日历、复权（前/后复权与动态前复权）、
  证券列表、除权除息；指数/财务/板块/tick 等扩展接口明确 UNSUPPORTED。
- 数据源现状：日线 2005 年至今；分钟线覆盖不完整（部分历史日期无 1m 数据）；
  复权因子表（div/give/trans/mult/cum）本地齐全。

## 提交历史

| 提交 | 日期 | 标题 | 要点与验证 |
| --- | --- | --- | --- |
| `d9ecf2a` | 2026-08-02 | 新增 StockDB 本地数据源 Provider | 实现 `StockDBProvider`（行情/日历/证券列表/复权/分红）、注册到 `data/api.py` 与 `env_loader`、17 个单元测试、e2e 对账测试、真实回测冒烟（买入成交 + 分红入账） |
| `daa7bf9` | 2026-08-02 | StockDB 分钟线覆盖不完整时回退日线近似 | 引擎日频回测的 count=1 当前行情探测在分钟线无数据区间自动回退当日日线；新增 `STOCKDB_MINUTE_DAILY_FALLBACK` 开关；单元测试 +3 |
| `34ee766` | 2026-08-02 | 配置示例移除 JQData/Tushare 凭据，默认数据源改为 StockDB | 删除 `.env` 与示例中的 JQData 账号密码、Tushare token；`DEFAULT_DATA_PROVIDER=stockdb`、`DEFAULT_BENCHMARK=510300.XSHG`；全工作区验证无真实凭据残留 |
| `6d39221` | 2026-08-03 | 优化 StockDB 回测性能：内存缓存日历/证券表 + count 查询有界窗口 | 消除日历缓存逐元素 `pd.to_datetime`（约 300s）与证券表重复转换（约 70s）；count 查询从全历史倒序扫描（约 250ms/次）改为有界窗口（约 2ms/次）；98 天 demo 总耗时约 200s+ 降至 10.6s（引擎 5.7s），结果一致；单元测试 +4 |
| `8797268` | 2026-08-03 | demo 策略基准改为沪深300ETF，忽略回测产物与本地缓存 | `strategies/demo_strategy.py` 基准 `000300.XSHG` → `510300.XSHG`；`.gitignore` 忽略 `backtest_results/`、`.cache/`、`~/` |
| `5a9e7c0` | 2026-08-03 | P0：默认启用回测行情块预取，清理 CONCURRENT_DATA_FETCH 死配置 | `BT_BACKTEST_DATA_SESSION` + `BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS` 默认开启；删除代码未引用的 `CONCURRENT_DATA_FETCH`；实测 1 年回测引擎耗时 14.6s→9.1s（-37%）、98 天 demo 5.7s→4.3s，结果一致 |
| `e3bcbd9` | 2026-08-03 | P1：默认启用轻量客户端 + 服务健康自愈（阈值 3） | 新增 `LightStockDBClient`（底层 pyd 直连，冷启动 303ms→8ms，8 组查询与 stock_sdk 逐项对照一致）；`STOCKDB_USE_LIGHT_CLIENT` 默认开启；健康自愈 `STOCKDB_AUTO_HEAL` + `STOCKDB_HEAL_THRESHOLD=3`（实测阈值 1~5 对应自愈约 15s~76s，阈值 3 约 46s）；修复仓库根目录探测层级；单元测试共 25 个通过 |

## 性能演进（98 天 demo 回测）

| 阶段 | 总耗时 | 引擎 runtime | 结果 |
| --- | --- | --- | --- |
| 初始实现（含全历史 count 扫描、无内存缓存） | 200s~390s（服务劣化时更久） | ~167s+ | 4.17% |
| 内存缓存 + 有界窗口（`6d39221`） | 10.6s | 5.7s | 4.17% |
| 开启行情块预取（`5a9e7c0`，P0） | 9.3s | 4.3s | 4.17% |
| 轻量客户端（`e3bcbd9`，P1） | 8.1s | 4.2s | 4.17% |

1 年（242 个交易日）回测：默认 19.0s（引擎 14.6s）→ 开启行情块预取 13.6s（引擎 9.1s），结果一致（8.11%）。

## 已知边界（详见 DATA_PROVIDER_STOCKDB.md）

- 分钟线覆盖不完整，日频回测依赖 count=1 日线回退；分钟级取数在无数据区间返回空。
- 交易日历为锚点并集近似；证券名称取自最新交易日快照，停牌标的回退为代码。
- 指数 K 线（基准 000300.XSHG 等）缺失时报告自动降级，不影响策略交易。
- 与 JQData 的数值对账因账号未开通 SDK 权限而标记 PARTIAL/BLOCKED，待权限恢复后补充。

## 相关文档

- [StockDB 本地数据源使用说明](DATA_PROVIDER_STOCKDB.md)
- [数据源能力矩阵](DATA_PROVIDER_MATRIX.md)
- [数据源验收报告](DATA_PROVIDER_ACCEPTANCE.md)
- [后续优化 TODO-list](STOCKDB_TODO.md)
