import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jr_indonesia_v1 import runner


ROOT = Path(__file__).resolve().parents[1]


def env_values(data_dir):
    return {
        "DISCORD_TOKEN": "test-token",
        "DISCORD_USER_ID": "123456789012345678",
        "DISCORD_USERNAME": "portable_user",
        "DISCORD_GUILD_ID": "234567890123456789",
        "DISCORD_CHANNEL_ID": "345678901234567890",
        "DISCORD_AUTOREPLY_DATA_DIR": str(data_dir),
    }


def message(mid, text, author="peer", age=1, parent=None):
    item = {
        "id": str(mid),
        "content": text,
        "author": {"id": author, "username": author},
        "timestamp": datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - age, timezone.utc
        ).isoformat(),
    }
    if parent is not None:
        item["referenced_message"] = parent
        item["message_reference"] = {"message_id": parent["id"]}
    return item


class FakeReadAPI:
    def __init__(self, identity, messages):
        self.identity = identity
        self.messages = messages
        self.allow_posts = False
        self.calls = []

    def current_user(self):
        self.calls.append(("GET", "/users/@me"))
        return 200, self.identity

    def fetch_messages(self):
        self.calls.append(("GET", "/channels/messages"))
        return 200, self.messages

    def fetch_around(self, message_id):
        self.calls.append(("GET", "/channels/messages?around=" + message_id))
        return 200, self.messages

    def send_message(self, target, reply):
        self.calls.append(("POST", target["id"]))
        if not self.allow_posts:
            raise AssertionError("dry-run attempted a Discord POST")
        return 200, {"id": "response-id", "timestamp": "now"}


