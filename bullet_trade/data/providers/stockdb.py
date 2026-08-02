"""
StockDB 本地数据源 Provider

通过 stockdb（本地 LevelDB 服务，默认 127.0.0.1:7899）提供聚宽兼容的行情接口：
- 股票/ETF/基金 日线、分钟线（1m/5m/15m/30m/60m）、周线、月线
- 交易日历（由一组长历史锚点股票日 K 并集推导，经磁盘缓存）
- 复权（前/后复权、factor、pre_factor_ref_date 动态前复权）
- 证券列表（股票代码 表 + 最新交易日快照名称）

v1 范围之外的扩展数据（指数 K 线、财务、板块、tick、龙虎榜等）保持
DataProvider 基类的 NotImplementedError，不伪造数据。
"""

from __future__ import annotations

import bisect
import importlib
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)

_TABLE_DAILY = "日k"
_TABLE_MINUTE = "分钟k"
_TABLE_FACTOR = "复权"
_TABLE_CODES = "股票代码"

_DEFAULT_PRICE_FIELDS = ["open", "close", "high", "low", "volume", "money"]
_PRICE_SCALE_FIELDS = {"open", "high", "low", "close", "pre_close", "avg", "price"}
_DEFAULT_START = "20050101"
_DEFAULT_END = "29991231"
_FACTOR_RANGE = "19900101<29991231"
_ANCHOR_CODES = ["600000", "600519", "601398", "000001", "000002", "000651"]

_SUFFIX_TO_MARKET = {
    "XSHE": "XSHE",
    "SZ": "XSHE",
    "XSHG": "XSHG",
    "SH": "XSHG",
    "XBJG": "XBJG",
    "BJ": "XBJG",
    "BSE": "XBJG",
}

_FREQUENCY_ALIASES = {
    "daily": "1d",
    "1d": "1d",
    "d": "1d",
    "day": "1d",
    "minute": "1m",
    "1m": "1m",
    "m1": "1m",
    "1min": "1m",
    "weekly": "1w",
    "monthly": "1M",
}


