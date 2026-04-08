"""财务比率因子 (~30个) — PE/PB/ROE/营收增速"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


# ── 估值因子 ──────────────────────────────────────────

@registry.register("pe_ttm", "financial", "市盈率 TTM")
def pe_ttm(df: pd.DataFrame) -> pd.Series:
    if "pe_ttm" in df.columns:
        return df["pe_ttm"]
    if "eps_ttm" in df.columns:
        return df["close"] / df["eps_ttm"].replace(0, np.nan)
    return pd.Series(np.nan, index=df.index)


@registry.register("pb", "financial", "市净率")
def pb(df: pd.DataFrame) -> pd.Series:
    if "pb" in df.columns:
        return df["pb"]
    if "bvps" in df.columns:
        return df["close"] / df["bvps"].replace(0, np.nan)
    return pd.Series(np.nan, index=df.index)


@registry.register("ps_ttm", "financial", "市销率 TTM")
def ps_ttm(df: pd.DataFrame) -> pd.Series:
    if "ps_ttm" in df.columns:
        return df["ps_ttm"]
    return pd.Series(np.nan, index=df.index)


@registry.register("pcf", "financial", "市现率 (价格/每股现金流)")
def pcf(df: pd.DataFrame) -> pd.Series:
    if "cfps" in df.columns:
        return df["close"] / df["cfps"].replace(0, np.nan)
    return pd.Series(np.nan, index=df.index)


@registry.register("ev_ebitda", "financial", "EV/EBITDA")
def ev_ebitda(df: pd.DataFrame) -> pd.Series:
    if "ev_ebitda" in df.columns:
        return df["ev_ebitda"]
    return pd.Series(np.nan, index=df.index)


# ── 盈利能力 ──────────────────────────────────────────

@registry.register("roe", "financial", "净资产收益率 ROE")
def roe(df: pd.DataFrame) -> pd.Series:
    if "roe" in df.columns:
        return df["roe"]
    return pd.Series(np.nan, index=df.index)


@registry.register("roa", "financial", "总资产收益率 ROA")
def roa(df: pd.DataFrame) -> pd.Series:
    if "roa" in df.columns:
        return df["roa"]
    return pd.Series(np.nan, index=df.index)


@registry.register("gross_margin", "financial", "毛利率")
def gross_margin(df: pd.DataFrame) -> pd.Series:
    if "gross_margin" in df.columns:
        return df["gross_margin"]
    return pd.Series(np.nan, index=df.index)


@registry.register("net_margin", "financial", "净利率")
def net_margin(df: pd.DataFrame) -> pd.Series:
    if "net_margin" in df.columns:
        return df["net_margin"]
    return pd.Series(np.nan, index=df.index)


@registry.register("eps_ttm", "financial", "每股收益 TTM")
def eps_ttm(df: pd.DataFrame) -> pd.Series:
    if "eps_ttm" in df.columns:
        return df["eps_ttm"]
    return pd.Series(np.nan, index=df.index)


# ── 成长性 ────────────────────────────────────────────

@registry.register("revenue_yoy", "financial", "营收同比增速")
def revenue_yoy(df: pd.DataFrame) -> pd.Series:
    if "revenue_yoy" in df.columns:
        return df["revenue_yoy"]
    return pd.Series(np.nan, index=df.index)


@registry.register("profit_yoy", "financial", "净利润同比增速")
def profit_yoy(df: pd.DataFrame) -> pd.Series:
    if "profit_yoy" in df.columns:
        return df["profit_yoy"]
    return pd.Series(np.nan, index=df.index)


@registry.register("eps_yoy", "financial", "EPS同比增速")
def eps_yoy(df: pd.DataFrame) -> pd.Series:
    if "eps_yoy" in df.columns:
        return df["eps_yoy"]
    return pd.Series(np.nan, index=df.index)


# ── 财务健康 ──────────────────────────────────────────

@registry.register("debt_ratio", "financial", "资产负债率")
def debt_ratio(df: pd.DataFrame) -> pd.Series:
    if "debt_ratio" in df.columns:
        return df["debt_ratio"]
    return pd.Series(np.nan, index=df.index)


@registry.register("current_ratio", "financial", "流动比率")
def current_ratio(df: pd.DataFrame) -> pd.Series:
    if "current_ratio" in df.columns:
        return df["current_ratio"]
    return pd.Series(np.nan, index=df.index)


@registry.register("quick_ratio", "financial", "速动比率")
def quick_ratio(df: pd.DataFrame) -> pd.Series:
    if "quick_ratio" in df.columns:
        return df["quick_ratio"]
    return pd.Series(np.nan, index=df.index)


# ── 市值相关 ──────────────────────────────────────────

@registry.register("market_cap", "financial", "总市值 (亿元)")
def market_cap(df: pd.DataFrame) -> pd.Series:
    if "market_cap" in df.columns:
        return df["market_cap"]
    if "total_shares" in df.columns:
        return df["close"] * df["total_shares"] / 1e8
    return pd.Series(np.nan, index=df.index)


@registry.register("float_market_cap", "financial", "流通市值 (亿元)")
def float_market_cap(df: pd.DataFrame) -> pd.Series:
    if "float_market_cap" in df.columns:
        return df["float_market_cap"]
    if "float_shares" in df.columns:
        return df["close"] * df["float_shares"] / 1e8
    return pd.Series(np.nan, index=df.index)


@registry.register("log_market_cap", "financial", "对数市值 (规模因子)")
def log_market_cap(df: pd.DataFrame) -> pd.Series:
    mc = market_cap(df)
    return np.log(mc.replace(0, np.nan))


# ── 财务质量因子 (市场验证的Alpha因子) ────────────────────

@registry.register("piotroski_f_score", "financial", "Piotroski F-Score (9分制财务健康)")
def piotroski_f_score(df: pd.DataFrame) -> pd.Series:
    """9项财务指标综合评分: 盈利4项 + 杠杆3项 + 效率2项"""
    score = pd.Series(0.0, index=df.index)
    # 盈利能力 (4分)
    if "roa" in df.columns:
        score += (df["roa"] > 0).astype(float)
    if "cfps" in df.columns:
        score += (df["cfps"] > 0).astype(float)
    if "roa" in df.columns:
        score += (df["roa"].diff() > 0).astype(float)
    if "roa" in df.columns and "cfps" in df.columns and "close" in df.columns:
        accrual = df["roa"] - df["cfps"] / df["close"].replace(0, np.nan)
        score += (accrual < 0).astype(float)  # 应计项为负=高质量
    # 杠杆/流动性 (3分)
    if "debt_ratio" in df.columns:
        score += (df["debt_ratio"].diff() < 0).astype(float)
    if "current_ratio" in df.columns:
        score += (df["current_ratio"].diff() > 0).astype(float)
    if "total_shares" in df.columns:
        score += (df["total_shares"].diff() <= 0).astype(float)  # 未增发
    # 经营效率 (2分)
    if "gross_margin" in df.columns:
        score += (df["gross_margin"].diff() > 0).astype(float)
    if "revenue_yoy" in df.columns and "total_shares" in df.columns:
        turnover_proxy = df["revenue_yoy"]  # 资产周转率代理
        score += (turnover_proxy.diff() > 0).astype(float)
    return score


@registry.register("altman_z_score", "financial", "Altman Z-Score (破产风险)")
def altman_z_score(df: pd.DataFrame) -> pd.Series:
    """Z = 1.2*WC/TA + 1.4*RE/TA + 3.3*EBIT/TA + 0.6*MVE/TL + 1.0*Sales/TA
    Z > 2.99 安全, 1.81-2.99 灰色, < 1.81 危险"""
    required = ["working_capital", "retained_earnings", "ebit", "total_assets", "total_liabilities", "revenue"]
    if not all(c in df.columns for c in required):
        return pd.Series(np.nan, index=df.index)
    ta = df["total_assets"].replace(0, np.nan)
    tl = df["total_liabilities"].replace(0, np.nan)
    mve = market_cap(df) * 1e8  # 转回元
    z = (1.2 * df["working_capital"] / ta
         + 1.4 * df["retained_earnings"] / ta
         + 3.3 * df["ebit"] / ta
         + 0.6 * mve / tl
         + 1.0 * df["revenue"] / ta)
    return z


@registry.register("accruals_ratio", "financial", "应计比率 (盈利质量)")
def accruals_ratio(df: pd.DataFrame) -> pd.Series:
    """(净利润 - 经营现金流) / 总资产，越低盈利质量越高"""
    if "net_income" in df.columns and "cfo" in df.columns and "total_assets" in df.columns:
        ta = df["total_assets"].replace(0, np.nan)
        return (df["net_income"] - df["cfo"]) / ta
    return pd.Series(np.nan, index=df.index)


@registry.register("earnings_quality", "financial", "盈利现金含量 (CFO/净利润)")
def earnings_quality(df: pd.DataFrame) -> pd.Series:
    """经营现金流 / 净利润，>1 表示盈利有真实现金支撑"""
    if "cfo" in df.columns and "net_income" in df.columns:
        return df["cfo"] / df["net_income"].replace(0, np.nan)
    return pd.Series(np.nan, index=df.index)
