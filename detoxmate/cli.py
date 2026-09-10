from __future__ import annotations

import argparse
import logging
from datetime import date

from .config import credentials, load_env
from .discord import DiscordClient
from .posts import korea_today, make_posts, publish


LOG = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="한국 시간 기준 오늘의 질문/할 일을 Discord에 게시하고 종료합니다.")
    parser.add_argument("--job", choices=("all", "question", "todo"), default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="토큰이나 네트워크 없이 게시 내용 미리보기")
    mode.add_argument("--check", action="store_true", help="봇 인증, 채널 종류와 권한만 확인 (게시 없음)")
    parser.add_argument("--date", type=date.fromisoformat, help="미리보기 날짜 YYYY-MM-DD (--dry-run 전용)")
    args = parser.parse_args(argv)
    if args.date and not args.dry_run:
        parser.error("--date는 --dry-run에서만 사용할 수 있습니다. 실제 게시 날짜는 항상 한국의 오늘입니다.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        day = args.date or korea_today()
        jobs = ["question", "todo"] if args.job == "all" else [args.job]
        posts = [] if args.check else make_posts(args.job, day)
        if args.dry_run:
            for post in posts:
                print(f"[{post.job}] 스레드: {post.title}\n{post.content}\n")
            if not posts:
                print(f"{day}: 주말이므로 오늘의 할 일을 게시하지 않습니다.")
            return 0
        if not args.check:
            jobs = [post.job for post in posts]
            if not jobs:
                LOG.info("%s: 주말이므로 오늘의 할 일을 건너뜁니다.", day)
                return 0
        load_env()
        token, channel_ids = credentials(jobs)
        client = DiscordClient(token)
        user = client.identity()
        channels = {job: client.check_channel(channel_ids[job], user["id"]) for job in jobs}
        if len({channel["guild_id"] for channel in channels.values()}) > 1:
            raise RuntimeError("두 채널은 같은 Discord 서버에 있어야 합니다.")
        for job, channel in channels.items():
            LOG.info("%s: 채널 #%s 접근 및 필수 권한 확인 완료", job, channel["name"])
        if args.check:
            LOG.info("연결 확인 완료. 메시지는 게시하지 않았습니다.")
            return 0
        failures = 0
        for post in posts:
            try:
                url = publish(client, user["id"], channels[post.job], post)
                LOG.info("%s: 게시물과 스레드 준비 완료 %s", post.job, url)
            except RuntimeError as exc:
                LOG.error("%s: %s", post.job, exc)
                failures += 1
        return 1 if failures else 0
    except (ValueError, RuntimeError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
