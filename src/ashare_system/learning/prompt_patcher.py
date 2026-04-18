"""Prompt 自进化引擎。

读取 openclaw/prompts/ashare-{role}.txt，在文件末尾维护
<!-- LESSONS_START --> 和 <!-- LESSONS_END --> 之间的教训块。

安全约束（硬编码，永不被自进化覆盖）：
- 每个 Agent 最多 MAX_LESSONS 条 lesson
- 每条最多 MAX_LESSON_CHARS 字符
- 不触碰 LESSONS 标记之外的原始 prompt 内容
- 每次修改写入 prompt_patch_history.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..contracts import Lesson, PatchResult
from ..logging_config import get_logger

logger = get_logger("learning.prompt_patcher")

# ── 硬编码安全约束 ────────────────────────────────────────
MAX_LESSONS = 10          # 每个 Agent 最多保留的教训数量
MAX_LESSON_CHARS = 200    # 每条教训最大字符数
LESSON_TTL_DAYS = 30      # 教训默认有效期（天）

LESSONS_START_MARKER = "<!-- LESSONS_START -->"
LESSONS_END_MARKER = "<!-- LESSONS_END -->"

AGENT_IDS = ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit")


class PromptPatcher:
    """Prompt 自进化引擎。"""

    def __init__(
        self,
        prompts_dir: Path,
        history_path: Path | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._prompts_dir = prompts_dir
        self._history_path = history_path or (prompts_dir.parent / "learning" / "prompt_patch_history.json")
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def read_lessons(self, agent_id: str) -> list[Lesson]:
        """读取指定 Agent 当前的教训列表。"""
        prompt_path = self._prompt_path(agent_id)
        if not prompt_path.exists():
            return []
        content = prompt_path.read_text(encoding="utf-8")
        return self._parse_lessons(content, agent_id)

    def write_lessons(self, agent_id: str, lessons: list[Lesson]) -> PatchResult:
        """将教训列表写入指定 Agent 的 prompt 文件。

        自动执行：
        - 截断超长教训文本
        - 淘汰过期教训
        - 保留最新 MAX_LESSONS 条
        """
        prompt_path = self._prompt_path(agent_id)
        if not prompt_path.exists():
            return PatchResult(agent_id=agent_id, error=f"Prompt 文件不存在: {prompt_path}")

        now = self._now_factory()
        content = prompt_path.read_text(encoding="utf-8")
        old_lessons = self._parse_lessons(content, agent_id)

        # 截断 + 过期过滤 + 数量限制
        valid_lessons: list[Lesson] = []
        for lesson in lessons:
            text = lesson.text[:MAX_LESSON_CHARS]
            expires_at = lesson.expires_at or (now + timedelta(days=LESSON_TTL_DAYS)).isoformat()
            if self._is_expired(expires_at, now):
                continue
            valid_lessons.append(lesson.model_copy(update={
                "text": text,
                "expires_at": expires_at,
                "agent_id": agent_id,
                "created_at": lesson.created_at or now.isoformat(),
            }))
        valid_lessons = valid_lessons[-MAX_LESSONS:]

        # 重新组装 prompt 文件
        new_content = self._rebuild_prompt(content, valid_lessons)
        prompt_path.write_text(new_content, encoding="utf-8")

        # 记录历史
        added = [l.text for l in valid_lessons if l.text not in {ol.text for ol in old_lessons}]
        removed = [ol.text for ol in old_lessons if ol.text not in {l.text for l in valid_lessons}]
        result = PatchResult(
            agent_id=agent_id,
            lessons_before=len(old_lessons),
            lessons_after=len(valid_lessons),
            added=added,
            removed=removed,
        )
        self._append_history(result)
        logger.info("Prompt patch [%s]: before=%d after=%d added=%d removed=%d",
                     agent_id, result.lessons_before, result.lessons_after,
                     len(added), len(removed))
        return result

    def run_daily(
        self,
        agent_lessons: dict[str, list[str]],
    ) -> list[PatchResult]:
        """每日盘后批量执行 Prompt 自进化。

        Args:
            agent_lessons: {agent_id: [lesson_text, ...]}，由 auto_governance.build_agent_lesson_patches() 生成

        Returns:
            每个 Agent 的 patch 结果

        TODO:
            1. 从 attribution_report 和 score_states 自动生成 agent_lessons
            2. 与 scheduler.py 的盘后任务集成
        """
        now = self._now_factory()
        results: list[PatchResult] = []
        for agent_id in AGENT_IDS:
            new_texts = agent_lessons.get(agent_id, [])
            existing = self.read_lessons(agent_id)

            # 合并：旧的有效教训 + 新教训
            merged: list[Lesson] = []
            for lesson in existing:
                if not self._is_expired(lesson.expires_at, now):
                    merged.append(lesson)
            for text in new_texts:
                if text and text not in {l.text for l in merged}:
                    merged.append(Lesson(
                        text=text[:MAX_LESSON_CHARS],
                        source="auto_governance.daily",
                        agent_id=agent_id,
                        created_at=now.isoformat(),
                        expires_at=(now + timedelta(days=LESSON_TTL_DAYS)).isoformat(),
                    ))
            result = self.write_lessons(agent_id, merged)
            results.append(result)
        return results

    def rollback_lesson(self, agent_id: str, lesson_text: str) -> PatchResult:
        """手动回滚指定教训。"""
        lessons = self.read_lessons(agent_id)
        filtered = [l for l in lessons if l.text != lesson_text]
        return self.write_lessons(agent_id, filtered)

    # ── 内部方法 ──

    def _prompt_path(self, agent_id: str) -> Path:
        return self._prompts_dir / f"{agent_id}.txt"

    @staticmethod
    def _parse_lessons(content: str, agent_id: str) -> list[Lesson]:
        """从 prompt 文件内容中解析教训块。"""
        start_idx = content.find(LESSONS_START_MARKER)
        end_idx = content.find(LESSONS_END_MARKER)
        if start_idx < 0 or end_idx < 0 or end_idx <= start_idx:
            return []
        block = content[start_idx + len(LESSONS_START_MARKER):end_idx].strip()
        if not block:
            return []
        lessons: list[Lesson] = []
        for line in block.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 格式: - [expires:YYYY-MM-DD] [source:xxx] 教训文本
            text = line.lstrip("- ").strip()
            expires_at = ""
            source = ""
            if "[expires:" in text:
                try:
                    exp_start = text.index("[expires:") + 9
                    exp_end = text.index("]", exp_start)
                    expires_at = text[exp_start:exp_end]
                    text = text[exp_end + 1:].strip()
                except ValueError:
                    pass
            if "[source:" in text:
                try:
                    src_start = text.index("[source:") + 8
                    src_end = text.index("]", src_start)
                    source = text[src_start:src_end]
                    text = text[src_end + 1:].strip()
                except ValueError:
                    pass
            if text:
                lessons.append(Lesson(
                    text=text,
                    source=source,
                    agent_id=agent_id,
                    expires_at=expires_at,
                ))
        return lessons

    @staticmethod
    def _rebuild_prompt(content: str, lessons: list[Lesson]) -> str:
        """重新组装 prompt 文件，替换或追加教训块。"""
        lines: list[str] = []
        for lesson in lessons:
            parts = []
            if lesson.expires_at:
                parts.append(f"[expires:{lesson.expires_at}]")
            if lesson.source:
                parts.append(f"[source:{lesson.source}]")
            parts.append(lesson.text)
            lines.append("- " + " ".join(parts))
        block = "\n".join(lines)
        new_section = f"{LESSONS_START_MARKER}\n{block}\n{LESSONS_END_MARKER}"

        start_idx = content.find(LESSONS_START_MARKER)
        end_idx = content.find(LESSONS_END_MARKER)
        if start_idx >= 0 and end_idx >= 0:
            return content[:start_idx] + new_section + content[end_idx + len(LESSONS_END_MARKER):]
        else:
            return content.rstrip() + "\n\n" + new_section + "\n"

    @staticmethod
    def _is_expired(expires_at: str, now: datetime) -> bool:
        if not expires_at:
            return False
        try:
            exp = datetime.fromisoformat(expires_at)
            return now > exp
        except ValueError:
            return False

    def _append_history(self, result: PatchResult) -> None:
        history: list[dict] = []
        if self._history_path.exists():
            try:
                history = json.loads(self._history_path.read_text(encoding="utf-8"))
            except Exception:
                history = []
        history.append({
            "timestamp": self._now_factory().isoformat(),
            **result.model_dump(),
        })
        # 只保留最近 200 条记录
        history = history[-200:]
        self._history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
