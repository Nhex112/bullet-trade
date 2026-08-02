# StockDB 本地数据源（Provider）

StockDB 是一个免费的本地股票数据库（LevelDB），通过 `stockdb.exe` 提供本地行情服务。
BulletTrade 的 `StockDBProvider` 将其接入为聚宽兼容的数据源，
`DEFAULT_DATA_PROVIDER=stockdb` 即可启用。

## 前置条件

1. 同步数据：双击 `stockdb/数据更新.exe`，等待同步完成（可每日或隔 N 天执行；运行前先退出 stockdb.exe）。
2. 启动服务：`stockdb/stockdb.exe`（监听 127.0.0.1:7899）。
   - 也可以不手动启动：provider 在 `STOCKDB_AUTO_START=true`（默认）时自动拉起并健康检查。
3. Python SDK：`stockdb/pybao/` 目录（`stockdb.pyd` + `stock_sdk.py`），provider 会自动探测仓库同级 `stockdb/pybao` 路径，也可用 `STOCKDB_SDK_DIR` 显式指定。

## 配置示例

```env
DEFAULT_DATA_PROVIDER=stockdb
STOCKDB_HOST=127.0.0.1
STOCKDB_PORT=7899
#STOCKDB_SDK_DIR=          # 指向 stockdb/pybao，留空时自动探测仓库同级路径
STOCKDB_AUTO_START=true    # 服务未启动时自动拉起 stockdb.exe
#STOCKDB_EXE=              # stockdb.exe 完整路径，留空时自动探测
STOCKDB_TIMEOUT=15         # 服务就绪轮询超时（秒）
```

## 支持范围（v1）

| 能力 | 说明 |
| --- | --- |
| 日线 | 股票/ETF/基金，2005 年至今，字段含 OHLC、volume（股）、amount（元）、pre_close、turnover、pct_chg、pe_ttm、pb 等 |
| 分钟线 | 1m/5m/15m/30m/60m，14 位时间戳对齐 |
| 周线/月线 | 由日线在内存聚合 |
| 复权 | 未复权、前复权、后复权；`factor` 字段；`pre_factor_ref_date` 动态前复权（复权表 cum 因子，raw * factor / factor_ref） |
| 交易日历 | 一组长历史锚点股票日 K 日期并集（经磁盘缓存） |
| 证券列表 | `get_all_securities`/`get_security_info`，代码格式为聚宽风格（`600519.XSHG`/`000001.XSHE`/`510050.XSHG`/`920000.XBJG`） |
| 除权除息 | `get_split_dividend` 由复权表解析（div=每股现金、give/trans=每 10 股送转） |

## 明确不支持的接口（UNSUPPORTED）

指数 K 线（如基准 `000300.XSHG`）、期货/期权、财务（`get_fundamentals`）、
行业/概念、tick（`get_ticks`/`get_current_tick`）、龙虎榜、限售股等扩展接口
保持 `NotImplementedError`，不返回假数据。基准缺失时回测报告自动降级（基准指标为 0），不影响策略交易。

## 代码格式约定

策略内统一使用聚宽格式代码；provider 按代码段自动推断交易所：

- `6`/`5` 开头 → `XSHG`（沪市股票/ETF）
- `0`/`1`/`2`/`3` 开头 → `XSHE`（深市股票/基金）
- `4`/`8`/`9` 开头 → `XBJG`（北交所）

后缀与代码段不匹配的标的（例如把 `000001.XSHG` 当上证指数查询）会返回空数据并记录警告，
避免误取同名深市股票（`000001.XSHE` 平安银行）。

## 已知边界

- 交易日历为锚点并集近似，与官方日历可能存在极小概率偏差；`get_trade_days` 支持 start/end/count 语义。
- 证券列表名称取自最新交易日快照，停牌或无记录标的名称回退为代码。
- 停牌日不写入 K 线记录，行情序列不包含停牌日空行（与 `fill_paused` 语义不同）。
- 首次 `auth()` 需导入 SDK 并预热复权因子，可能耗时数秒到数十秒（一次性，之后走磁盘缓存）。
- 价格精度：股票 2 位小数，`1`/`5` 开头基金/ETF 3 位小数。

## 验收状态

详见 [DATA_PROVIDER_ACCEPTANCE.md](DATA_PROVIDER_ACCEPTANCE.md) 的 StockDB 打样结果；
与 JQData 的数值对账因当前 JQData 账号未开通 SDK 权限而标记 PARTIAL/BLOCKED，待权限恢复后补充。
