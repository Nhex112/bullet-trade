"""LightStockDBClient 单元测试（不依赖真实服务）。"""

from __future__ import annotations

import datetime

from bullet_trade.data.providers.stockdb_light import LightStockDBClient

from .test_stockdb_provider import FakeRD, _daily_row


def _daily_rows(code: str, start: datetime.date, end: datetime.date) -> list:
    rows = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            rows.append(_daily_row(int(day.strftime("%Y%m%d")), 10.0, code=code))
        day += datetime.timedelta(days=1)
    return rows


def test_light_get_data_range_and_fields() -> None:
    rd = FakeRD(
        daily={
            "600000": _daily_rows("600000", datetime.date(2024, 1, 1), datetime.date(2024, 1, 20))
        }
    )
    client = LightStockDBClient(rd)
    rows = client.get_data(
        code="600000",
        start="20240102",
        end="20240110",
        frequency="1d",
        fields="date,close",
    )
    assert rows[0] == [20240102, 10.0]
    assert rows[-1] == [20240110, 10.0]
    assert len(rows) == 7


def test_light_get_data_desc_limit() -> None:
    rd = FakeRD(
        daily={
            "600000": _daily_rows("600000", datetime.date(2024, 1, 1), datetime.date(2024, 1, 20))
        }
    )
    client = LightStockDBClient(rd)
    rows = client.get_data(
        code="600000",
        start="20240101",
        end="20240120",
        frequency="1d",
        fields="date",
        desc=True,
        limit=3,
    )
    assert rows == [[20240119], [20240118], [20240117]]


def test_light_get_data_weekly_aggregation() -> None:
    rd = FakeRD(
        daily={
            "600000": _daily_rows("600000", datetime.date(2024, 1, 1), datetime.date(2024, 2, 15))
        }
    )
    client = LightStockDBClient(rd)
    rows = client.get_data(
        code="600000",
        start="20240101",
        end="20240215",
        frequency="1w",
        fields="date,close",
    )
    # 每周一根聚合 K，且按周升序
    assert len(rows) >= 6
    dates = [r[0] for r in rows]
    assert dates == sorted(dates)
    assert all(len(r) == 2 for r in rows)


def test_light_get_data_batch() -> None:
    rd = FakeRD(
        daily={
            "600000": _daily_rows("600000", datetime.date(2024, 1, 1), datetime.date(2024, 1, 10)),
            "000001": _daily_rows("000001", datetime.date(2024, 1, 1), datetime.date(2024, 1, 10)),
        }
    )
    client = LightStockDBClient(rd)
    result = client.get_data(
        code=["600000", "000001"],
        start="20240102",
        end="20240105",
        frequency="1d",
        fields="date",
    )
    assert set(result) == {"600000", "000001"}
    assert result["600000"] == [[20240102], [20240103], [20240104], [20240105]]
    assert len(result["000001"]) == 4
