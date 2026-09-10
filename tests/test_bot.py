from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from detoxmate.cli import main
from detoxmate.config import ConfigError, credentials, load_env
from detoxmate.discord import API_URL, REQUIRED_PERMISSIONS, DiscordClient, DiscordError, channel_permissions
from detoxmate.posts import KST, korea_today, load_questions, make_posts, pick_question


BOT = "300000000000000001"
GUILD = "200000000000000001"
QUESTION = "100000000000000001"
TODO = "100000000000000002"
DAY = date(2026, 9, 10)
ENV = {"DISCORD_BOT_TOKEN": "test-token", "QUESTION_CHANNEL_ID": QUESTION, "TODO_CHANNEL_ID": TODO}
ALL_PERMISSIONS = sum(REQUIRED_PERMISSIONS)


def response(data):
    return io.BytesIO(json.dumps(data).encode())


def http_error(status, data):
    return HTTPError(API_URL, status, "test error", {}, response(data))


class FakeDiscord:
    """Stateful fake at the HTTP boundary, preserving Discord state between runs."""

    def __init__(self):
        self.messages = {QUESTION: [], TODO: []}
        self.threads = {}
        self.writes = []
        self.fail_next_thread = False
        self.permissions = ALL_PERMISSIONS

    def __call__(self, request, timeout):
        assert request.get_header("Authorization") == "Bot test-token"
        assert timeout > 0
        url = urlparse(request.full_url)
        path = url.path.removeprefix("/api/v10")
        method = request.get_method()
        parts = path.strip("/").split("/")
        if path == "/users/@me":
            return response({"id": BOT})
        if path == f"/guilds/{GUILD}/roles":
            return response([{"id": GUILD, "permissions": str(self.permissions)}])
        if path == f"/guilds/{GUILD}/members/{BOT}":
            return response({"roles": []})
        if parts[0] != "channels":
            raise AssertionError(f"Unexpected route: {method} {path}")
        channel_id = parts[1]
        if len(parts) == 2:
            if channel_id in self.messages:
                return response({"id": channel_id, "name": channel_id, "guild_id": GUILD, "type": 0})
            if channel_id in self.threads:
                return response(self.threads[channel_id])
            raise http_error(404, {"code": 10003})
        if len(parts) == 3 and method == "GET":
            before = parse_qs(url.query).get("before", [None])[0]
            messages = self.messages[channel_id]
            if before:
                messages = [m for m in messages if int(m["id"]) < int(before)]
            return response(messages[:100])
        payload = json.loads(request.data)
        if len(parts) == 3 and method == "POST":
            assert payload["allowed_mentions"] == {"parse": []}
            assert payload["enforce_nonce"] is True
            assert len(payload["nonce"]) <= 25
            message_id = str(400000000000000001 + sum(map(len, self.messages.values())))
            message = {
                "id": message_id, "author": {"id": BOT}, "content": payload["content"],
                "timestamp": "2026-09-10T00:00:00+00:00",
            }
            self.messages[channel_id].insert(0, message)
            self.writes.append((path, payload))
            return response(message)
        if len(parts) == 5 and method == "POST" and parts[-1] == "threads":
            if self.fail_next_thread:
                self.fail_next_thread = False
                raise http_error(403, {"code": 50013})
            message_id = parts[3]
            self.threads[message_id] = {"id": message_id, "name": payload["name"]}
            self.writes.append((path, payload))
            return response(self.threads[message_id])
        raise AssertionError(f"Unexpected route: {method} {path}")


