"""StockDBProvider 离线单元测试（mock stock_sdk，不依赖真实服务）。"""

from __future__ import annotations

import datetime
from datetime import date as Date

import pandas as pd
import pytest

from bullet_trade.data.providers.stockdb import StockDBProvider

_TABLE_DAILY = "日k"
_TABLE_FACTOR = "复权"
_TABLE_CODES = "股票代码"


class FakeRD:
    """模拟 stockdb SDK rd 客户端的最小实现。"""

    def __init__(
        self,
        daily: dict | None = None,
        minute: dict | None = None,
        factors: dict | None = None,
        codes: dict | None = None,
    ) -> None:
        self._daily = {k: list(v) for k, v in (daily or {}).items()}
        self._minute = {k: list(v) for k, v in (minute or {}).items()}
        self._factors = {k: list(v) for k, v in (factors or {}).items()}
        self._codes = codes or {}
        self.last_kwargs: dict | None = None

    # ---------- 内部工具 ----------
    def _factor_pairs(self, code: str):
        pairs = [
            (f"{_TABLE_FACTOR}:{code}:{date_int}", dict(rec))
            for date_int, rec in self._factors.get(code, [])
        ]
        return list(reversed(pairs))  # 与真实 SDK 一致：降序

    def _all_daily_records(self):
        for records in self._daily.values():
            yield from records

    # ---------- get_data ----------
    def get_data(
        self,
        code=None,
        start=None,
        end=None,
        frequency="1d",
        fields=None,
        fq="qfq",
        limit=None,
        desc=False,
        as_df=False,
    ):
        _ = fq, as_df
        self.last_kwargs = {
            "code": code,
            "start": start,
            "end": end,
            "frequency": frequency,
            "limit": limit,
            "desc": desc,
        }
        code = str(code)
        if str(frequency) in ("1m", "5m", "15m", "30m", "60m"):
            records = list(self._minute.get(code, []))
        else:
            records = list(self._daily.get(code, []))
        start_i = int(start) if start is not None else None
        end_i = int(end) if end is not None else None
        rows = []
        for record in records:
            day = int(record["date"])
            if start_i is not None and day < start_i:
                continue
            if end_i is not None and day > end_i:
                continue
            rows.append(record)
        if desc:
            rows = list(reversed(rows))
        if limit is not None:
            rows = rows[: int(limit)]
        if fields:
            names = [item.strip() for item in str(fields).split(",")]
            return [[record.get(name) for name in names] for record in rows]
        return rows

    # ---------- get ----------
    def get(self, table, code=None, query=None):
        if table == _TABLE_CODES and code is None:
            return dict(self._codes)
        if table == _TABLE_DAILY and code and query:
            day = int(str(query)[:8])
            for record in self._daily.get(str(code), []):
                if int(record["date"]) == day:
                    return dict(record)
            return None
        if table == _TABLE_FACTOR and code:
            pairs = self._factor_pairs(str(code))
            if len(pairs) == 1:
                return dict(pairs[0][1])
            return list(pairs)
        return []

    # ---------- keys ----------
    def keys(self, table, code=None, query=None):
        if table == _TABLE_FACTOR and code:
            return [key for key, _ in self._factor_pairs(str(code))]
        if table == _TABLE_DAILY and code and query and "*" in str(query):
            prefix = str(query).replace("*", "")
            return [
                f"{_TABLE_DAILY}:{code}:{day}"
                for day in sorted(
                    int(record["date"])
                    for record in self._daily.get(str(code), [])
                )
                if str(day).startswith(prefix)
            ]
        return []

    # ---------- vals ----------
    def vals(self, table, code="*", query=None):
        if table == _TABLE_DAILY and str(code) == "*":
            day = int(str(query)[:8])
            return [
                dict(record)
                for record in self._all_daily_records()
                if int(str(record["date"])[:8]) == day
            ]
        if table == _TABLE_DAILY and str(code) != "*":
            q = str(query or "")
            records = [dict(r) for r in self._daily.get(str(code), [])]
            if ">" in q:
                start, end = q.split(">", 1)
                return [
                    r for r in records
                    if int(r["date"]) >= int(start) and int(r["date"]) <= int(end)
                ]
            if "<" in q:
                start, end = q.split("<", 1)
                return [
                    r for r in records
                    if int(r["date"]) >= int(start) and int(r["date"]) <= int(end)
                ][::-1]
            if q.endswith("*"):
                prefix = q[:-1]
                return [r for r in records if str(r["date"]).startswith(prefix)]
            if q and q != "*":
                day = int(q[:8])
                return [r for r in records if int(r["date"]) == day]
            return records
        if table == _TABLE_FACTOR and str(code) != "*":
            return [dict(rec) for _, rec in self._factor_pairs(str(code))]
        return []

    def pipe(self):
        return _FakePipe(self)


