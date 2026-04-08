"""XtQuant 运行时加载器"""

from __future__ import annotations

import importlib
import sys
from functools import lru_cache
from pathlib import Path


class XtQuantRuntimeError(RuntimeError):
    pass


def _inject_path(path: Path) -> None:
    """注入路径 - xtquant 包在 path/xtquant/"""
    # xtquant 包在 xtquantservice/xtquant/，需要添加 xtquantservice 目录
    # 或者如果路径直接包含 xtquant 文件夹，也需要添加其父目录
    if (path / "xtquant").exists():
        text = str(path)
    elif (path.parent / "xtquant").exists():
        text = str(path.parent)
    else:
        text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
        print(f"Added to sys.path: {text}")


def _clear_xtquant_modules() -> None:
    for name in list(sys.modules):
        if name == "xtquant" or name.startswith("xtquant."):
            sys.modules.pop(name, None)


def _try_import_from(base: Path | None) -> dict[str, object]:
    _clear_xtquant_modules()
    if base is not None:
        _inject_path(base)
    try:
        xtdata = importlib.import_module("xtquant.xtdata")
        xttrader = importlib.import_module("xtquant.xttrader")
        xttype = importlib.import_module("xtquant.xttype")
        xtconstant = importlib.import_module("xtquant.xtconstant")
        return {
            "xtdata": xtdata,
            "xttrader": xttrader,
            "xttype": xttype,
            "xtconstant": xtconstant,
        }
    except Exception as e:
        if base is not None:
            text = str(base)
            if text in sys.path:
                sys.path.remove(text)
        raise e


@lru_cache(maxsize=1)
def load_xtquant_modules(xtquant_root: str, xtquantservice_root: str) -> dict[str, object]:
    if sys.version_info[:2] not in {(3, 8), (3, 10), (3, 11), (3, 12)}:
        print(f"Warning: Python {sys.version_info.major}.{sys.version_info.minor} 可能不兼容 XtQuant")

    # 优先尝试 pip 安装的原生包
    try:
        return _try_import_from(None)
    except Exception:
        pass

    candidates = [
        Path(xtquantservice_root),
        Path(xtquant_root),
    ]
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return _try_import_from(candidate)
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error
    raise FileNotFoundError("未找到可用的 xtquant 运行环境或目录")
