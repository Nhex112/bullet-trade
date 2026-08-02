"""
StockDB 轻量客户端（P1-3 原型）

直接基于底层 stockdb pyd（import stockdb 约 9ms），自实现 get_data 封装，
绕开 stock_sdk 模块导入时的复权因子全表预加载（健康时约 0.3s，
服务劣化时实测可达 8~70s），降低冷启动时间与失败风险。

聚合逻辑移植自 stockdb/pybao/stock_sdk.py（周/月 K 由日 K 聚合，
5m/15m/30m/60m 由 1m 聚合），行为与 stock_sdk.rd.get_data 对齐。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Union

_TABLE_DAILY = "日k"
_TABLE_MINUTE = "分钟k"


def _build_time_query(
    start: Optional[str], end: Optional[str], desc: bool
) -> str:
    """与 stock_sdk 相同语义：">" 升序、"<" 降序、单日直查。"""
    if not start and not end:
        return "*"
    if start and (not end or start == end):
        return start
    op = "<" if desc else ">"
    start_val = start if start else "N"
    end_val = end if end else "N"
    return f"{start_val}{op}{end_val}"


def _filter_fields(
    data_list: List[Dict[str, Any]], fields: Optional[Union[str, List[str]]]
) -> List[List[Any]]:
    """按字段列表投影为二维列表（与 stock_sdk 一致）。"""
    if not fields:
        return data_list
    if isinstance(fields, str):
        fields = [item.strip() for item in fields.split(",")]
    return [[item.get(f) for f in fields] for item in data_list]


def _merge_to_period(
    daily_data: List[Dict[str, Any]], frequency: str
) -> List[Dict[str, Any]]:
    """日 K 聚合为周 K（1w）/ 月 K（1M）。移植自 stock_sdk。"""
    if not daily_data:
        return []
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for item in daily_data:
        date_val = item.get("date")
        if not date_val:
            continue
        date_str = str(date_val)
        try:
            dt = datetime.datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            continue
        if frequency == "1w":
            iso = dt.isocalendar()
            key = (iso[0], iso[1])
        else:
            key = (dt.year, dt.month)
        grouped.setdefault(key, []).append(item)

    merged_list: List[Dict[str, Any]] = []
    for key in sorted(grouped.keys()):
        items = grouped[key]
        first_item = items[0]
        last_item = items[-1]

        def valid_values(field: str) -> List[Any]:
            return [x[field] for x in items if x.get(field) is not None]

        open_values = valid_values("open")
        high_values = valid_values("high")
        low_values = valid_values("low")
        close_values = valid_values("close")
        if not (open_values and high_values and low_values and close_values):
            continue
        high = max(high_values)
        low = min(low_values)
        volume_values = valid_values("volume")
        amount_values = valid_values("amount")
        volume = sum(volume_values) if volume_values else None
        amount = sum(amount_values) if amount_values else None

        merged_item: Dict[str, Any] = {
            "date": last_item["date"],
            "code": last_item["code"],
            "name": last_item.get("name", ""),
            "open": open_values[0],
            "high": high,
            "low": low,
            "close": close_values[-1],
            "volume": volume,
            "amount": amount,
        }
        if merged_list:
            pre_close = merged_list[-1]["close"]
        else:
            pre_close = first_item.get("pre_close") or open_values[0]
        merged_item["pre_close"] = pre_close
        if pre_close:
            merged_item["pct_chg"] = round(
                ((merged_item["close"] - pre_close) / pre_close) * 100, 3
            )
            merged_item["amplitude"] = round(((high - low) / pre_close) * 100, 3)
        else:
            merged_item["pct_chg"] = 0.0
            merged_item["amplitude"] = 0.0
        turnover_values = valid_values("turnover")
        if turnover_values:
            merged_item["turnover"] = round(sum(turnover_values), 3)
        vol_ratio_values = valid_values("vol_ratio")
        if vol_ratio_values:
            merged_item["vol_ratio"] = round(
                sum(vol_ratio_values) / len(vol_ratio_values), 3
            )
        for field in (
            "pe_ttm",
            "pb",
            "total_mv",
            "float_mv",
            "float_share",
            "total_share",
            "is_st",
        ):
            if field in last_item:
                merged_item[field] = last_item[field]
        merged_list.append(merged_item)
    return merged_list


def _trading_elapsed(minute_of_day: int) -> Optional[int]:
    if 570 <= minute_of_day <= 690:
        return minute_of_day - 570
    if 780 <= minute_of_day <= 900:
        if minute_of_day == 780:
            return 121
        return 120 + (minute_of_day - 780)
    return None


def _elapsed_to_minute_of_day(elapsed: int) -> int:
    if elapsed <= 120:
        return 570 + elapsed
    if elapsed > 240:
        elapsed = 240
    return 780 + (elapsed - 120)


def _merge_minutes_to_period(
    minute_data: List[Dict[str, Any]], frequency: str
) -> List[Dict[str, Any]]:
    """1m 聚合为 5m/15m/30m/60m。移植自 stock_sdk。"""
    if not minute_data:
        return []
    interval = int(frequency[:-1])
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for item in minute_data:
        date_val = item.get("date")
        if not date_val:
            continue
        try:
            date_int = int(date_val)
        except (TypeError, ValueError):
            continue
        if date_int < 10000000000000:
            continue
        ymd = date_int // 1000000
        hour = (date_int // 10000) % 100
        minute = (date_int // 100) % 100
        elapsed = _trading_elapsed(hour * 60 + minute)
        if elapsed is None:
            continue
        if elapsed <= 0:
            group_end_elapsed = interval
        else:
            group_idx = (elapsed - 1) // interval
            group_end_elapsed = (group_idx + 1) * interval
        grouped.setdefault((ymd, group_end_elapsed), []).append(item)

    merged_list: List[Dict[str, Any]] = []
    for idx, (key, items) in enumerate(sorted(grouped.items())):
        ymd, end_elapsed = key
        first_item = items[0]
        last_item = items[-1]
        high = max(x["high"] for x in items if "high" in x)
        low = min(x["low"] for x in items if "low" in x)
        volume = sum(x["volume"] for x in items if "volume" in x)
        amount = sum(x["amount"] for x in items if "amount" in x)
        end_minute_of_day = _elapsed_to_minute_of_day(end_elapsed)
        end_hour = end_minute_of_day // 60
        end_minute = end_minute_of_day % 60
        if end_hour >= 24:
            end_hour = 23
            end_minute = 59
        aligned_date_int = ymd * 1000000 + end_hour * 10000 + end_minute * 100
        merged_item: Dict[str, Any] = {
            "date": aligned_date_int,
            "code": last_item["code"],
            "name": last_item.get("name", ""),
            "open": first_item["open"],
            "high": high,
            "low": low,
            "close": last_item["close"],
            "volume": volume,
            "amount": amount,
        }
        if idx > 0:
            pre_close = merged_list[-1]["close"]
        else:
            pre_close = first_item.get("pre_close", first_item["open"])
        merged_item["pre_close"] = pre_close
        if pre_close:
            merged_item["pct_chg"] = round(
                ((merged_item["close"] - pre_close) / pre_close) * 100, 3
            )
            merged_item["amplitude"] = round(((high - low) / pre_close) * 100, 3)
        else:
            merged_item["pct_chg"] = 0.0
            merged_item["amplitude"] = 0.0
        for field in (
            "vol_ratio",
            "pe_ttm",
            "pb",
            "total_mv",
            "float_mv",
            "float_share",
            "total_share",
            "is_st",
        ):
            if field in last_item:
                merged_item[field] = last_item[field]
        merged_list.append(merged_item)
    return merged_list


class LightStockDBClient:
    """底层 pyd 的轻量封装：get/vals/keys/pipe 透传 + get_data 对齐 stock_sdk。"""

    def __init__(self, raw_rd: Any) -> None:
        self._rd = raw_rd

    # ---------- 底层透传 ----------
    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._rd.get(*args, **kwargs)

    def vals(self, *args: Any, **kwargs: Any) -> Any:
        return self._rd.vals(*args, **kwargs)

    def keys(self, *args: Any, **kwargs: Any) -> Any:
        return self._rd.keys(*args, **kwargs)

    def pipe(self) -> Any:
        return self._rd.pipe()

    def close(self) -> Any:
        closer = getattr(self._rd, "close", None)
        if callable(closer):
            return closer()
        return None

    # ---------- get_data ----------
    def get_data(
        self,
        code: Union[str, List[str]],
        start: Optional[str] = None,
        end: Optional[str] = None,
        frequency: str = "1d",
        fields: Optional[Union[str, List[str]]] = None,
        fq: Optional[str] = None,
        limit: Optional[int] = None,
        desc: bool = False,
        as_df: bool = False,
    ) -> Any:
        """与 stock_sdk.rd.get_data 对齐（fq 由 provider 手动处理，此处忽略）。"""
        _ = fq
        is_batch = isinstance(code, list)
        codes = code if is_batch else [code]
        table = (
            _TABLE_MINUTE
            if frequency in ("1m", "5m", "15m", "30m", "60m")
            else _TABLE_DAILY
        )
        requires_aggregation = frequency in (
            "5m",
            "15m",
            "30m",
            "60m",
            "1w",
            "1M",
        )
        retrieval_desc = desc and not requires_aggregation
        if frequency in ("1m", "5m", "15m", "30m", "60m"):
            if start and len(start) == 8:
                start = start + "000000"
            if end and len(end) == 8:
                end = end + "235959"
        time_query = _build_time_query(start, end, retrieval_desc)

        data_dict: Dict[str, List[Dict[str, Any]]] = {}
        if len(codes) == 1:
            single_code = codes[0]
            res = self._rd.vals(table, single_code, time_query)
            data_dict[single_code] = list(res)
        else:
            pp = self._rd.pipe()
            for c in codes:
                pp.mget(table, c, time_query)
            raw = pp.do()
            if not isinstance(raw, list):
                raw = [raw]
            for c, items in zip(codes, raw):
                if isinstance(items, dict):
                    data_dict[c] = [items]
                elif isinstance(items, list):
                    data_dict[c] = [
                        item[1]
                        for item in items
                        if isinstance(item, (list, tuple)) and len(item) > 1
                    ]
                else:
                    data_dict[c] = []

        for c in codes:
            records = [
                r for r in data_dict[c] if isinstance(r, dict)
            ]
            if frequency in ("1w", "1M"):
                records = _merge_to_period(records, frequency)
            elif frequency in ("5m", "15m", "30m", "60m"):
                records = _merge_minutes_to_period(records, frequency)
            if desc and requires_aggregation:
                records = records[::-1]
            if limit is not None:
                records = records[:limit]
            if fields:
                records = _filter_fields(records, fields)
            data_dict[c] = records

        if is_batch:
            if as_df:
                import pandas as pd

                return self._to_dataframe(data_dict, fields)
            return data_dict
        single = data_dict[codes[0]]
        if as_df:
            import pandas as pd

            if single and isinstance(single[0], list):
                field_list = (
                    [f.strip() for f in fields.split(",")]
                    if isinstance(fields, str)
                    else list(fields or [])
                )
                return pd.DataFrame(single, columns=field_list)
            return pd.DataFrame(single)
        return single

    @staticmethod
    def _to_dataframe(
        data_dict: Dict[str, List[Any]], fields: Optional[Union[str, List[str]]]
    ) -> Any:
        import pandas as pd

        field_list = None
        if fields:
            field_list = (
                [f.strip() for f in fields.split(",")]
                if isinstance(fields, str)
                else list(fields)
            )
        all_records: List[Dict[str, Any]] = []
        for code, records in data_dict.items():
            for r in records:
                if isinstance(r, list):
                    record_dict = dict(zip(field_list or [], r))
                else:
                    record_dict = dict(r)
                record_dict["code"] = code
                all_records.append(record_dict)
        if not all_records:
            return pd.DataFrame()
        df = pd.DataFrame(all_records)
        cols = ["code"] + [col for col in df.columns if col != "code"]
        return df[cols]
