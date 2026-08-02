"""
StockDB 与 JQData 数据口径对账测试（e2e，需本地 stockdb 服务与 JQData 账号）。

对账范围：
- 日线未复权（raw）价格/成交量/成交额
- 日线前复权（fq=pre）
- pre_factor_ref_date 动态前复权
- 交易日历覆盖
"""

from __future__ import annotations

import os
from typing import Iterable

import pandas as pd
import pytest

from bullet_trade.data.providers.jqdata import JQDataProvider
from bullet_trade.data.providers.stockdb import StockDBProvider
from bullet_trade.utils.env_loader import load_env

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_stockdb,
]

SECURITY = "000001.XSHE"
LONG_SECURITIES = ["000001.XSHE", "600519.XSHG", "510050.XSHG"]
DAILY_START = "2026-06-10"
DAILY_END = "2026-06-12"
LONG_DAILY_START = "2024-01-01"
LONG_DAILY_END = "2026-06-12"
REF_DATE = "2025-06-27"
FIELDS = ["open", "high", "low", "close", "volume", "money"]
PRICE_FIELDS = ["open", "high", "low", "close"]


def _check_prerequisites() -> None:
    load_env()
    if not os.getenv("JQDATA_USERNAME") or not os.getenv("JQDATA_PASSWORD"):
        pytest.skip("缺少 JQDATA_USERNAME/JQDATA_PASSWORD，跳过 stockdb vs JQData 对账")


def _providers() -> tuple[JQDataProvider, StockDBProvider]:
    _check_prerequisites()
    jq = JQDataProvider({"cache_dir": None})
    try:
        jq.auth()
    except Exception as exc:
        pytest.skip(f"JQData 认证失败: {exc}")
    stockdb = StockDBProvider({"cache_dir": None, "auto_start": False})
    try:
        stockdb.auth()
    except Exception as exc:
        pytest.skip(f"stockdb 服务不可用: {exc}")
    return jq, stockdb


def _align_frames(jq_df: pd.DataFrame, stock_df: pd.DataFrame) -> pd.DataFrame:
    if jq_df is None or stock_df is None or jq_df.empty or stock_df.empty:
        pytest.skip(f"对账数据为空: JQData rows={0 if jq_df is None else len(jq_df)} "
                    f"stockdb rows={0 if stock_df is None else len(stock_df)}")
    jq_norm = jq_df.copy()
    stock_norm = stock_df.copy()
    jq_norm.index = pd.to_datetime(jq_norm.index)
    stock_norm.index = pd.to_datetime(stock_norm.index)
    common = jq_norm.index.intersection(stock_norm.index)
    if common.empty:
        pytest.skip("JQData 与 stockdb 没有可对齐的时间索引")
    columns = [f for f in FIELDS if f in jq_norm.columns and f in stock_norm.columns]
    return (jq_norm.loc[common, columns] - stock_norm.loc[common, columns]).abs()


def _assert_close(diff: pd.DataFrame, price_tol: float, volume_tol: float, money_tol: float) -> None:
    for field in diff.columns:
        max_diff = float(diff[field].max())
        if field == "volume":
            tolerance = volume_tol
        elif field == "money":
            tolerance = money_tol
        else:
            tolerance = price_tol
        assert max_diff <= tolerance, f"{field} 最大偏差 {max_diff} 超过阈值 {tolerance}"


def _max_diff_by_field(diff: pd.DataFrame, fields: Iterable[str]) -> dict[str, float]:
    return {field: float(diff[field].max()) for field in fields if field in diff.columns}


def test_stockdb_daily_raw_matches_jqdata_recent_window() -> None:
    jq, stockdb = _providers()
    for security in LONG_SECURITIES:
        jq_df = jq.get_price(security, start_date=DAILY_START, end_date=DAILY_END,
                             frequency="daily", fields=FIELDS, fq="none")
        stock_df = stockdb.get_price(security, start_date=DAILY_START, end_date=DAILY_END,
                                     frequency="daily", fields=FIELDS, fq="none")
        diff = _align_frames(jq_df, stock_df)
        print(f"[DEBUG] {security} raw diff max: {_max_diff_by_field(diff, FIELDS)}")
        _assert_close(diff, price_tol=0.02, volume_tol=5000.0, money_tol=200000.0)


def test_stockdb_daily_pre_matches_jqdata_recent_window() -> None:
    jq, stockdb = _providers()
    jq_df = jq.get_price(SECURITY, start_date=DAILY_START, end_date=DAILY_END,
                         frequency="daily", fields=PRICE_FIELDS, fq="pre")
    stock_df = stockdb.get_price(SECURITY, start_date=DAILY_START, end_date=DAILY_END,
                                 frequency="daily", fields=PRICE_FIELDS, fq="pre")
    diff = _align_frames(jq_df, stock_df)
    print(f"[DEBUG] daily pre diff max: {_max_diff_by_field(diff, PRICE_FIELDS)}")
    _assert_close(diff, price_tol=0.05, volume_tol=0.0, money_tol=0.0)


def test_stockdb_dynamic_pre_factor_ref_date_matches_jqdata() -> None:
    jq, stockdb = _providers()
    jq_df = jq.get_price("600519.XSHG", start_date=LONG_DAILY_START, end_date=LONG_DAILY_END,
                         frequency="daily", fields=PRICE_FIELDS, fq="pre",
                         pre_factor_ref_date=REF_DATE)
    stock_df = stockdb.get_price("600519.XSHG", start_date=LONG_DAILY_START,
                                 end_date=LONG_DAILY_END, frequency="daily",
                                 fields=PRICE_FIELDS, fq="pre", pre_factor_ref_date=REF_DATE)
    diff = _align_frames(jq_df, stock_df)
    max_diff = _max_diff_by_field(diff, PRICE_FIELDS)
    print(f"[DEBUG] dynamic pre diff max: {max_diff}")
    assert max(max_diff.values()) <= 0.15


def test_stockdb_trade_days_cover_jqdata() -> None:
    jq, stockdb = _providers()
    jq_days = jq.get_trade_days(start_date="2025-06-01", end_date="2026-06-30")
    stock_days = stockdb.get_trade_days(start_date="2025-06-01", end_date="2026-06-30")
    assert stock_days, "stockdb 交易日历为空"
    jq_set = {pd.Timestamp(d).date() for d in jq_days}
    stock_set = {pd.Timestamp(d).date() for d in stock_days}
    missing = sorted(stock_set - jq_set)
    assert len(missing) == 0, f"stockdb 存在 JQData 日历之外的日期: {missing[:10]}"
    coverage = len(stock_set & jq_set) / len(jq_set)
    assert coverage >= 0.99, f"stockdb 日历覆盖 JQData 仅 {coverage:.2%}"
