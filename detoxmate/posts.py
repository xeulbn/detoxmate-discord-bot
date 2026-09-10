from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, ConfigError


KST = ZoneInfo("Asia/Seoul")


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


def make_posts(job: str, day: date) -> list[Post]:
    posts = []
    label = f"{day.month:02d}월 {day.day:02d}일"
    if job in ("all", "question"):
        questions = load_questions()
        # Cycle through the list once before repeating; reruns pick the same entry.
        question = questions[(day - date(2026, 1, 1)).days % len(questions)]
        title = f"{label} · 오늘의 질문"
        marker = f"-# detoxmate:question:{day.isoformat()}"
        content = f"## 💬 {title}\n\n{question}\n\n아래 스레드에서 편하게 이야기해 주세요. 짧은 답변도 좋아요!\n\n{marker}"
        posts.append(Post("question", day, title, content, marker))
    if job in ("all", "todo") and day.weekday() < 5:
        title = f"{label} · 오늘의 할 일"
        marker = f"-# detoxmate:todo:{day.isoformat()}"
        content = (
            f"## ✅ {title}\n\n오늘 할 일과 도움이 필요한 일을 아래 스레드에 남겨 주세요.\n"
            "양식을 복사해서 편하게 작성하면 됩니다.\n\n"
            "```text\n[오늘 할 일]\n- \n- \n\n[도움이 필요한 일]\n- 없으면 ‘없음’\n```\n\n"
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