class PostTests(unittest.TestCase):
    def test_korean_midnight_and_weekday_boundary(self):
        friday_utc = datetime(2026, 9, 11, 14, 59, tzinfo=timezone.utc)
        self.assertEqual(korea_today(friday_utc), date(2026, 9, 11))
        saturday_utc = friday_utc + timedelta(minutes=1)
        self.assertEqual(korea_today(saturday_utc), date(2026, 9, 12))
        self.assertEqual(len(make_posts("all", korea_today(saturday_utc))), 2)
        self.assertEqual(make_posts("todo", korea_today(saturday_utc))[0].title, "09월 12일 · 오늘의 할 일")

    def test_todo_template_and_posting_cover_all_seven_days(self):
        for offset in range(7):
            day = date(2026, 9, 7) + timedelta(days=offset)
            posts = make_posts("all", day)
            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[1].title, f"09월 {7 + offset:02d}일 · 오늘의 할 일")
        todo = make_posts("todo", date(2026, 9, 9))[0]
        self.assertEqual(todo.title, "09월 09일 · 오늘의 할 일")
        self.assertIn("```text\n[오늘 할 일]\n\n한 일 :\n병목 (없으면 없음) :\n오늘의 한마디 :\n회고 :\n```", todo.content)

    def test_shuffled_questions_are_stable_and_exhaust_each_cycle(self):
        questions = load_questions()
        self.assertGreaterEqual(len(questions), 365)
        start = date(2026, 9, 10)
        cycles = []
        for cycle in range(2):
            selected = [pick_question(questions, start + timedelta(days=cycle * len(questions) + i)) for i in range(len(questions))]
            self.assertEqual(set(selected), set(questions))
            self.assertNotEqual(selected, questions)
            cycles.append(selected)
        self.assertNotEqual(cycles[0], cycles[1])
        self.assertEqual(make_posts("question", DAY), make_posts("question", DAY))

    def test_cycle_boundaries_do_not_repeat_yesterdays_question(self):
        start = date(2026, 9, 10)
        for questions in (["a", "b"], ["a", "b", "c"], load_questions()):
            for cycle in range(-2, 20):
                boundary = start + timedelta(days=cycle * len(questions))
                self.assertNotEqual(pick_question(questions, boundary), pick_question(questions, boundary - timedelta(days=1)))
        self.assertEqual(pick_question(["only question"], start), "only question")

    def test_shuffle_boundary_fix_preserves_last_question(self):
        start = date(2026, 9, 10)
        order = lambda questions, cycle: ["a", "b", "c"] if cycle == 0 else ["b", "c", "a"]
        with patch("detoxmate.posts._question_order", side_effect=order):
            selected = [pick_question(["a", "b", "c"], start + timedelta(days=i)) for i in range(3)]
        self.assertEqual(selected, ["b", "a", "c"])


    def test_invalid_questions_fail_before_posting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "questions.json"
            for value in ([], {}, [" "], [2], ["x" * 1201], ["a", "a"]):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_questions(path)

    def test_dry_run_never_reads_credentials_or_uses_network(self):
        with patch("detoxmate.cli.load_env") as env, patch("detoxmate.discord.urlopen") as network:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--dry-run", "--date", "2026-09-10"]), 0)
            self.assertIn("09월 10일 · 오늘의 할 일", output.getvalue())
            env.assert_not_called()
            network.assert_not_called()

    def test_live_date_override_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--date", "2026-09-10"])
        self.assertEqual(caught.exception.code, 2)


