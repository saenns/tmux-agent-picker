from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

from agent_picker import (  # noqa: E402
    SEP,
    decode_target,
    encode_target,
    load_mru,
    parse_hosts,
    parse_inventory,
    record_mru,
    fallback_summary,
    scrollback_summary,
    sort_windows,
    Pane,
    Window,
)
from inventory import remote_attach_command  # noqa: E402
from summarize import extract_output_text  # noqa: E402


def row(
    window_id: str, command: str, state: str = "", agent: str = "", pane_title: str = ""
) -> str:
    return SEP.join(
        (
            "$1",
            "main",
            window_id,
            window_id.removeprefix("@"),
            "editor",
            "1",
            "0",
            "",
            "%1",
            "0",
            "1",
            command,
            pane_title,
            "/home/me/code/project",
            "0",
            state,
            agent,
            "session-1",
            "",
            "",
            "",
            "",
            "",
        )
    )


class AgentPickerTests(unittest.TestCase):
    def test_inventory_keeps_shells_and_agents(self) -> None:
        windows = parse_inventory("\n".join((row("@1", "bash"), row("@2", "codex", "working", "codex"))))
        self.assertEqual(2, len(windows))
        self.assertEqual(["shell", "working"], [window.state for window in windows])

    def test_state_sort_prioritizes_attention(self) -> None:
        windows = parse_inventory("\n".join((row("@1", "bash"), row("@2", "codex", "waiting", "codex"))))
        ordered = sort_windows(windows, "state", {})
        self.assertEqual("@2", ordered[0].window_id)

    def test_mru_sort_uses_focus_history(self) -> None:
        windows = parse_inventory("\n".join((row("@1", "bash"), row("@2", "bash"))))
        ordered = sort_windows(windows, "mru", {"local|$1|@2": 20, "local|$1|@1": 10})
        self.assertEqual("@2", ordered[0].window_id)

    def test_target_round_trip(self) -> None:
        value = {"host": "dev", "session": "work tree", "window_id": "@4"}
        self.assertEqual(value, decode_target(encode_target(value)))

    def test_mru_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mru.json"
            record_mru("local|$1|@1", path, 42)
            self.assertEqual(42, load_mru(path)["local|$1|@1"])

    def test_host_aliases(self) -> None:
        self.assertEqual(
            [("dev", "me@example"), ("server", "root@server")],
            parse_hosts("dev=me@example,root@server"),
        )

    def test_offline_remote_sorts_without_numeric_window(self) -> None:
        offline = Window(
            "dev", "me@dev", "", "unavailable", "", "-", "offline", False,
            [Pane("", "0", True, "", "", "0", "offline", "", "", "", "", "")],
        )
        ordered = sort_windows([offline], "state", {})
        self.assertEqual("offline", ordered[0].state)

    def test_remote_attach_is_one_safely_quoted_shell_command(self) -> None:
        command = remote_attach_command(
            {"ssh": "me@dev", "window_id": "@4", "pane": "%9", "session": "work tree"}
        )
        self.assertIn("ssh -tt me@dev", command)
        self.assertIn("attach-session", command)
        self.assertIn("work tree", command)

    def test_fallback_summary_prefers_first_substantive_line(self) -> None:
        message = "# Summary\n\n- Added cached async summaries.\n- Tests pass."
        self.assertEqual("Added cached async summaries.", fallback_summary(message))

    def test_scrollback_summary_uses_last_non_prompt_line(self) -> None:
        scrollback = "old output\nnew result\nme@host:~/code$ \n"
        self.assertEqual("new result", scrollback_summary(scrollback, "bash"))

    def test_scrollback_summary_skips_agent_tui_chrome(self) -> None:
        scrollback = (
            "Implemented session tracking.\n"
            "◦ Working (8s • esc to interrupt)\n"
            "› Ask Codex to do anything\n"
            "gpt-5.6-sol medium · ~\n"
            "Shift+Tab:mode │ Ctrl+x:shortcuts\n"
            "319 continue\n"
        )
        self.assertEqual("Implemented session tracking.", scrollback_summary(scrollback, "codex"))

    def test_inventory_preserves_cached_summary(self) -> None:
        fields = row("@1", "codex", "done", "codex").split(SEP)
        fields[-3:] = ["Implemented session summaries", "model", "turn-1"]
        window = parse_inventory(SEP.join(fields))[0]
        self.assertEqual("Implemented session summaries", window.selected_pane.summary)
        self.assertEqual("model", window.selected_pane.summary_status)

    def test_extract_output_text_from_responses_payload(self) -> None:
        response = {
            "output": [{"content": [{"type": "output_text", "text": "One line"}]}]
        }
        self.assertEqual("One line", extract_output_text(response))


if __name__ == "__main__":
    unittest.main()
