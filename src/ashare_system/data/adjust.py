"""K 线停牌与除权标记。"""

from __future__ import annotations

import pandas as pd


def fill_suspended_days(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    if frame.empty:
        return frame
    previous_close = None
    for index, row in frame.iterrows():
        close = float(row.get("close", 0.0) or 0.0)
        pre_close = float(row.get("pre_close", row.get("preClose", 0.0)) or 0.0)
        volume = float(row.get("volume", 0.0) or 0.0)
        if previous_close is None and close > 0:
            previous_close = close
        if volume <= 0.0:
            suspended_price = pre_close or previous_close or close
            if suspended_price > 0:
                frame.at[index, "open"] = suspended_price
                frame.at[index, "high"] = suspended_price
                frame.at[index, "low"] = suspended_price
                frame.at[index, "close"] = suspended_price
                frame.at[index, "volume"] = 0.0
                frame.at[index, "amount"] = 0.0
        resolved_close = float(frame.at[index, "close"] or 0.0)
        if resolved_close > 0:
            previous_close = resolved_close
    return frame


def detect_ex_rights(df: pd.DataFrame) -> list[str]:
    frame = df.copy()
    if frame.empty:
        return []
    dates: list[str] = []
    previous_close = None
    for index, row in frame.iterrows():
        pre_close = float(row.get("pre_close", row.get("preClose", 0.0)) or 0.0)
        if previous_close and pre_close > 0:
            deviation = abs(pre_close / max(previous_close, 1e-9) - 1.0)
            if deviation > 0.01:
                dates.append(str(index))
        close = float(row.get("close", 0.0) or 0.0)
        if close > 0:
            previous_close = close
    return dates


def mark_adjustment_flags(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    if frame.empty:
        frame["is_suspended"] = []
        frame["is_ex_rights"] = []
        return frame
    ex_rights_dates = set(detect_ex_rights(frame))
    suspended_flags = []
    ex_rights_flags = []
    for index, row in frame.iterrows():
        volume = float(row.get("volume", 0.0) or 0.0)
        close = float(row.get("close", 0.0) or 0.0)
        pre_close = float(row.get("pre_close", row.get("preClose", 0.0)) or 0.0)
        suspended_flags.append(volume <= 0.0 and abs(close - pre_close) < 1e-9)
        ex_rights_flags.append(str(index) in ex_rights_dates)
    frame["is_suspended"] = suspended_flags
    frame["is_ex_rights"] = ex_rights_flags
    return frame