class ConfigTests(unittest.TestCase):
    def test_env_file_does_not_override_actions_secrets(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "actions-token"}, clear=True):
            path = Path(folder) / ".env"
            path.write_text('# comment\nDISCORD_BOT_TOKEN="local-token"\nQUESTION_CHANNEL_ID=100000000000000001\n', encoding="utf-8")
            load_env(path)
            self.assertEqual(credentials(["question"]), ("actions-token", {"question": QUESTION}))

    def test_invalid_env_error_does_not_expose_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('DISCORD_BOT_TOKEN="secret-not-closed', encoding="utf-8")
            with self.assertRaises(ConfigError) as caught:
                load_env(path)
            self.assertNotIn("secret-not-closed", str(caught.exception))

    def test_missing_token_invalid_channel_and_same_channel(self):
        for env in ({}, {**ENV, "QUESTION_CHANNEL_ID": "오늘의-질문"}, {**ENV, "TODO_CHANNEL_ID": QUESTION}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
                credentials(["question", "todo"])


class PermissionTests(unittest.TestCase):
    def permissions(self, overwrites, base=ALL_PERMISSIONS):
        roles = [{"id": GUILD, "permissions": str(base)}, {"id": "role", "permissions": "0"}]
        return channel_permissions(GUILD, BOT, roles, {"roles": ["role"]}, {"permission_overwrites": overwrites})

    def test_member_denial_wins_over_role_allow(self):
        history = 1 << 16
        result = self.permissions([
            {"id": GUILD, "type": 0, "deny": str(history), "allow": "0"},
            {"id": "role", "type": 0, "deny": "0", "allow": str(history)},
            {"id": BOT, "type": 1, "deny": str(history), "allow": "0"},
        ])
        self.assertFalse(result & history)

    def test_role_can_restore_everyone_denial(self):
        history = 1 << 16
        result = self.permissions([
            {"id": GUILD, "type": 0, "deny": str(history), "allow": "0"},
            {"id": "role", "type": 0, "deny": "0", "allow": str(history)},
        ])
        self.assertTrue(result & history)

    def test_admin_bypasses_denials(self):
        result = self.permissions([{"id": BOT, "type": 1, "deny": str(ALL_PERMISSIONS), "allow": "0"}], base=1 << 3)
        self.assertEqual(result & ALL_PERMISSIONS, ALL_PERMISSIONS)

    def test_forum_channel_is_rejected(self):
        client = DiscordClient("test-token")
        client.request = Mock(return_value={"type": 15, "guild_id": GUILD})
        with self.assertRaisesRegex(RuntimeError, "일반 서버 텍스트"):
            client.check_channel(QUESTION, BOT)


class HttpTests(unittest.TestCase):
    @patch("detoxmate.discord.time.sleep")
    @patch("detoxmate.discord.urlopen")
    def test_rate_limit_honors_retry_after(self, network, sleep):
        network.side_effect = [http_error(429, {"retry_after": 1.25}), response({"id": BOT})]
        self.assertEqual(DiscordClient("test-token").identity()["id"], BOT)
        sleep.assert_called_once_with(1.25)

    @patch("detoxmate.discord.time.sleep")
    @patch("detoxmate.discord.urlopen")
    def test_ambiguous_message_retry_keeps_nonce(self, network, sleep):
        network.side_effect = [URLError("socket closed"), http_error(502, {}), response({"id": "message"})]
        DiscordClient("test-token").create_post(QUESTION, "hello", "marker")
        payloads = [json.loads(call.args[0].data) for call in network.call_args_list]
        self.assertEqual(len({p["nonce"] for p in payloads}), 1)
        self.assertTrue(all(p["enforce_nonce"] for p in payloads))
        self.assertEqual(sleep.call_count, 2)

    @patch("detoxmate.discord.time.sleep")
    @patch("detoxmate.discord.urlopen")
    def test_permanent_failure_is_not_retried_or_leaked(self, network, sleep):
        network.side_effect = http_error(401, {"code": 0, "message": "test-token"})
        with self.assertRaises(DiscordError) as caught:
            DiscordClient("test-token").identity()
        self.assertNotIn("test-token", str(caught.exception))
        self.assertEqual(network.call_count, 1)
        sleep.assert_not_called()

    @patch("detoxmate.discord.time.sleep")
    @patch("detoxmate.discord.urlopen")
    def test_retries_are_bounded(self, network, sleep):
        network.side_effect = URLError("offline")
        with self.assertRaisesRegex(RuntimeError, "네트워크"):
            DiscordClient("test-token").identity()
        self.assertEqual(network.call_count, 5)
        self.assertEqual(sleep.call_count, 4)

    def test_thread_retry_recovers_already_created_response(self):
        client = DiscordClient("test-token")
        client.request = Mock(side_effect=[DiscordError(404, 10003), DiscordError(400, 160004), {"id": "thread"}])
        self.assertEqual(client.ensure_thread(QUESTION, "thread", "title"), {"id": "thread"})

    def test_pagination_finds_older_post_and_checks_author(self):
        client = DiscordClient("test-token")
        marker = make_posts("question", DAY)[0].marker
        recent = [{"id": str(1000 - i), "author": {"id": "another-bot"}, "content": marker,
                   "timestamp": "2026-09-10T02:00:00Z"} for i in range(100)]
        target = {"id": "900", "author": {"id": BOT}, "content": marker, "timestamp": "2026-09-10T01:00:00Z"}
        client.request = Mock(side_effect=[recent, [target]])
        self.assertEqual(client.find_post(QUESTION, BOT, marker, datetime(2026, 9, 10, tzinfo=KST)), target)
        self.assertIn("before=901", client.request.call_args.args[1])

    def test_history_limit_fails_closed(self):
        client = DiscordClient("test-token")
        page = lambda n: [{"id": str(20000 - n * 100 - i), "author": {"id": BOT}, "content": "other",
                           "timestamp": "2026-09-10T01:00:00Z"} for i in range(100)]
        client.request = Mock(side_effect=[page(n) for n in range(100)])
        with self.assertRaisesRegex(RuntimeError, "10,000"):
            client.find_post(QUESTION, BOT, "marker", datetime(2026, 9, 10, tzinfo=KST))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeDiscord()
        for patcher in (
            patch.dict(os.environ, ENV, clear=True),
            patch("detoxmate.cli.load_env"),
            patch("detoxmate.cli.korea_today", return_value=DAY),
            patch("detoxmate.discord.urlopen", side_effect=self.server),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_posts_once_even_across_fresh_cli_invocations(self):
        self.assertEqual(main([]), 0)
        self.assertEqual(main([]), 0)
        self.assertEqual(len(self.server.writes), 4)  # Two messages, two threads.
        self.assertEqual(len(self.server.messages[QUESTION]), 1)
        self.assertEqual(len(self.server.messages[TODO]), 1)
        self.assertEqual({t["name"] for t in self.server.threads.values()}, {"09월 10일 · 오늘의 질문", "09월 10일 · 오늘의 할 일"})

    def test_partial_failure_resumes_without_duplicate_parent(self):
        self.server.fail_next_thread = True
        self.assertEqual(main([]), 1)
        self.assertEqual(len(self.server.messages[QUESTION]), 1)
        self.assertEqual(len(self.server.threads), 1)  # Todo still succeeded.
        self.assertEqual(main([]), 0)
        self.assertEqual(len(self.server.messages[QUESTION]), 1)
        self.assertEqual(len(self.server.threads), 2)
        self.assertEqual(len(self.server.writes), 4)

    def test_check_is_read_only(self):
        self.assertEqual(main(["--check"]), 0)
        self.assertEqual(self.server.writes, [])

    def test_weekend_todo_creates_message_and_thread(self):
        with patch("detoxmate.cli.korea_today", return_value=date(2026, 9, 12)):
            self.assertEqual(main(["--job", "todo"]), 0)
        self.assertEqual(len(self.server.messages[TODO]), 1)
        self.assertEqual(len(self.server.threads), 1)
        self.assertIn("[오늘 할 일]", self.server.messages[TODO][0]["content"])

    def test_missing_history_permission_never_publishes(self):
        self.server.permissions &= ~(1 << 16)
        self.assertEqual(main([]), 1)
        self.assertEqual(self.server.writes, [])


if __name__ == "__main__":
    unittest.main()
