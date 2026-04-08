"""特殊数据获取 — 涨跌停 / 龙虎榜 / 资金流向 (AkShare)"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..logging_config import get_logger

logger = get_logger("data.special")


@dataclass
class LimitUpRecord:
    symbol: str
    name: str
    limit_up_price: float
    close_price: float
    turnover_rate: float = 0.0
    consecutive_days: int = 1
    first_limit_time: str = ""
    open_times: int = 0          # 炸板次数


@dataclass
class LimitDownRecord:
    symbol: str
    name: str
    limit_down_price: float
    close_price: float
    turnover_rate: float = 0.0


@dataclass
class DragonTigerData:
    symbol: str
    name: str
    date: str
    reason: str
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    net_amount: float = 0.0
    buy_seats: list[str] = field(default_factory=list)
    sell_seats: list[str] = field(default_factory=list)


@dataclass
class MoneyFlowRecord:
    symbol: str
    name: str
    date: str
    main_net_inflow: float = 0.0      # 主力净流入 (万元)
    super_large_net: float = 0.0      # 超大单净流入
    large_net: float = 0.0            # 大单净流入
    medium_net: float = 0.0           # 中单净流入
    small_net: float = 0.0            # 小单净流入


class SpecialDataFetcher:
    """特殊数据获取器 — 涨跌停/龙虎榜/资金流向"""

    def fetch_limit_up(self, trade_date: str | None = None) -> list[LimitUpRecord]:
        """获取涨停板数据"""
        dt = trade_date or date.today().strftime("%Y%m%d")
        try:
            import akshare as ak
            df = ak.stock_zt_pool_em(date=dt)
            if df is None or df.empty:
                return []
            records = []
            for _, row in df.iterrows():
                records.append(LimitUpRecord(
                    symbol=str(row.get("代码", "")),
                    name=str(row.get("名称", "")),
                    limit_up_price=float(row.get("涨停价", 0) or 0),
                    close_price=float(row.get("最新价", 0) or 0),
                    turnover_rate=float(row.get("换手率", 0) or 0),
                    consecutive_days=int(row.get("连板数", 1) or 1),
                    first_limit_time=str(row.get("首次封板时间", "") or ""),
                    open_times=int(row.get("炸板次数", 0) or 0),
                ))
            logger.info("涨停数据: %d 只 (%s)", len(records), dt)
            return records
        except Exception as e:
            logger.warning("涨停数据获取失败 (%s): %s", dt, e)
            return []

    def fetch_limit_down(self, trade_date: str | None = None) -> list[LimitDownRecord]:
        """获取跌停板数据"""
        dt = trade_date or date.today().strftime("%Y%m%d")
        try:
            import akshare as ak
            df = ak.stock_zt_pool_dtgc_em(date=dt)
            if df is None or df.empty:
                return []
            records = []
            for _, row in df.iterrows():
                records.append(LimitDownRecord(
                    symbol=str(row.get("代码", "")),
                    name=str(row.get("名称", "")),
                    limit_down_price=float(row.get("跌停价", 0) or 0),
                    close_price=float(row.get("最新价", 0) or 0),
                    turnover_rate=float(row.get("换手率", 0) or 0),
                ))
            logger.info("跌停数据: %d 只 (%s)", len(records), dt)
            return records
        except Exception as e:
            logger.warning("跌停数据获取失败 (%s): %s", dt, e)
            return []

    def fetch_dragon_tiger(self, trade_date: str | None = None) -> list[DragonTigerData]:
        """获取龙虎榜数据"""
        dt = trade_date or date.today().strftime("%Y%m%d")
        try:
            import akshare as ak
            df = ak.stock_lhb_detail_em(start_date=dt, end_date=dt)
            if df is None or df.empty:
                return []
            records: dict[str, DragonTigerData] = {}
            for _, row in df.iterrows():
                symbol = str(row.get("代码", ""))
                if symbol not in records:
                    records[symbol] = DragonTigerData(
                        symbol=symbol,
                        name=str(row.get("名称", "")),
                        date=dt,
                        reason=str(row.get("上榜原因", "")),
                    )
                rec = records[symbol]
                direction = str(row.get("买卖方向", ""))
                seat = str(row.get("营业部名称", ""))
                amount = float(row.get("成交额", 0) or 0)
                if "买" in direction:
                    rec.buy_amount += amount
                    rec.buy_seats.append(seat)
                else:
                    rec.sell_amount += amount
                    rec.sell_seats.append(seat)
            for rec in records.values():
                rec.net_amount = rec.buy_amount - rec.sell_amount
            result = list(records.values())
            logger.info("龙虎榜数据: %d 只 (%s)", len(result), dt)
            return result
        except Exception as e:
            logger.warning("龙虎榜数据获取失败 (%s): %s", dt, e)
            return []

    def fetch_money_flow(self, symbols: list[str]) -> list[MoneyFlowRecord]:
        """获取个股资金流向"""
        records: list[MoneyFlowRecord] = []
        try:
            import akshare as ak
            for symbol in symbols[:20]:  # 限制请求数量
                code = symbol.split(".")[0]
                try:
                    df = ak.stock_individual_fund_flow(stock=code, market="sh" if symbol.endswith(".SH") else "sz")
                    if df is None or df.empty:
                        continue
                    row = df.iloc[-1]
                    records.append(MoneyFlowRecord(
                        symbol=symbol,
                        name="",
                        date=str(row.get("日期", "")),
                        main_net_inflow=float(row.get("主力净流入-净额", 0) or 0) / 10000,
                        super_large_net=float(row.get("超大单净流入-净额", 0) or 0) / 10000,
                        large_net=float(row.get("大单净流入-净额", 0) or 0) / 10000,
                    ))
                except Exception:
                    continue
            logger.info("资金流向: %d 只", len(records))
        except Exception as e:
            logger.warning("资金流向获取失败: %s", e)
        return records

    def fetch_market_sentiment_data(self, trade_date: str | None = None) -> dict:
        """获取市场情绪原始数据 (涨停/跌停/成交额)"""
        dt = trade_date or date.today().strftime("%Y%m%d")
        limit_up = self.fetch_limit_up(dt)
        limit_down = self.fetch_limit_down(dt)
        board_fail = sum(1 for r in limit_up if r.open_times > 0)
        board_fail_rate = board_fail / max(len(limit_up), 1)
        max_consecutive = max((r.consecutive_days for r in limit_up), default=0)
        return {
            "date": dt,
            "limit_up_count": len(limit_up),
            "limit_down_count": len(limit_down),
            "board_fail_rate": board_fail_rate,
            "max_consecutive_up": max_consecutive,
            "limit_up_symbols": [r.symbol for r in limit_up],
            "limit_down_symbols": [r.symbol for r in limit_down],
        }