class _FakePipe:
    """模拟 rd.pipe()：单条返回 dict、多条返回 [key, value] 对（与真实 pyd 一致）。"""

    def __init__(self, rd: FakeRD) -> None:
        self._rd = rd
        self._calls = []

    def mget(self, table, code, query):
        self._calls.append((table, code, query))
        return self

    def do(self):
        out = []
        for table, code, query in self._calls:
            records = list(self._rd.vals(table, code, query))
            if len(records) == 1:
                out.append(dict(records[0]))
            else:
                out.append(
                    [
                        [f"{table}:{code}:{r['date']}", dict(r)]
                        for r in records
                    ]
                )
        return out


def _daily_row(day: int, close: float, **overrides) -> dict:
    row = {
        "date": day,
        "code": "600000",
        "name": "测试股",
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "pre_close": close - 0.05,
        "volume": 100000,
        "amount": 1000000,
        "turnover": 1.0,
        "pct_chg": 0.5,
        "amplitude": 2.0,
        "is_st": False,
        "vol_ratio": 1.0,
        "total_share": 1000000000,
        "float_share": 800000000,
        "total_mv": 10000000000,
        "float_mv": 8000000000,
        "pe_ttm": 10.0,
        "pb": 1.0,
    }
    row.update(overrides)
    return row


def _provider(rd: FakeRD) -> StockDBProvider:
    return StockDBProvider(config={"rd": rd, "cache_dir": None})


def test_code_mapping_and_market_guard() -> None:
    assert StockDBProvider.jq_to_stockdb("600519.XSHG") == "600519"
    assert StockDBProvider.jq_to_stockdb("000001.XSHE") == "000001"
    assert StockDBProvider.jq_to_stockdb("510050") == "510050"
    assert StockDBProvider._infer_suffix("600519") == "XSHG"
    assert StockDBProvider._infer_suffix("510050") == "XSHG"
    assert StockDBProvider._infer_suffix("000001") == "XSHE"
    assert StockDBProvider._infer_suffix("159915") == "XSHE"
    assert StockDBProvider._infer_suffix("920000") == "XBJG"
    # 指数类后缀与代码段不匹配 -> 拒绝，避免拿到同名股票数据
    assert StockDBProvider._resolve_code("000300.XSHG") is None
    assert StockDBProvider._resolve_code("000001.XSHG") is None
    assert StockDBProvider._resolve_code("000001.XSHE") == "000001"
    assert StockDBProvider._resolve_code("600519.XSHG") == "600519"
    assert StockDBProvider._resolve_code("600519.SH") == "600519"


def test_get_price_single_daily_panel() -> None:
    rd = FakeRD(
        daily={
            "600633": [
                _daily_row(20260622, 10.0, code="600633"),
                _daily_row(20260623, 10.5, code="600633"),
                _daily_row(20260624, 10.8, code="600633"),
            ]
        }
    )
    provider = _provider(rd)
    df = provider.get_price(
        "600633.XSHG",
        start_date="20260622",
        end_date="20260624",
        frequency="daily",
        fields=["open", "close", "volume", "money"],
        fq="none",
    )
    assert isinstance(df.index, pd.DatetimeIndex)
    assert list(df.columns) == ["open", "close", "volume", "money"]
    assert len(df) == 3
    assert df["money"].tolist() == [1000000.0, 1000000.0, 1000000.0]
    assert df["close"].tolist() == [10.0, 10.5, 10.8]


def test_get_price_multi_panel_and_long() -> None:
    rd = FakeRD(
        daily={
            "600519": [_daily_row(20260622, 100.0, code="600519")],
            "000001": [_daily_row(20260622, 12.0, code="000001")],
        }
    )
    provider = _provider(rd)
    securities = ["600519.XSHG", "000001.XSHE"]
    panel_df = provider.get_price(
        securities,
        start_date="20260622",
        end_date="20260622",
        fields=["close"],
        fq="none",
        panel=True,
    )
    assert isinstance(panel_df.columns, pd.MultiIndex)
    assert list(panel_df.columns) == [("close", "600519.XSHG"), ("close", "000001.XSHE")]
    long_df = provider.get_price(
        securities,
        start_date="20260622",
        end_date="20260622",
        fields=["close"],
        fq="none",
        panel=False,
    )
    assert list(long_df.columns) == ["time", "code", "close"]
    assert set(long_df["code"]) == set(securities)