class PublicRunnerTests(unittest.TestCase):
    def test_required_environment_variables_are_explicit(self):
        with self.assertRaises(runner.ConfigError) as caught:
            runner.Settings.from_env({})
        message_text = str(caught.exception)
        for name in runner.REQUIRED_ENV:
            self.assertIn(name, message_text)

    def test_mismatched_account_is_rejected_before_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            api = FakeReadAPI(
                {"id": "999999999999999999", "username": "portable_user"}, []
            )
            with self.assertRaises(runner.IdentityError):
                runner.validate_identity(settings, api)
            self.assertEqual(api.calls, [("GET", "/users/@me")])

    def test_username_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            api = FakeReadAPI(
                {"id": settings.user_id, "username": "another_user"}, []
            )
            with self.assertRaises(runner.IdentityError):
                runner.validate_identity(settings, api)

    def test_dry_run_never_posts_or_writes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            api = FakeReadAPI(
                {"id": settings.user_id, "username": settings.username},
                [message("1", "Chicken katsu")],
            )
            llm = runner.FakeLLM('[{"message_id":"1","reply":"with curry?"}]')
            result = runner.run_dry_run(settings, api, llm)
            self.assertEqual(result["mode"], "dry-run")
            self.assertEqual(result["discord_posts"], 0)
            self.assertEqual(result["candidates"], 1)
            self.assertEqual([item["reply"] for item in result["drafts"]], ["with curry?"])
            self.assertEqual([method for method, _ in api.calls], ["GET", "GET"])
            self.assertFalse(Path(directory, "state.json").exists())

    def test_dry_run_output_does_not_include_source_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            api = FakeReadAPI(
                {"id": settings.user_id, "username": settings.username},
                [message("1", "PRIVATE SOURCE CHAT TEXT")],
            )
            llm = runner.FakeLLM('[{"message_id":"1","reply":"with curry?"}]')
            output = io.StringIO()
            with redirect_stdout(output):
                runner.print_dry_run(runner.run_dry_run(settings, api, llm))
            self.assertNotIn("PRIVATE SOURCE CHAT TEXT", output.getvalue())
            self.assertIn('"discord_posts": 0', output.getvalue())

    def test_profile_keeps_only_calibration_pairs_without_private_ids(self):
        profile = runner.load_profile()
        self.assertEqual(profile["version"], "jr-indonesia-v1")
        self.assertIsNone(profile["country_of_origin"])
        self.assertEqual(len(profile["examples"]), 7)
        expected = [
            ("Go after him", "why chase him?"),
            ("Try typing with your little finger", "show me how then"),
            ("You've already got the blue role", "only noticed now?"),
            ("Five days, maybe a week", "lasts a while then"),
            ("I leave the seasoning at the shop and check it every two days", "how long does it keep?"),
            ("I'm off today. When work gets busy, it's hard to find time", "working tomorrow?"),
            ("Can't sleep, so I came here", "need someone to sing you to sleep?"),
        ]
        self.assertEqual(
            [(row["message_en"], row["reply_en"]) for row in profile["examples"]],
            expected,
        )
        serialized = json.dumps(profile)
        self.assertNotIn("source_reply_id", serialized)
        self.assertNotIn("source_parent_id", serialized)
        self.assertNotIn("destination_channel_id", serialized)
        self.assertNotIn("owner_id", serialized)

    def test_prompt_does_not_assume_a_country_and_marks_examples_as_calibration(self):
        system = runner.build_system(runner.load_profile())
        self.assertIn("Brief, familiar and direct", system)
        self.assertIn("show me how then", system)
        self.assertIn("English adaptations for calibration", system)
        self.assertIn("not current facts", system)
        self.assertNotIn("Malaysia", system)

    def test_rank_context_is_unknown_without_a_rank_file(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                runner.build_stats_line(Path(directory) / "missing-rank.json"),
                "Rank/points are unknown. Do not invent them.",
            )

    def test_candidate_start_window_is_thirty_seconds_and_send_window_ninety(self):
        fresh = message("1", "Winter here", age=29)
        too_old_to_start = message("2", "Winter here", age=31)
        self.assertEqual(
            runner.candidate_batch([fresh, too_old_to_start], [], "me"), [fresh]
        )
        self.assertTrue(runner.is_sendable(fresh, now=datetime.now(timezone.utc).timestamp()))
        ninety_one = message("3", "Winter here", age=91)
        self.assertFalse(
            runner.is_sendable(ninety_one, now=datetime.now(timezone.utc).timestamp())
        )

    def test_drafts_must_reference_supplied_targets_and_deduplicate_recent_text(self):
        targets = [message("1", "I study chemistry"), message("2", "I study biology")]
        raw = json.dumps(
            [
                {"message_id": "unknown", "reply": "What are you studying?"},
                {"message_id": "1", "reply": "What are you studying?"},
                {"message_id": "2", "reply": "What are you studying?"},
            ]
        )
        result = runner.parse_drafts(raw, targets, ["What are you studying?"])
        self.assertEqual(result, [])

    def test_send_payload_always_contains_a_reply_reference(self):
        settings = runner.Settings.from_env(env_values(".runtime/test"))
        target = message("target-id", "A specific topic")
        payload = runner.build_message_payload(settings, target, "short reply")
        self.assertEqual(payload["content"], "short reply")
        self.assertEqual(
            payload["message_reference"],
            {
                "message_id": "target-id",
                "guild_id": settings.guild_id,
                "channel_id": settings.channel_id,
            },
        )
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "replied_user": False})

    def test_stop_flag_is_checked_before_post(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            target = message("1", "A specific topic")
            api = FakeReadAPI(
                {"id": settings.user_id, "username": settings.username}, [target]
            )
            stop = runner.StopFlag()
            original_fetch = api.fetch_around

            def stop_after_refresh(message_id):
                result = original_fetch(message_id)
                stop.request()
                return result

            api.fetch_around = stop_after_refresh
            state = runner.new_state(runner.utc_day_key())
            self.assertFalse(runner.send_one(settings, api, state, target, "short reply", stop))
            self.assertEqual([method for method, _ in api.calls], ["GET"])

    def test_hermes_defaults_are_supported_and_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            values = env_values(directory)
            settings = runner.Settings.from_env(values)
            self.assertEqual(settings.hermes_model, "gpt-6-astra")
            self.assertEqual(settings.hermes_provider, "custom:api.xingchiapi.com")
            values["HERMES_MODEL"] = "other-model"
            values["HERMES_PROVIDER"] = "custom:other.example"
            changed = runner.Settings.from_env(values)
            self.assertEqual(changed.hermes_model, "other-model")
            self.assertEqual(changed.hermes_provider, "custom:other.example")


if __name__ == "__main__":
    unittest.main(verbosity=2)
