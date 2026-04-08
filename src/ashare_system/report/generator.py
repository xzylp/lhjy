"""报告模板引擎"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..logging_config import get_logger

logger = get_logger("report.generator")


class ReportGenerator:
    """报告模板引擎"""

    def __init__(self, output_dir: Path | None = None) -> None:
        self.output_dir = output_dir or Path("logs/reports")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, template: str, context: dict) -> str:
        """简单模板渲染 (替换 {key} 占位符)"""
        try:
            return template.format(**context)
        except KeyError as e:
            logger.warning("模板渲染缺少变量: %s", e)
            return template

    def save(self, content: str, filename: str) -> Path:
        """保存报告到文件"""
        path = self.output_dir / filename
        path.write_text(content, encoding="utf-8")
        logger.info("报告已保存: %s", path)
        return path

    def timestamp_filename(self, prefix: str, ext: str = "md") -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{ts}.{ext}"