def test_get_price_factor_and_dynamic_pre_adjustment() -> None:
    rd = FakeRD(
        daily={
            "600000": [
                _daily_row(20250102, 10.0),
                _daily_row(20250103, 11.0),
            ]
        },
        factors={
            "600000": [
                (20250103, {"div": 0.1, "give": 0.0, "trans": 0.0, "mult": 1.01, "cum": 1.1}),
            ]
        },
    )
    provider = _provider(rd)

    pre = provider.get_price(
        "600000.XSHG",
        start_date="20250102",
        end_date="20250103",
        fields=["close"],
        fq="pre",
    )
    # 前复权（到最新因子）：01-02 = 10 * 1.0 / 1.1 ≈ 9.09；01-03 = 11 * 1.1 / 1.1 = 11
    assert pre["close"].round(2).tolist() == [9.09, 11.0]

    ref = provider.get_price(
        "600000.XSHG",
        start_date="20250102",
        end_date="20250103",
        fields=["close"],
        fq="pre",
        pre_factor_ref_date="20250102",
    )
    # 以 2025-01-02 为参考日：01-02 = 10；01-03 = 11 * 1.1 = 12.1
    assert ref["close"].round(2).tolist() == [10.0, 12.1]

    post = provider.get_price(
        "600000.XSHG",
        start_date="20250102",
        end_date="20250103",
        fields=["close"],
        fq="post",
    )
    assert post["close"].round(2).tolist() == [10.0, 12.1]

    factor_df = provider.get_price(
        "600000.XSHG",
        start_date="20250102",
        end_date="20250103",
        fields=["factor"],
        fq="none",
    )
    assert factor_df["factor"].tolist() == [1.0, 1.1]


def test_get_price_dynamic_ref_requires_factor() -> None:
    rd = FakeRD(daily={"600000": [_daily_row(20250102, 10.0)]})
    provider = _provider(rd)
    with pytest.raises(NotImplementedError):
        provider.get_price(
            "600000.XSHG",
            start_date="20250102",
            end_date="20250102",
            fields=["close"],
            fq="pre",
            pre_factor_ref_date="20250102",
        )


def test_get_price_index_returns_empty() -> None:
    provider = _provider(FakeRD())
    df = provider.get_price(
        "000300.XSHG",
        start_date="20260101",
        end_date="20260201",
        fields=["close"],
        fq="none",
    )
    assert df.empty


def test_get_price_minute_shape() -> None:
    rd = FakeRD(
        minute={
            "600633": [
                {"date": 20260625093500, "code": "600633", "open": 10.0, "high": 10.1,
                 "low": 9.9, "close": 10.05, "volume": 100, "amount": 1005},
                {"date": 20260625094000, "code": "600633", "open": 10.05, "high": 10.2,
                 "low": 10.0, "close": 10.15, "volume": 200, "amount": 2030},
            ]
        }
    )
    provider = _provider(rd)
    df = provider.get_price(
        "600633.XSHG",
        start_date="20260625093000",
        end_date="20260625094000",
        frequency="1m",
        fields=["close", "volume"],
        fq="none",
    )
    assert len(df) == 2
    assert df.index[0].hour == 9 and df.index[0].minute == 35
    assert df["close"].tolist() == [10.05, 10.15]


def test_get_price_minute_count1_falls_back_to_daily() -> None:
    rd = FakeRD(
        daily={
            "600000": [
                _daily_row(20240103, 10.0),
                _daily_row(20240102, 9.8),
            ]
        }
    )
    provider = _provider(rd)
    df = provider.get_price(
        "600000.XSHG",
        end_date="2024-01-03 10:00:00",
        count=1,
        frequency="minute",
        fields=["open", "close"],
        fq="none",
    )
    # 分钟线无数据时，count=1 的行情探测回退到当日日线
    assert len(df) == 1
    assert df.index[0].date() == pd.Timestamp("2024-01-03").date()
    assert df["close"].tolist() == [10.0]

    # count>1 的分钟级取数不伪造数据，仍返回空
    empty = provider.get_price(
        "600000.XSHG",
        end_date="2024-01-03 10:00:00",
        count=5,
        frequency="minute",
        fields=["close"],
        fq="none",
    )
    assert empty.empty