class StockDBProvider(DataProvider):
    """基于 stockdb 本地服务的聚宽兼容数据源。"""

    name: str = "stockdb"
    requires_live_data: bool = False

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self._host = str(self.config.get("host") or os.getenv("STOCKDB_HOST") or "127.0.0.1")
        self._port = int(self.config.get("port") or os.getenv("STOCKDB_PORT") or 7899)
        self._timeout = float(
            self.config.get("timeout") or os.getenv("STOCKDB_TIMEOUT") or 15.0
        )
        self._auto_start = self._parse_bool(
            self.config.get("auto_start", os.getenv("STOCKDB_AUTO_START", "true"))
        )
        self._minute_daily_fallback = self._parse_bool(
            self.config.get(
                "minute_daily_fallback",
                os.getenv("STOCKDB_MINUTE_DAILY_FALLBACK", "true"),
            )
        )
        self._exe: Optional[str] = self.config.get("exe") or os.getenv("STOCKDB_EXE")
        self._sdk_dir: Optional[str] = self.config.get("sdk_dir") or os.getenv(
            "STOCKDB_SDK_DIR"
        )
        if not self._sdk_dir:
            self._sdk_dir = self._detect_sdk_dir()
        # 测试注入：直接提供 rd 客户端或 stock_sdk 模块
        self._rd: Optional[Any] = self.config.get("rd")
        self._sdk: Optional[Any] = self.config.get("sdk_module")
        self._proc: Optional[subprocess.Popen] = None
        self._factor_cache: Dict[str, Dict[int, float]] = {}
        self._calendar_cache: Optional[List[datetime]] = None
        self._security_rows_cache: Optional[List[Dict[str, Any]]] = None
        cache_dir = self.config.get("cache_dir")
        use_env_cache = "cache_dir" not in self.config
        try:
            from ..cache import CacheManager

            self._cache = CacheManager(
                provider_name=self.name,
                cache_dir=cache_dir,
                fallback_to_env=use_env_cache,
            )
        except Exception:
            self._cache = None

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _as_list(security: Union[str, Sequence[str]]) -> List[str]:
        if isinstance(security, str):
            return [security]
        if security is None:
            return []
        return [str(item) for item in security]

    @staticmethod
    def _detect_sdk_dir() -> Optional[str]:
        """自动探测仓库同级 stockdb/pybao 目录。"""
        repo_root = Path(__file__).resolve().parents[2]
        candidates = [
            repo_root.parent / "stockdb" / "pybao",
            repo_root / "stockdb" / "pybao",
        ]
        for candidate in candidates:
            if (candidate / "stock_sdk.py").exists():
                return str(candidate)
        return None

    @staticmethod
    def jq_to_stockdb(security: str) -> str:
        """聚宽/简写代码 -> stockdb 6 位代码。"""
        sec = str(security).strip()
        code = sec.split(".", 1)[0]
        code = "".join(ch for ch in code if ch.isdigit())
        if len(code) > 6:
            code = code[-6:]
        return code.zfill(6)

    @staticmethod
    def _infer_suffix(code6: str) -> str:
        """按代码段推断交易所后缀。"""
        if code6.startswith(("6", "5")):
            return "XSHG"
        if code6.startswith(("4", "8", "9")):
            return "XBJG"
        return "XSHE"

    @staticmethod
    def _bucket_suffix(bucket: str, code6: str) -> str:
        bucket = str(bucket)
        if bucket in ("4", "8", "9"):
            return "XBJG"
        if bucket in ("5", "6"):
            return "XSHG"
        if bucket in ("0", "1", "2", "3"):
            return "XSHE"
        return StockDBProvider._infer_suffix(code6)

    @classmethod
    def _resolve_code(cls, security: str) -> Optional[str]:
        """返回 stockdb 6 位代码；后缀与代码段不匹配（如指数）时返回 None。"""
        sec = str(security).strip()
        code6 = cls.jq_to_stockdb(sec)
        if "." in sec:
            suffix = sec.rsplit(".", 1)[-1].upper()
            suffix = _SUFFIX_TO_MARKET.get(suffix)
            if suffix is not None and suffix != cls._infer_suffix(code6):
                return None
        return code6

    @staticmethod
    def _infer_security_type(code6: str) -> str:
        if code6.startswith(("6", "0", "2", "3", "4", "8", "9")):
            return "stock"
        if code6.startswith(("159", "5")):
            return "etf"
        if code6.startswith("16"):
            return "lof"
        return "fund"

    @staticmethod
    def _normalize_frequency(frequency: str) -> str:
        f = str(frequency or "daily").strip().lower()
        return _FREQUENCY_ALIASES.get(f, f)

    @staticmethod
    def _to_ymd(value: Optional[Union[str, datetime, Date]]) -> Optional[str]:
        if value is None:
            return None
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.strftime("%Y%m%d")

    @staticmethod
    def _to_stockdb_ts(value: Optional[Union[str, datetime, Date]]) -> Optional[str]:
        """分钟线查询时间戳：带时间用 14 位，纯日期用 8 位。"""
        if value is None:
            return None
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        if ts.hour == 0 and ts.minute == 0 and ts.second == 0:
            return ts.strftime("%Y%m%d")
        return ts.strftime("%Y%m%d%H%M%S")

    @staticmethod
    def _format_timestamp(value: Optional[Any]) -> Optional[pd.Timestamp]:
        if value is None:
            return None
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return pd.Timestamp(parsed)

    # ------------------------------------------------------------------
    # 连接与服务拉起
    # ------------------------------------------------------------------
    def _port_open(self, host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _resolve_exe(self) -> Optional[str]:
        candidates: List[str] = []
        if self._exe:
            candidates.append(self._exe)
        env_exe = os.getenv("STOCKDB_EXE")
        if env_exe:
            candidates.append(env_exe)
        repo_root = Path(__file__).resolve().parents[2]
        candidates.append(str(repo_root.parent / "stockdb" / "stockdb.exe"))
        candidates.append(str(repo_root / "stockdb" / "stockdb.exe"))
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None

    def _start_server(self) -> None:
        exe = self._resolve_exe()
        if not exe:
            raise RuntimeError(
                "未找到 stockdb.exe，请设置 STOCKDB_EXE 环境变量，或先手动启动 stockdb.exe"
            )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            [exe],
            cwd=str(Path(exe).parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def _wait_port(self, host: str, port: int, timeout: Optional[float] = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        while time.monotonic() < deadline:
            if self._port_open(host, port, timeout=0.5):
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def _import_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        if self._sdk_dir and str(self._sdk_dir) not in sys.path:
            sys.path.insert(0, str(self._sdk_dir))
        try:
            self._sdk = importlib.import_module("stock_sdk")
        except Exception as exc:
            raise RuntimeError(
                "无法导入 stock_sdk（stockdb Python SDK）。"
                "请确认 STOCKDB_SDK_DIR 指向 stockdb/pybao 目录"
                "（可先运行一次 stockdb/pybao/安装.py 写入 .pth）。"
                f"原始错误: {exc}"
            ) from exc
        return self._sdk

    def _ensure_rd(self) -> Any:
        if self._rd is None:
            self.auth()
        return self._rd

    def auth(
        self,
        user: Optional[str] = None,
        pwd: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        """连接 stockdb 服务；未运行时按配置自动拉起 stockdb.exe。"""
        _ = user, pwd
        if self._rd is not None:
            return
        target_host = host or self._host
        target_port = int(port or self._port)
        if not self._port_open(target_host, target_port):
            if self._auto_start:
                self._start_server()
                if not self._wait_port(target_host, target_port):
                    raise RuntimeError(
                        f"stockdb 服务启动超时（{target_host}:{target_port}），"
                        "请检查 stockdb.exe 是否可运行、端口是否被占用"
                    )
            else:
                raise RuntimeError(
                    f"stockdb 服务未运行（{target_host}:{target_port}）。"
                    "请先双击 stockdb/stockdb.exe 启动，"
                    "或设置 STOCKDB_AUTO_START=true 让 provider 自动拉起"
                )
        module = self._import_sdk()
        rd = getattr(module, "rd", None)
        if rd is None:
            raise RuntimeError("stock_sdk 模块未暴露 rd 客户端对象")
        self._rd = rd

    # ------------------------------------------------------------------
    # 复权因子
    # ------------------------------------------------------------------
    def _factor_records(self, code6: str) -> List[Tuple[int, Dict[str, Any]]]:
        """返回 [(YYYYMMDD, {div,give,trans,mult,cum}), ...] 升序。"""
        raw = self._rd.get(_TABLE_FACTOR, code6, _FACTOR_RANGE)
        if raw is None:
            return []
        records: List[Tuple[int, Dict[str, Any]]] = []
        if "cum" in raw:
            # 单条记录：get 直接返回字段字典，日期需从 keys 补取
            keys = list(self._rd.keys(_TABLE_FACTOR, code6, _FACTOR_RANGE))
            if keys:
                try:
                    date_int = int(str(keys[0]).rsplit(":", 1)[-1])
                    records.append((date_int, dict(raw)))
                except (ValueError, TypeError):
                    pass
        else:
            # 多条记录：[key, value] 对，key 形如 "复权:code:YYYYMMDD"
            for item in list(raw or []):
                if not (isinstance(item, (list, tuple)) and len(item) >= 2):
                    continue
                key, value = item[0], item[1]
                if not (isinstance(value, dict) and "cum" in value):
                    continue
                try:
                    date_int = int(str(key).rsplit(":", 1)[-1])
                    records.append((date_int, value))
                except (ValueError, TypeError):
                    continue
        records.sort(key=lambda pair: pair[0])
        return records

    def _factor_map(self, code6: str) -> Dict[int, float]:
        cached = self._factor_cache.get(code6)
        if cached is not None:
            return cached
        try:
            factor_map = {
                date_int: float(rec.get("cum") or 1.0)
                for date_int, rec in self._factor_records(code6)
            }
        except Exception as exc:
            logger.warning("读取 stockdb 复权因子失败 [%s]: %s", code6, exc)
            return {}
        if factor_map:
            self._factor_cache[code6] = factor_map
        return factor_map

    @staticmethod
    def _factor_series(factor_map: Dict[int, float], date_ints: List[int]) -> List[float]:
        dates = sorted(factor_map)
        if not dates:
            return [1.0] * len(date_ints)
        values: List[float] = []
        for d in date_ints:
            idx = bisect.bisect_right(dates, d) - 1
            values.append(factor_map[dates[idx]] if idx >= 0 else 1.0)
        return values

    def _resolve_factor_ref(
        self, factor_map: Dict[int, float], ref_date: Optional[Union[str, datetime, Date]]
    ) -> float:
        if not factor_map:
            return 1.0
        if ref_date is None:
            return factor_map[max(factor_map)]
        ref_ymd = self._to_ymd(ref_date)
        if ref_ymd is None:
            return factor_map[max(factor_map)]
        ref_int = int(ref_ymd)
        dates = sorted(factor_map)
        idx = bisect.bisect_right(dates, ref_int) - 1
        return factor_map[dates[idx]] if idx >= 0 else 1.0

    # ------------------------------------------------------------------
    # get_price
    # ------------------------------------------------------------------
    def _records_to_frame(
        self,
        records: List[Dict[str, Any]],
        code6: str,
        requested_fields: List[str],
        *,
        fq_norm: str,
        factor_map: Dict[int, float],
        factor_ref: float,
    ) -> pd.DataFrame:
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        if "date" not in df.columns:
            return pd.DataFrame()
        date_str = df["date"].astype(str)
        if (date_str.str.len() == 14).any():
            df["dt"] = pd.to_datetime(date_str, format="%Y%m%d%H%M%S", errors="coerce")
        else:
            df["dt"] = pd.to_datetime(date_str, format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["dt"]).set_index("dt").sort_index()
        if df.empty:
            return pd.DataFrame()
        df.index.name = "time"

        out = pd.DataFrame(index=df.index)
        for field in requested_fields:
            if field == "date":
                continue
            if field == "factor":
                continue
            if field == "paused":
                out[field] = False
            elif field in ("high_limit", "low_limit"):
                out[field] = 0.0
            elif field == "money":
                out[field] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0.0)
            elif field == "avg":
                volume = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0.0)
                amount = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0.0)
                out[field] = amount / volume.replace(0.0, pd.NA)
                out[field] = pd.to_numeric(out[field], errors="coerce").fillna(0.0)
            elif field == "price":
                out[field] = pd.to_numeric(df.get("close"), errors="coerce").fillna(0.0)
            elif field == "name":
                out[field] = df.get("name", pd.Series(index=df.index)).astype(str)
            elif field in df.columns:
                out[field] = pd.to_numeric(df[field], errors="coerce")
                if field in ("is_st",):
                    out[field] = out[field].fillna(False).astype(bool)
                else:
                    out[field] = out[field].fillna(0.0)
            else:
                out[field] = 0.0

        needs_scale = fq_norm in ("pre", "qfq", "post", "hfq") and factor_map
        if needs_scale:
            date_ints = [int(str(v)[:8]) for v in df.index.strftime("%Y%m%d")]
            factor_values = self._factor_series(factor_map, date_ints)
            factor_series = pd.Series(factor_values, index=df.index, dtype="float64")
            if fq_norm in ("pre", "qfq"):
                multiplier = factor_series / (factor_ref or 1.0)
            else:
                multiplier = factor_series
            decimals = 3 if code6.startswith(("1", "5")) else 2
            for field in _PRICE_SCALE_FIELDS:
                if field in out.columns:
                    scaled = pd.to_numeric(out[field], errors="coerce") * multiplier
                    out[field] = scaled.round(decimals).fillna(0.0)

        if "factor" in requested_fields:
            if factor_map:
                date_ints = [int(str(v)[:8]) for v in df.index.strftime("%Y%m%d")]
                out["factor"] = pd.Series(
                    self._factor_series(factor_map, date_ints), index=df.index, dtype="float64"
                )
            else:
                out["factor"] = 1.0
        return out

    def _fetch_price_for_security(
        self,
        code6: str,
        *,
        start_q: Optional[str],
        end_q: Optional[str],
        frequency: str,
        count: Optional[int],
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "code": code6,
            "frequency": frequency,
            "fq": None,
        }
        if count is not None:
            kwargs.update({"start": start_q, "end": end_q, "desc": True, "limit": count})
        else:
            kwargs.update({"start": start_q, "end": end_q, "desc": False})
        try:
            records = self._rd.get_data(**kwargs)
        except Exception as exc:
            logger.warning("stockdb get_data 失败 [%s]: %s", code6, exc)
            return []
        if records is None:
            return []
        if not isinstance(records, list):
            records = list(records)
        if count is not None:
            records = list(reversed(records))
        return records

    @staticmethod
    def _bounded_start_for_count(
        end_q: Optional[str], frequency: str, count: Optional[int]
    ) -> Optional[str]:
        """count 查询改为有界窗口，避免 SDK 对全历史倒序扫描（约 250ms/次）。

        stockdb 的 desc+limit 查询在 LevelDB 上会先做整段反向扫描再截断；
        按频率与 count 反推一个足够宽的起始日期，可将单次查询降到毫秒级。
        """
        if end_q is None or count is None:
            return None
        try:
            end_ts = pd.to_datetime(str(end_q)[:8], format="%Y%m%d")
        except (ValueError, TypeError):
            return None
        if pd.isna(end_ts):
            return None
        n = max(int(count), 1)
        if frequency in ("1m", "5m", "15m", "30m", "60m"):
            buffer_days = 45
        elif frequency == "1w":
            buffer_days = max(n * 8 + 12, 30)
        elif frequency == "1M":
            buffer_days = max(n * 32 + 12, 60)
        else:
            buffer_days = max(n * 3 + 10, 15)
        start_ts = end_ts - pd.Timedelta(days=buffer_days)
        if start_ts.year < 1990:
            start_ts = pd.Timestamp("1990-01-01")
        base = start_ts.strftime("%Y%m%d")
        if frequency in ("1m", "5m", "15m", "30m", "60m") and len(str(end_q)) == 14:
            return base + "000000"
        return base

    def get_price(
        self,
        security: Union[str, List[str]],
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        frequency: str = "daily",
        fields: Optional[List[str]] = None,
        skip_paused: bool = False,
        fq: str = "pre",
        count: Optional[int] = None,
        panel: bool = True,
        fill_paused: bool = True,
        pre_factor_ref_date: Optional[Union[str, datetime]] = None,
        prefer_engine: bool = False,
        force_no_engine: bool = False,
    ) -> pd.DataFrame:
        _ = skip_paused, fill_paused, prefer_engine, force_no_engine
        securities = self._as_list(security)
        if not securities:
            return pd.DataFrame()
        self._ensure_rd()

        requested_fields = list(fields or _DEFAULT_PRICE_FIELDS)
        requested_fields = [f for f in requested_fields if f != "date"]
        frequency_norm = self._normalize_frequency(frequency)
        fq_norm = str(fq or "").lower().strip() or "none"
        manual_fq = fq_norm in ("pre", "qfq", "post", "hfq")
        needs_factor = "factor" in requested_fields

        is_minute = frequency_norm in ("1m", "5m", "15m", "30m", "60m")
        if is_minute:
            start_q = self._to_stockdb_ts(start_date)
            end_q = self._to_stockdb_ts(end_date)
        else:
            start_q = self._to_ymd(start_date)
            end_q = self._to_ymd(end_date)
        if end_q is None:
            end_q = datetime.now().strftime("%Y%m%d")
        if count is not None:
            start_q = self._bounded_start_for_count(end_q, frequency_norm, count)
            if start_q is None:
                start_q = "19900101"
        elif start_q is None:
            start_q = _DEFAULT_START

        frames: Dict[str, pd.DataFrame] = {}
        for sec in securities:
            code6 = self._resolve_code(sec)
            if code6 is None:
                logger.warning(
                    "stockdb 不包含该代码段对应的市场数据（可能是指数）: %s", sec
                )
                continue
            factor_map = self._factor_map(code6) if (manual_fq or needs_factor) else {}
            factor_ref = 1.0
            if manual_fq and fq_norm in ("pre", "qfq"):
                factor_ref = self._resolve_factor_ref(factor_map, pre_factor_ref_date)
                if not factor_map and pre_factor_ref_date is not None:
                    raise NotImplementedError(
                        "stockdb 无该标的复权因子，无法执行 pre_factor_ref_date 动态前复权: "
                        + sec
                    )
            records = self._fetch_price_for_security(
                code6,
                start_q=start_q,
                end_q=end_q,
                frequency=frequency_norm,
                count=count,
            )
            if (
                not records
                and is_minute
                and count is not None
                and count == 1
                and self._minute_daily_fallback
                and end_q is not None
            ):
                # stockdb 分钟线覆盖不完整；对 count=1 的“当前行情探测”
                # 回退到当日日线近似（日频回测的引擎 current_data 场景）。
                logger.debug(
                    "stockdb 分钟线在 %s 无数据，回退到当日日线近似（count=1 探测）: %s",
                    str(end_q)[:8],
                    sec,
                )
                records = self._fetch_price_for_security(
                    code6,
                    start_q=self._to_ymd(end_q),
                    end_q=self._to_ymd(end_q),
                    frequency="1d",
                    count=1,
                )
            if not records:
                continue
            frame = self._records_to_frame(
                records,
                code6,
                requested_fields,
                fq_norm=fq_norm if manual_fq else "none",
                factor_map=factor_map,
                factor_ref=factor_ref,
            )
            if not frame.empty:
                frames[sec] = frame
        return self._assemble_price_result(
            frames,
            fields=requested_fields,
            securities=securities,
            panel=panel,
        )

    @staticmethod
    def _assemble_price_result(
        frames: Dict[str, pd.DataFrame],
        *,
        fields: List[str],
        securities: List[str],
        panel: bool,
    ) -> pd.DataFrame:
        """按聚宽兼容 shape 组装单标的、多标的、panel 与长表。"""
        if not frames:
            if panel and len(securities) == 1:
                return pd.DataFrame(columns=fields)
            return pd.DataFrame()
        if len(securities) == 1 and panel:
            return frames.get(securities[0], pd.DataFrame())
        if panel:
            parts = []
            for field in fields:
                field_frames = []
                for security in securities:
                    df = frames.get(security)
                    if df is None or df.empty:
                        continue
                    series = (
                        df[field]
                        if field in df.columns
                        else pd.Series(index=df.index, dtype="float64")
                    )
                    field_frames.append(series.rename(security))
                if field_frames:
                    parts.append(pd.concat(field_frames, axis=1))
                else:
                    parts.append(pd.DataFrame(columns=securities))
            wide = pd.concat(parts, axis=1, keys=fields)
            wide.columns.names = ["field", "code"]
            wide.sort_index(inplace=True)
            return wide
        rows = []
        for security in securities:
            df = frames.get(security)
            if df is None or df.empty:
                continue
            part = df.copy().reset_index()
            if "time" not in part.columns:
                part.rename(columns={part.columns[0]: "time"}, inplace=True)
            part["code"] = security
            for field in fields:
                if field not in part.columns:
                    part[field] = False if field == "paused" else 0.0
            rows.append(part[["time", "code"] + fields])
        if not rows:
            return pd.DataFrame()
        return (
            pd.concat(rows, ignore_index=True).sort_values(["time", "code"]).reset_index(drop=True)
        )

    def get_bars(
        self,
        security: Union[str, List[str]],
        count: int,
        unit: str = "1d",
        fields: Optional[List[str]] = None,
        include_now: bool = False,
        end_dt: Optional[Union[str, datetime]] = None,
        fq_ref_date: Union[int, datetime] = 1,
        df: bool = False,
    ) -> Any:
        """通过 get_price 提供聚宽 get_bars 兼容入口。"""
        _ = include_now, fq_ref_date
        request_fields = [
            field for field in (fields or _DEFAULT_PRICE_FIELDS) if field != "date"
        ]
        frequency = "daily" if str(unit).lower() in {"1d", "d", "day", "daily"} else unit
        result = self.get_price(
            security=security,
            end_date=end_dt,
            frequency=frequency,
            fields=request_fields,
            count=count,
            fq="pre",
            panel=df,
        )
        if df or not isinstance(result, pd.DataFrame):
            return result
        return result.to_dict()

    # ------------------------------------------------------------------
    # 交易日历 / 证券列表 / 除权除息
    # ------------------------------------------------------------------
    def _fetch_calendar(self) -> List[datetime]:
        """以一组锚点股票日 K 日期并集近似 A 股交易日历。"""
        end8 = datetime.now().strftime("%Y%m%d")
        dates: set = set()
        for code in _ANCHOR_CODES:
            try:
                records = self._rd.get_data(
                    code=code,
                    start=_DEFAULT_START,
                    end=end8,
                    frequency="1d",
                    fields="date",
                    fq=None,
                )
            except Exception as exc:
                logger.warning("读取锚点股票交易日失败 [%s]: %s", code, exc)
                continue
            for row in records or []:
                if isinstance(row, (list, tuple)) and row:
                    try:
                        dates.add(int(row[0]))
                    except (TypeError, ValueError):
                        continue
                elif isinstance(row, dict) and row.get("date") is not None:
                    try:
                        dates.add(int(row["date"]))
                    except (TypeError, ValueError):
                        continue
        return sorted(pd.to_datetime(str(d), format="%Y%m%d") for d in dates)

    def _load_calendar(self) -> List[datetime]:
        if self._calendar_cache is not None:
            return self._calendar_cache
        if self._cache is not None:
            try:
                days = self._cache.cached_call(
                    "get_trade_days",
                    {"start_date": None, "end_date": None, "count": None},
                    lambda kwargs: self._fetch_calendar(),
                    result_type="list_date",
                )
            except Exception:
                days = self._fetch_calendar()
        else:
            days = self._fetch_calendar()
        self._calendar_cache = list(days)
        return self._calendar_cache

    def get_trade_days(
        self,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        count: Optional[int] = None,
    ) -> List[datetime]:
        days = list(self._load_calendar())
        start_ts = self._format_timestamp(start_date)
        end_ts = self._format_timestamp(end_date)
        if start_ts is not None:
            days = [d for d in days if pd.Timestamp(d) >= start_ts]
        if end_ts is not None:
            days = [d for d in days if pd.Timestamp(d) <= end_ts]
        if count is not None:
            days = days[-int(count):]
        return [pd.to_datetime(d) for d in days]

    def get_trade_day(
        self,
        security: Union[str, List[str]],
        query_dt: Union[str, datetime],
    ) -> Any:
        """返回 query_dt 当日或之后的第一个交易日。"""
        _ = security
        ts = pd.Timestamp(query_dt).normalize()
        for day in self._load_calendar():
            if pd.Timestamp(day) >= ts:
                return day.date() if hasattr(day, "date") else day
        return None

    def _latest_trade_date(self) -> Optional[str]:
        days = self._load_calendar()
        if not days:
            return None
        return pd.Timestamp(days[-1]).strftime("%Y%m%d")

    def _fetch_names_on_latest_day(self) -> Dict[str, str]:
        latest8 = self._latest_trade_date()
        if latest8 is None:
            return {}
        try:
            records = list(self._rd.vals(_TABLE_DAILY, "*", latest8))
        except Exception as exc:
            logger.warning("读取 stockdb 最新交易日证券快照失败: %s", exc)
            return {}
        names: Dict[str, str] = {}
        for record in records:
            if isinstance(record, dict) and record.get("code"):
                code6 = str(record["code"])
                names[code6] = str(record.get("name") or code6)
        return names

    def _fetch_security_rows(self) -> List[Dict[str, Any]]:
        codes_res = self._rd.get(_TABLE_CODES)
        buckets = dict(codes_res) if not isinstance(codes_res, dict) else codes_res
        names = self._fetch_names_on_latest_day()
        rows: List[Dict[str, Any]] = []
        for bucket, codes in (buckets or {}).items():
            for code6 in codes or []:
                code6 = str(code6)
                jq_code = f"{code6}.{self._bucket_suffix(bucket, code6)}"
                name = names.get(code6, code6)
                rows.append(
                    {
                        "code": jq_code,
                        "display_name": name,
                        "name": name,
                        "start_date": Date(2005, 1, 1),
                        "end_date": Date(2200, 1, 1),
                        "type": self._infer_security_type(code6),
                    }
                )
        return rows

    def _load_security_rows(self) -> List[Dict[str, Any]]:
        if self._security_rows_cache is not None:
            return self._security_rows_cache
        if self._cache is not None:
            try:
                rows = self._cache.cached_call(
                    "get_all_securities",
                    {"types": "all", "date": None},
                    lambda kwargs: self._fetch_security_rows(),
                    result_type="list_dict",
                )
            except Exception:
                rows = self._fetch_security_rows()
        else:
            rows = self._fetch_security_rows()
        self._security_rows_cache = list(rows)
        return self._security_rows_cache

    def get_all_securities(
        self,
        types: Union[str, List[str]] = "stock",
        date: Optional[Union[str, datetime]] = None,
    ) -> pd.DataFrame:
        _ = date
        requested = {types} if isinstance(types, str) else set(types or [])
        requested = {str(item).lower() for item in requested} or {"stock"}
        rows = [
            row
            for row in self._load_security_rows()
            if "all" in requested or str(row.get("type", "")).lower() in requested
        ]
        if not rows:
            return pd.DataFrame(columns=["display_name", "name", "start_date", "end_date", "type"])
        df = pd.DataFrame(rows)
        return df.drop_duplicates(subset=["code"]).set_index("code")

    def get_security_info(
        self,
        security: str,
        date: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        _ = date
        code6 = self._resolve_code(security)
        if code6 is None:
            return {}
        name = code6
        try:
            latest8 = self._latest_trade_date()
            if latest8:
                record = self._rd.get(_TABLE_DAILY, code6, latest8)
                if record is not None and "name" in record:
                    name = str(dict(record).get("name") or code6)
        except Exception:
            pass
        return {
            "code": security,
            "display_name": name,
            "name": name,
            "start_date": Date(2005, 1, 1),
            "end_date": Date(2200, 1, 1),
            "type": self._infer_security_type(code6),
        }

    def get_split_dividend(
        self,
        security: str,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
    ) -> List[Dict[str, Any]]:
        """从 stockdb 复权表解析分红/送转事件。

        复权记录字段：div（每股现金分红）、give/trans（每 10 股送/转）。
        """
        code6 = self._resolve_code(security)
        if code6 is None:
            return []
        start8 = self._to_ymd(start_date)
        end8 = self._to_ymd(end_date)

        def _fetch(kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
            events: List[Dict[str, Any]] = []
            for date_int, rec in self._factor_records(code6):
                if start8 and date_int < int(start8):
                    continue
                if end8 and date_int > int(end8):
                    continue
                div = float(rec.get("div") or 0.0)
                give = float(rec.get("give") or 0.0)
                trans = float(rec.get("trans") or 0.0)
                if div <= 0 and give <= 0 and trans <= 0:
                    continue
                events.append(
                    {
                        "security": security,
                        "date": pd.to_datetime(str(date_int), format="%Y%m%d").date(),
                        "scale_factor": 1.0 + (give + trans) / 10.0,
                        "bonus_pre_tax": round(div * 10.0, 4),
                        "per_base": 10,
                    }
                )
            return events

        if self._cache is not None:
            try:
                return self._cache.cached_call(
                    "get_split_dividend",
                    {"security": security, "start_date": start_date, "end_date": end_date},
                    _fetch,
                    result_type="list_dict",
                )
            except Exception:
                pass
        return _fetch({})

    # ------------------------------------------------------------------
    # 扩展接口：v1 明确不支持，保持基类 NotImplementedError
    # ------------------------------------------------------------------
    def get_ticks(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_ticks")

    def get_current_tick(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_current_tick")

    def get_extras(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_extras")

    def get_fundamentals(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_fundamentals")

    def get_fundamentals_continuously(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_fundamentals_continuously")

    def get_index_weights(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_index_weights")

    def get_index_stocks(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_index_stocks")

    def get_industry_stocks(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_industry_stocks")

    def get_industry(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_industry")

    def get_concept_stocks(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_concept_stocks")

    def get_concept(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_concept")

    def get_fund_info(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_fund_info")

    def get_margincash_stocks(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_margincash_stocks")

    def get_marginsec_stocks(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_marginsec_stocks")

    def get_dominant_future(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_dominant_future")

    def get_future_contracts(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_future_contracts")

    def get_billboard_list(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_billboard_list")

    def get_locked_shares(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("stockdb provider 未实现 get_locked_shares")

    def subscribe_ticks(self, symbols: List[str]) -> None:
        return None

    def subscribe_markets(self, markets: List[str]) -> None:
        return None

    def unsubscribe_ticks(self, symbols: Optional[List[str]] = None) -> None:
        return None

    def unsubscribe_markets(self, markets: Optional[List[str]] = None) -> None:
        return None
