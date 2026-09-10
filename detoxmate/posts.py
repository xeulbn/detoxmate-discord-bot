from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, ConfigError


KST = ZoneInfo("Asia/Seoul")
QUESTION_CYCLE_START = date(2026, 9, 10)


@dataclass(frozen=True)
class Post:
    job: str
    day: date
    title: str
    content: str
    marker: str


def korea_today(now: datetime | None = None) -> date:
    return (now or datetime.now(KST)).astimezone(KST).date()


def load_questions(path: Path = ROOT / "questions.json") -> list[str]:
    try:
        questions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("questions.json을 읽을 수 없습니다. UTF-8 JSON 배열을 확인하세요.") from exc
    if not isinstance(questions, list) or not questions:
        raise ConfigError("questions.json에는 질문 문자열이 하나 이상 있어야 합니다.")
    if any(not isinstance(q, str) or not q.strip() or len(q) > 1200 for q in questions):
        raise ConfigError("각 질문은 1~1,200자의 비어 있지 않은 문자열이어야 합니다.")
    questions = [q.strip() for q in questions]
    if len(set(questions)) != len(questions):
        raise ConfigError("questions.json에 중복 질문이 있습니다.")
    return questions


def _question_order(questions: list[str], cycle: int) -> list[str]:
    # A hash-based shuffle is reproducible on both the laptop and Actions,
    # without saving state or depending on Python's random implementation.
    return sorted(questions, key=lambda q: hashlib.sha256(f"detoxmate:{cycle}:{q}".encode()).digest())


def pick_question(questions: list[str], day: date) -> str:
    cycle, position = divmod((day - QUESTION_CYCLE_START).days, len(questions))
    if len(questions) <= 2:
        # With two questions, alternating is the only way to avoid repeats.
        return _question_order(questions, 0)[position]
    order = _question_order(questions, cycle)
    previous_last = _question_order(questions, cycle - 1)[-1]
    if order[0] == previous_last:
        # Swapping the first two leaves the last item stable for the next cycle.
        order[0], order[1] = order[1], order[0]
    return order[position]


def make_posts(job: str, day: date) -> list[Post]:
    posts = []
    label = f"{day.month:02d}월 {day.day:02d}일"
    if job in ("all", "question"):
        questions = load_questions()
        question = pick_question(questions, day)
        title = f"{label} · 오늘의 질문"
        marker = f"-# detoxmate:question:{day.isoformat()}"
        content = f"## 💬 {title}\n\n{question}\n\n아래 스레드에서 편하게 이야기해 주세요. 짧은 답변도 좋아요!\n\n{marker}"
        posts.append(Post("question", day, title, content, marker))
    if job in ("all", "todo"):
        title = f"{label} · 오늘의 할 일"
        marker = f"-# detoxmate:todo:{day.isoformat()}"
        content = (
            f"## ✅ {title}\n\n오늘 한 일과 병목, 오늘의 한마디와 회고를 아래 스레드에 남겨 주세요.\n"
            "양식을 복사해서 편하게 작성하면 됩니다.\n\n"
            "```text\n[오늘 할 일]\n\n한 일 :\n병목 (없으면 없음) :\n오늘의 한마디 :\n회고 :\n```\n\n"
            f"{marker}"
        )
        posts.append(Post("todo", day, title, content, marker))
    return posts


def publish(client, user_id: str, channel: dict, post: Post) -> str:
    channel_id = channel["id"]
    since = datetime.combine(post.day, time.min, tzinfo=KST)
    message = client.find_post(channel_id, user_id, post.marker, since)
    if message is None:
        message = client.create_post(channel_id, post.content, post.marker)
    thread = client.ensure_thread(channel_id, message["id"], post.title)
    return f"https://discord.com/channels/{channel['guild_id']}/{thread['id']}"
