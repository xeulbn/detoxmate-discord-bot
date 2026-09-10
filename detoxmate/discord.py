from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger(__name__)
API_URL = "https://discord.com/api/v10"
REQUIRED_PERMISSIONS = {
    1 << 10: "View Channel",
    1 << 11: "Send Messages",
    1 << 16: "Read Message History",
    1 << 35: "Create Public Threads",
    1 << 38: "Send Messages in Threads",
}


class DiscordError(RuntimeError):
    def __init__(self, status: int, code=None):
        self.status = status
        self.code = code
        hints = {
            401: "봇 토큰이 유효한지 확인하세요.",
            403: "봇 역할과 채널별 권한을 확인하세요.",
            404: "채널 ID와 봇의 서버 참여 여부를 확인하세요.",
            429: "요청 제한이 계속되고 있습니다. 잠시 후 다시 실행하세요.",
        }
        # Do not log response bodies or request headers, which may contain secrets.
        super().__init__(f"Discord HTTP {status}, code={code}. {hints.get(status, '다시 실행하거나 설정을 확인하세요.')}")


def channel_permissions(guild_id: str, user_id: str, roles: list, member: dict, channel: dict) -> int:
    role_ids = {guild_id, *member["roles"]}
    permissions = 0
    for role in roles:
        if role["id"] in role_ids:
            permissions |= int(role["permissions"])
    if permissions & (1 << 3):  # Administrator bypasses channel overwrites.
        return sum(REQUIRED_PERMISSIONS)
    overwrites = channel.get("permission_overwrites", [])
    for overwrite in overwrites:
        if overwrite["type"] == 0 and overwrite["id"] == guild_id:
            permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
    allow = deny = 0
    for overwrite in overwrites:
        if overwrite["type"] == 0 and overwrite["id"] in role_ids - {guild_id}:
            allow |= int(overwrite["allow"])
            deny |= int(overwrite["deny"])
    permissions = (permissions & ~deny) | allow
    for overwrite in overwrites:
        if overwrite["type"] == 1 and overwrite["id"] == user_id:
            permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
    return permissions


class DiscordClient:
    def __init__(self, token: str):
        self._token = token
        self._guilds = {}

    def request(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            API_URL + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://discord.com/developers/docs, 1.0.0)",
            },
        )
        for attempt in range(5):
            delay = 2 ** attempt
            try:
                with urlopen(request, timeout=20) as response:
                    return json.load(response)
            except HTTPError as exc:
                try:
                    error = json.loads(exc.read())
                    if not isinstance(error, dict):
                        error = {}
                except (ValueError, UnicodeError):
                    error = {}
                if exc.code == 429:
                    try:
                        delay = float(error.get("retry_after", exc.headers.get("Retry-After", delay)))
                    except (ValueError, TypeError):
                        raise DiscordError(429) from None
                    # Long waits belong to a later invocation, not a hanging runner.
                    if not math.isfinite(delay) or not 0 <= delay <= 60:
                        raise DiscordError(429, error.get("code")) from None
                    delay = max(delay, 0.1)
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise DiscordError(exc.code, error.get("code")) from None
                if attempt == 4:
                    raise DiscordError(exc.code, error.get("code")) from None
            except (URLError, OSError, HTTPException):
                if attempt == 4:
                    raise RuntimeError("Discord 네트워크 연결에 실패했습니다. 연결 상태를 확인하고 다시 실행하세요.") from None
            except (ValueError, UnicodeError):
                if attempt == 4:
                    raise RuntimeError("Discord 응답을 해석할 수 없습니다. 다시 실행하세요.") from None
            LOG.warning("Discord 요청 재시도 %s/4: %.1f초 후", attempt + 1, delay)
            time.sleep(delay)

    def identity(self) -> dict:
        return self.request("GET", "/users/@me")

    def check_channel(self, channel_id: str, user_id: str) -> dict:
        channel = self.request("GET", f"/channels/{channel_id}")
        if channel.get("type") != 0 or not channel.get("guild_id"):
            raise RuntimeError("일반 서버 텍스트 채널을 지정하세요. 포럼·음성·스레드 채널은 지원하지 않습니다.")
        guild_id = channel["guild_id"]
        if guild_id not in self._guilds:
            roles = self.request("GET", f"/guilds/{guild_id}/roles")
            member = self.request("GET", f"/guilds/{guild_id}/members/{user_id}")
            self._guilds[guild_id] = (roles, member)
        roles, member = self._guilds[guild_id]
        permissions = channel_permissions(guild_id, user_id, roles, member, channel)
        missing = [name for bit, name in REQUIRED_PERMISSIONS.items() if not permissions & bit]
        if missing:
            raise RuntimeError(f"채널 {channel_id}의 봇 권한 부족: {', '.join(missing)}")
        # Without READ_MESSAGE_HISTORY Discord returns [], even when posts exist.
        # Checking effective permissions first prevents silently creating duplicates.
        return channel

    def find_post(self, channel_id: str, user_id: str, marker: str, since: datetime):
        before = None
        for _ in range(100):
            query = {"limit": 100}
            if before:
                query["before"] = before
            messages = self.request("GET", f"/channels/{channel_id}/messages?{urlencode(query)}")
            for message in messages:
                created = datetime.fromisoformat(message["timestamp"].replace("Z", "+00:00"))
                if created < since:
                    return None
                if message.get("author", {}).get("id") == user_id and marker in message.get("content", "").splitlines():
                    return message
            if len(messages) < 100:
                return None
            oldest = messages[-1]["id"]
            if oldest == before:
                raise RuntimeError("채널 기록 페이지가 반복되어 중복 확인을 중단했습니다.")
            before = oldest
        raise RuntimeError("오늘 채널 기록이 10,000건을 초과해 중복 여부를 확인하지 못했습니다. 게시를 중단합니다.")

    def create_post(self, channel_id: str, content: str, marker: str) -> dict:
        # Discord's short-lived nonce protection also covers ambiguous POST retries.
        nonce = hashlib.sha256(f"{channel_id}:{marker}".encode()).hexdigest()[:24]
        return self.request("POST", f"/channels/{channel_id}/messages", {
            "content": content,
            "allowed_mentions": {"parse": []},
            "nonce": nonce,
            "enforce_nonce": True,
        })

    def ensure_thread(self, channel_id: str, message_id: str, title: str) -> dict:
        # A thread created from a message has exactly the source message's ID.
        try:
            return self.request("GET", f"/channels/{message_id}")
        except DiscordError as exc:
            if exc.status != 404:
                raise
        try:
            return self.request("POST", f"/channels/{channel_id}/messages/{message_id}/threads", {
                "name": title,
                "auto_archive_duration": 1440,
            })
        except DiscordError as exc:
            if exc.code != 160004:  # A thread already exists (race / retried POST).
                raise
            return self.request("GET", f"/channels/{message_id}")
