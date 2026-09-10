from __future__ import annotations

import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CHANNEL_KEYS = {"question": "QUESTION_CHANNEL_ID", "todo": "TODO_CHANNEL_ID"}


class ConfigError(ValueError):
    pass


def load_env(path: Path = ROOT / ".env") -> None:
    """Read simple KEY=value entries; never execute shell code or replace env vars."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f".env {number}행: KEY=value 형식으로 작성하세요.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError(f".env {number}행: 따옴표가 닫히지 않았습니다.")
            value = value[1:-1]
        os.environ.setdefault(key, value)


def credentials(jobs: list[str]) -> tuple[str, dict[str, str]]:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token or token.startswith("Bot ") or any(c.isspace() for c in token):
        raise ConfigError("DISCORD_BOT_TOKEN에 접두사나 공백 없이 봇 토큰을 설정하세요.")
    channels = {}
    for job in jobs:
        key = CHANNEL_KEYS[job]
        channel_id = os.environ.get(key, "").strip()
        if not re.fullmatch(r"[0-9]{17,20}", channel_id):
            raise ConfigError(f"{key}에 채널 이름 대신 숫자로 된 채널 ID를 설정하세요.")
        channels[job] = channel_id
    if len(channels) > 1 and len(set(channels.values())) != len(channels):
        raise ConfigError("오늘의 질문과 오늘의 할 일에는 서로 다른 채널 ID를 설정하세요.")
    return token, channels
