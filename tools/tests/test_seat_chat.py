"""Check persistence and request assembly with local simulated responses."""

import argparse
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("seat_chat", Path(__file__).parents[1] / "seat_chat.py")
chat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chat)


def reply(content="first answer", finish="stop"):
    return {"model": "test-model", "choices": [{"message": {"content": content}, "finish_reason": finish}], "usage": {"total_tokens": 7}}


class SeatsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = Path(self.temp.name)
        self.source = self.game / "system-source.md"
        self.source.write_text("Only use your seat's information.")
        self.env = self.game / ".env"
        self.env.write_text("DEEPSEEK_API_KEY='test-credential'\n")
        for seat in ("01", "02"):
            args = chat.parser().parse_args(["init", "--game", str(self.game), "--seat", seat, "--system-file", str(self.source)])
            chat.initialize(args)

    def post(self, text, *seats):
        source = self.game / "message.md"
        source.write_text(text)
        return chat.send(argparse.Namespace(game=self.game, seats=seats or ["01"], file=source))

    def call(self, seat="01", recover=None):
        return chat.call(argparse.Namespace(game=self.game, seat=seat, env_file=self.env, timeout=1, recover=recover))

    def test_full_history_and_private_message_separation(self):
        self.post("Private clue A", "01")
        with patch.object(chat, "request_json", return_value=reply()) as request:
            self.call()
            payload = request.call_args.args[2]
            self.assertEqual([m["content"] for m in payload["messages"]], [self.source.read_text(), "Private clue A"])
        self.post("Public event", "01", "02")
        with patch.object(chat, "request_json", return_value=reply("second answer")) as request:
            output = self.call()
            self.assertEqual(output["reply"], "second answer")
            self.assertNotIn("messages", output)
            self.assertEqual([m["content"] for m in request.call_args.args[2]["messages"]][1:], ["Private clue A", "first answer", "Public event"])
            self.call("02")
            self.assertEqual([m["content"] for m in request.call_args.args[2]["messages"]][1:], ["Public event"])
        seat = chat.seat_path(self.game, "01")
        self.assertNotIn("test-credential", "".join(p.read_text() for p in seat.rglob("*.json")))

    def test_network_failure_keeps_inbox_without_committing_or_retrying(self):
        self.post("Pick a move")
        with patch.object(chat, "request_json", side_effect=chat.SeatError("network error")) as request:
            with self.assertRaises(chat.SeatError):
                self.call()
            self.assertEqual(request.call_count, 1)
        self.assertEqual(chat.history(chat.seat_path(self.game, "01")), [])
        with patch.object(chat, "request_json", return_value=reply()) as request:
            self.call()
            self.assertEqual(request.call_args.args[2]["messages"][-1]["content"], "Pick a move")

    def test_truncated_output_is_audited_but_not_committed(self):
        self.post("Pick a move")
        with patch.object(chat, "request_json", return_value=reply("partial", "length")), self.assertRaises(chat.SeatError):
            self.call()
        seat = chat.seat_path(self.game, "01")
        self.assertEqual(chat.history(seat), [])
        self.assertEqual(len(list((seat / "calls").glob("*/response.json"))), 1)
        with patch.object(chat, "request_json", return_value=reply("complete")):
            self.assertEqual(self.call()["reply"], "complete")

    def test_saved_response_recovery_does_not_repeat_api_or_consume_new_inbox(self):
        self.post("original message")
        original_write = chat.atomic_text

        def crash_on_history(path, text):
            if path.name == "history.jsonl":
                raise OSError("simulated interruption")
            return original_write(path, text)

        with (
            patch.object(chat, "request_json", return_value=reply()),
            patch.object(chat, "atomic_text", side_effect=crash_on_history),
            self.assertRaises(OSError),
        ):
            self.call()
        seat = chat.seat_path(self.game, "01")
        call_id = next((seat / "calls").iterdir()).name
        self.post("new arrival")
        with patch.object(chat, "request_json") as request:
            with self.assertRaises(chat.SeatError):
                self.call()
            self.assertEqual(self.call(recover=call_id)["reply"], "first answer")
            self.call(recover=call_id)
            request.assert_not_called()
        self.assertEqual(len(chat.history(seat)), 1)
        with patch.object(chat, "request_json", return_value=reply("next")) as request:
            self.call()
            self.assertEqual(request.call_args.args[2]["messages"][-1]["content"], "new arrival")

    def test_empty_inbox_never_calls_provider(self):
        with patch.object(chat, "request_json") as request:
            with self.assertRaises(chat.SeatError):
                self.call()
            request.assert_not_called()

    def test_different_models_and_reserved_parameter_rejection(self):
        seat = chat.seat_path(self.game, "02")
        config = chat.read_json(seat / "config.json")
        config.update(model="other-model", base_url="https://provider.example/v1")
        chat.write_json(seat / "config.json", config)
        self.post("your turn", "02")
        with patch.object(chat, "request_json", return_value=reply()) as request:
            self.call("02")
            self.assertEqual(request.call_args.args[0], "https://provider.example/v1/chat/completions")
            self.assertEqual(request.call_args.args[2]["model"], "other-model")
        config["parameters"] = {"messages": [{"role": "system", "content": "override"}]}
        chat.write_json(seat / "config.json", config)
        with patch.object(chat, "request_json") as request:
            with self.assertRaises(chat.SeatError):
                self.call("02")
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
