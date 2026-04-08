"""A股代码过滤工具"""


def is_a_share_symbol(symbol: str) -> bool:
    if "." not in symbol:
        return False
    code, market = symbol.split(".", 1)
    if market == "SH":
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "SZ":
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "BJ":
        return code.startswith(("43", "83", "87", "88", "92"))
    return False


def is_main_board_symbol(symbol: str) -> bool:
    if not is_a_share_symbol(symbol):
        return False
    code, market = symbol.split(".", 1)
    if market == "SH":
        return code.startswith(("600", "601", "603", "605"))
    if market == "SZ":
        return code.startswith(("000", "001", "002", "003"))
    return False


def filter_a_share(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    accepted: list[str] = []
    for symbol in symbols:
        if symbol in seen or not is_a_share_symbol(symbol):
            continue
        seen.add(symbol)
        accepted.append(symbol)
    return accepted


def get_price_limit_ratio(symbol: str, is_st: bool = False) -> float:
    """获取涨跌停幅度 — 主板10%, 创业板/科创板20%, 北交所30%, ST 5%"""
    if is_st:
        return 0.05
    code = symbol.split(".", 1)[0] if "." in symbol else symbol
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("43", "83", "87", "88", "92")):
        return 0.30
    return 0.10


def get_min_lot_size(symbol: str) -> int:
    """最小交易单位 — 主板100股, 科创板200股, 其余100股"""
    code = symbol.split(".", 1)[0] if "." in symbol else symbol
    if code.startswith(("688", "689")):
        return 200
    return 100


def filter_main_board(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    accepted: list[str] = []
    for symbol in symbols:
        if symbol in seen or not is_main_board_symbol(symbol):
            continue
        seen.add(symbol)
        accepted.append(symbol)
    return accepted