def test_get_price_minute_fallback_can_be_disabled() -> None:
    rd = FakeRD(daily={"600000": [_daily_row(20240103, 10.0)]})
    provider = StockDBProvider(
        config={"rd": rd, "cache_dir": None, "minute_daily_fallback": False}
    )
    df = provider.get_price(
        "600000.XSHG",
        end_date="2024-01-03 10:00:00",
        count=1,
        frequency="minute",
        fields=["close"],
        fq="none",
    )
    assert df.empty


def test_get_trade_days_union_and_semantics() -> None:
    rd = FakeRD(
        daily={
            "600000": [_daily_row(20260105, 1.0), _daily_row(20260106, 1.0)],
            "600519": [_daily_row(20260105, 1.0), _daily_row(20260107, 1.0)],
            "601398": [_daily_row(20260106, 1.0), _daily_row(20260107, 1.0)],
            "000001": [_daily_row(20260105, 1.0), _daily_row(20260106, 1.0),
                       _daily_row(20260107, 1.0)],
        }
    )
    provider = _provider(rd)
    days = provider.get_trade_days(start_date="20260101", end_date="20260131")
    assert [d.strftime("%Y%m%d") for d in days] == ["20260105", "20260106", "20260107"]
    assert len(provider.get_trade_days(start_date="20260106", end_date="20260131")) == 2
    assert len(provider.get_trade_days(end_date="20260131", count=2)) == 2
    assert provider.get_trade_day("600000.XSHG", "20260105") == Date(2026, 1, 5)
    assert provider.get_trade_day("600000.XSHG", "20260104") == Date(2026, 1, 5)


def test_auth_raises_when_service_down_and_no_auto_start() -> None:
    provider = StockDBProvider(config={"auto_start": False, "cache_dir": None, "sdk_dir": None})
    provider._port_open = lambda *args, **kwargs: False
    with pytest.raises(RuntimeError, match="未运行"):
        provider.auth()


def test_auth_auto_start_flow(monkeypatch) -> None:
    started = []

    class FakeModule:
        rd = FakeRD()

    provider = StockDBProvider(
        config={"cache_dir": None, "sdk_dir": None, "use_light_client": False}
    )
    provider._port_open = lambda *args, **kwargs: False
    provider._start_server = lambda: started.append(True)
    provider._wait_port = lambda *args, **kwargs: True
    provider._import_sdk = lambda: FakeModule()
    provider.auth()
    assert started == [True]
    assert provider._rd is FakeModule.rd


def test_auth_auto_start_timeout_raises() -> None:
    provider = StockDBProvider(config={"cache_dir": None, "sdk_dir": None})
    provider._port_open = lambda *args, **kwargs: False
    provider._start_server = lambda: None
    provider._wait_port = lambda *args, **kwargs: False
    with pytest.raises(RuntimeError, match="超时"):
        provider.auth()


def test_get_all_securities_types_filter() -> None:
    rd = FakeRD(
        daily={
            "600000": [_daily_row(20260105, 10.0, code="600000", name="浦发银行")],
            "000001": [_daily_row(20260105, 12.0, code="000001", name="平安银行")],
            "510050": [_daily_row(20260105, 3.0, code="510050", name="上证50ETF")],
        },
        codes={"0": ["000001"], "6": ["600000"], "5": ["510050"]},
    )
    provider = _provider(rd)
    stocks = provider.get_all_securities(types="stock")
    assert set(stocks.index) == {"000001.XSHE", "600000.XSHG"}
    assert stocks.loc["600000.XSHG", "name"] == "浦发银行"
    etfs = provider.get_all_securities(types="etf")
    assert list(etfs.index) == ["510050.XSHG"]
    all_df = provider.get_all_securities(types=["stock", "etf"])
    assert len(all_df) == 3


def test_get_security_info() -> None:
    rd = FakeRD(
        daily={
            "600000": [
                _daily_row(20260105, 10.0, code="600000", name="浦发银行"),
                _daily_row(20260106, 10.2, code="600000", name="浦发银行"),
                _daily_row(20260107, 10.3, code="600000", name="浦发银行"),
            ],
            "000001": [_daily_row(20260105, 12.0, code="000001", name="平安银行")],
        }
    )
    provider = _provider(rd)
    info = provider.get_security_info("600000.XSHG")
    assert info["name"] == "浦发银行"
    assert info["type"] == "stock"


def test_get_split_dividend_events() -> None:
    rd = FakeRD(
        factors={
            "600633": [
                (20240115, {"div": 0.5, "give": 0.4, "trans": 0.6, "mult": 1.11, "cum": 2.0}),
            ]
        }
    )
    provider = _provider(rd)
    events = provider.get_split_dividend(
        "600633.XSHG", start_date="20240101", end_date="20240201"
    )
    assert len(events) == 1
    event = events[0]
    assert event["security"] == "600633.XSHG"
    assert event["date"] == Date(2024, 1, 15)
    assert abs(event["scale_factor"] - 1.1) < 1e-9
    assert abs(event["bonus_pre_tax"] - 5.0) < 1e-9
    assert event["per_base"] == 10
    assert provider.get_split_dividend(
        "600633.XSHG", start_date="20240116", end_date="20240201"
    ) == []


def test_get_bars_wraps_get_price() -> None:
    rd = FakeRD(
        daily={
            "600519": [
                _daily_row(20260622, 100.0, code="600519"),
                _daily_row(20260623, 101.0, code="600519"),
                _daily_row(20260624, 102.0, code="600519"),
            ]
        }
    )
    provider = _provider(rd)
    frame = provider.get_bars(
        "600519.XSHG", count=2, unit="1d", df=True, end_dt="2026-06-24"
    )
    assert len(frame) == 2
    assert frame["close"].tolist() == [101.0, 102.0]
    raw = provider.get_bars(
        "600519.XSHG", count=2, unit="1d", df=False, end_dt="2026-06-24"
    )
    assert isinstance(raw, dict)


def test_get_price_count_uses_bounded_window() -> None:
    rows = []
    day = datetime.date(2023, 6, 1)
    while day <= datetime.date(2024, 6, 30):
        if day.weekday() < 5:
            rows.append(_daily_row(int(day.strftime("%Y%m%d")), 10.0, code="600519"))
        day += datetime.timedelta(days=1)
    rd = FakeRD(daily={"600519": rows})
    provider = _provider(rd)
    df = provider.get_price(
        "600519.XSHG",
        end_date="2024-06-01",
        count=5,
        fields=["close"],
        fq="none",
    )
    assert len(df) == 5
    assert rd.last_kwargs is not None
    assert rd.last_kwargs["start"] != "19900101"
    assert rd.last_kwargs["desc"] is True
    assert rd.last_kwargs["limit"] == 5
    # 返回 end 之前最新 5 个交易日
    assert df.index[-1].strftime("%Y%m%d") == "20240531"
    assert df.index[0].strftime("%Y%m%d") == "20240527"


def test_get_price_minute_count_uses_bounded_window() -> None:
    rd = FakeRD(
        minute={
            "600633": [
                {"date": 20260625093500, "code": "600633", "open": 10.0, "high": 10.1,
                 "low": 9.9, "close": 10.05, "volume": 100, "amount": 1005},
                {"date": 20260625094000, "code": "600633", "open": 10.05, "high": 10.2,
                 "low": 10.0, "close": 10.15, "volume": 200, "amount": 2030},
            ]
        }
    )
    provider = _provider(rd)
    provider.get_price(
        "600633.XSHG",
        end_date="2026-06-25 09:40:00",
        count=1,
        frequency="minute",
        fields=["close"],
        fq="none",
    )
    assert rd.last_kwargs is not None
    assert rd.last_kwargs["start"] != "19900101"
    assert rd.last_kwargs["start"].endswith("000000")


def test_unsupported_extensions_raise() -> None:
    provider = _provider(FakeRD())
    with pytest.raises(NotImplementedError):
        provider.get_current_tick("000001.XSHE")
    with pytest.raises(NotImplementedError):
        provider.get_fundamentals(None)
    with pytest.raises(NotImplementedError):
        provider.get_ticks("000001.XSHE", end_dt="20260101")
    with pytest.raises(NotImplementedError):
        provider.get_index_stocks("000300.XSHG")


def test_factor_records_single_and_multi_normalization() -> None:
    rd = FakeRD(
        factors={
            "600000": [(20240101, {"div": 0.1, "give": 0.0, "trans": 0.0,
                                   "mult": 1.01, "cum": 1.1})]
        }
    )
    provider = _provider(rd)
    assert provider._factor_records("600000") == [
        (20240101, {"div": 0.1, "give": 0.0, "trans": 0.0, "mult": 1.01, "cum": 1.1})
    ]
    rd2 = FakeRD(
        factors={
            "600000": [
                (20230101, {"div": 0.1, "give": 0.0, "trans": 0.0, "mult": 1.01, "cum": 1.1}),
                (20240101, {"div": 0.2, "give": 0.0, "trans": 0.0, "mult": 1.02, "cum": 1.3}),
            ]
        }
    )
    provider2 = _provider(rd2)
    records = provider2._factor_records("600000")
    assert [r[0] for r in records] == [20230101, 20240101]
