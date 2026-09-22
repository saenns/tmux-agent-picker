from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


SEP = "\x1f"
STATE_ORDER = {
    "waiting": 0,
    "done": 1,
    "working": 2,
    "idle": 3,
    "run": 4,
    "shell": 5,
    "dead": 6,
    "offline": 7,
}
STATE_LABEL = {
    "waiting": "WAIT",
    "done": "DONE",
    "working": "WORK",
    "idle": "IDLE",
    "run": "RUN ",
    "shell": "SH  ",
    "dead": "DEAD",
    "offline": "OFF ",
}
STATE_COLOR = {
    "waiting": "\x1b[31;1m",
    "done": "\x1b[32;1m",
    "working": "\x1b[33;1m",
    "idle": "\x1b[34m",
    "run": "\x1b[35m",
    "shell": "\x1b[2m",
    "dead": "\x1b[31m",
    "offline": "\x1b[2m",
}
RESET = "\x1b[0m"
SHELL_COMMANDS = {"bash", "dash", "fish", "ksh", "nu", "sh", "tcsh", "zsh"}
AGENT_COMMANDS = {"claude": "claude", "codex": "codex"}
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PROMPT_RE = re.compile(r"(?:^|\s)(?:[$#❯➜>]\s*|[^ ]+@[^ ]+:[^ ]+[$#]\s*)$")
TUI_STATUS_RE = re.compile(
    r"^(?:[•◦●›]\s*)?(?:working|thinking|esc to interrupt|ask codex to do anything)\b"
    r"|^(?:◆\s*)?(?:thought for|task completed|worked for)\b"
    r"|^•\s+(?:ran|edited|read|searched|listed)\b"
    r"|^(?:gpt|claude|grok)-[\w.-]+\s+.*[·│]"
    r"|(?:ctrl|shift|alt)\+\S+.*(?:shortcuts|mode)",
    re.IGNORECASE,
)
CODE_DIFF_RE = re.compile(r"^\d+\s+(?:[+-]\s*)?\S")
BOX_CHARS = frozenset("─━═│┃┄┈┌┐└┘╭╮╰╯❯█ ")


def state_dir() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) / "tmux-agent-picker" if root else Path.home() / ".local/state/tmux-agent-picker"


def mru_path() -> Path:
    return state_dir() / "mru.json"


def load_mru(path: Path | None = None) -> dict[str, float]:
    try:
        data = json.loads((path or mru_path()).read_text())
        return {str(key): float(value) for key, value in data.items()}
    except (FileNotFoundError, ValueError, TypeError, OSError):
        return {}


def save_mru(data: dict[str, float], path: Path | None = None) -> None:
    target = path or mru_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    trimmed = dict(sorted(data.items(), key=lambda item: item[1], reverse=True)[:1000])
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(trimmed, separators=(",", ":")))
    os.replace(temporary, target)


def record_mru(key: str, path: Path | None = None, timestamp: float | None = None) -> None:
    if not key:
        return
    data = load_mru(path)
    data[key] = timestamp if timestamp is not None else time.time()
    save_mru(data, path)


def encode_target(data: dict[str, str]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_target(value: str) -> dict[str, str]:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding))


def normalize_state(explicit: str, command: str, dead: str) -> tuple[str, str]:
    if dead == "1":
        return "dead", ""
    explicit = explicit.strip().lower()
    if explicit in STATE_ORDER:
        return explicit, ""
    command_name = os.path.basename(command.strip()).lower()
    if command_name in AGENT_COMMANDS:
        return "idle", AGENT_COMMANDS[command_name]
    if command_name in SHELL_COMMANDS or not command_name:
        return "shell", ""
    return "run", ""


@dataclass
class Pane:
    pane_id: str
    pane_index: str
    active: bool
    command: str
    path: str
    dead: str
    explicit_state: str
    explicit_agent: str
    agent_session: str
    summary: str
    summary_status: str
    summary_turn: str
    title: str = ""

    @property
    def state_agent(self) -> tuple[str, str]:
        state, detected_agent = normalize_state(self.explicit_state, self.command, self.dead)
        return state, self.explicit_agent.strip() or detected_agent


@dataclass
class Window:
    host: str
    ssh_target: str
    session_id: str
    session_name: str
    window_id: str
    window_index: str
    window_name: str
    active: bool
    panes: list[Pane] = field(default_factory=list)
    bell: bool = False
    last_view: float = 0

    @property
    def key(self) -> str:
        return f"{self.host}|{self.session_id}|{self.window_id}"

    @property
    def selected_pane(self) -> Pane:
        return min(
            self.panes,
            key=lambda pane: (STATE_ORDER[pane.state_agent[0]], not pane.active, int(pane.pane_index or 0)),
        )

    @property
    def state(self) -> str:
        return self.selected_pane.state_agent[0]

    @property
    def agent(self) -> str:
        return self.selected_pane.state_agent[1]

    @property
    def path(self) -> str:
        return self.selected_pane.path


def parse_inventory(text: str, host: str = "local", ssh_target: str = "") -> list[Window]:
    def timestamp(value: str) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            # An old remote hook may have left an unexpanded tmux format here.
            # It must not make the whole picker unavailable.
            return 0.0

    windows: dict[tuple[str, str], Window] = {}
    for line in text.splitlines():
        if not line:
            continue
        # tmux 3.5a renders a control-character format separator as its octal
        # spelling ("\\037") when invoked remotely.  Older tmux versions
        # return the control character directly.
        line = line.replace("\\037", SEP)
        fields = line.split(SEP)
        if len(fields) != 24:
            continue
        (
            session_id,
            session_name,
            window_id,
            window_index,
            window_name,
            window_active,
            window_bell,
            window_last_view,
            window_picker_last_view,
            pane_id,
            pane_index,
            pane_active,
            command,
            title,
            path,
            dead,
            explicit_state,
            explicit_agent,
            agent_session,
            hidden,
            bridge,
            summary,
            summary_status,
            summary_turn,
        ) = fields
        # Bridge viewer sessions are grouped copies of a real session. They
        # exist solely to isolate an interactive bridge's selected window and
        # must never appear as a second copy in any inventory.
        if session_name.startswith("__tap_"):
            continue
        if hidden == "1" or bridge == "1":
            continue
        key = (session_id, window_id)
        if key not in windows:
            windows[key] = Window(
                host=host,
                ssh_target=ssh_target,
                session_id=session_id,
                session_name=session_name,
                window_id=window_id,
                window_index=window_index,
                window_name=window_name,
                active=window_active == "1",
                bell=window_bell == "1",
                last_view=max(timestamp(window_last_view), timestamp(window_picker_last_view)),
            )
        windows[key].panes.append(
            Pane(
                pane_id=pane_id,
                pane_index=pane_index,
                active=pane_active == "1",
                command=command,
                path=path,
                dead=dead,
                explicit_state=explicit_state,
                explicit_agent=explicit_agent,
                agent_session=agent_session,
                summary=summary,
                summary_status=summary_status,
                summary_turn=summary_turn,
                title=title,
            )
        )
    return list(windows.values())


def sort_windows(windows: Iterable[Window], mode: str, mru: dict[str, float]) -> list[Window]:
    def index(window: Window) -> int:
        try:
            return int(window.window_index)
        except ValueError:
            return 1_000_000

    if mode == "state":
        return sorted(
            windows,
            key=lambda window: (
                STATE_ORDER[window.state],
                -mru.get(window.key, 0),
                window.host,
                window.session_name,
                index(window),
            ),
        )
    return sorted(
        windows,
        key=lambda window: (
            -mru.get(window.key, 0),
            STATE_ORDER[window.state],
            window.host,
            window.session_name,
            index(window),
        ),
    )


def compact_path(path: str) -> str:
    home = str(Path.home())
    value = path.replace(home, "~", 1) if path.startswith(home) else path
    parts = value.split("/")
    return value if len(parts) <= 3 else "/".join(parts[:2] + ["…", parts[-1]])


def clean(value: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", value)


def truncate(value: str, limit: int = 120) -> str:
    value = clean(value).strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def fallback_summary(message: str, limit: int = 120) -> str:
    """Produce an immediate, deterministic summary without calling a model."""
    message = ANSI_RE.sub("", message or "")
    candidates: list[str] = []
    in_fence = False
    for raw in message.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^(?:[-*+] |\d+[.)] )", "", line)
        line = re.sub(r"[*_`]+", "", line).strip()
        if not line or line.lower().rstrip(":") in {"summary", "result", "outcome", "done", "completed"}:
            continue
        candidates.append(line)
    if not candidates:
        return ""
    return truncate(candidates[0], limit)


def scrollback_summary(scrollback: str, command: str = "", limit: int = 120) -> str:
    """Return the last meaningful terminal line; terminal content stays local."""
    scrollback = ANSI_RE.sub("", scrollback or "")
    command = os.path.basename(command.strip())
    candidates: list[str] = []
    for raw in scrollback.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or PROMPT_RE.search(line):
            continue
        if TUI_STATUS_RE.search(line):
            continue
        if command in AGENT_COMMANDS and CODE_DIFF_RE.search(line):
            continue
        if command in AGENT_COMMANDS and line.startswith(("│", "└")):
            continue
        if all(character in BOX_CHARS for character in line):
            continue
        if len(line) > 4 and sum(character in BOX_CHARS for character in line) / len(line) >= 0.6:
            continue
        if line in {command, f"{command} ".strip()}:
            continue
        candidates.append(line)
    return truncate(candidates[-1], limit) if candidates else ""


def format_row(window: Window, current_window: str = "") -> str:
    pane = window.selected_pane
    marker = "*" if window.host == "local" and window.window_id == current_window else " "
    bell = "!" if window.bell else " "
    label = STATE_LABEL[window.state]
    state = f"{STATE_COLOR[window.state]}{label}{RESET}"
    agent = window.agent or "-"
    name = window.window_name or "-"
    # Coding agents keep the window name generic but update the pane title with
    # the active task. Keep explicit window names (especially remote ones) as
    # the primary label.
    if name.strip().lower() in AGENT_COMMANDS and pane.title.strip():
        name = re.sub(r"^\W+", "", clean(pane.title)).strip() or name
    summary = pane.summary.strip()
    if not summary:
        summary = pane.command
    if pane.summary_status == "pending":
        summary = f"{summary} …"
    target = encode_target(
        {
            "host": window.host,
            "ssh": window.ssh_target,
            "session_id": window.session_id,
            "session": window.session_name,
            "window_id": window.window_id,
            "window": window.window_index,
            "bridge_name": f"{window.host}:{window.window_index} {truncate(clean(name), 45)}",
            "pane": pane.pane_id,
            "key": window.key,
        }
    )
    display = (
        f"{marker}{bell} {state}  {agent:<7.7}  {window.host:<10.10}  "
        f"{clean(name):<40.40}  {clean(truncate(summary, 120))}"
    )
    return f"{target}\t{display}"


def parse_hosts(value: str) -> list[tuple[str, str]]:
    hosts: list[tuple[str, str]] = []
    for item in re.split(r"[,\n]+", value):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            label, target = item.split("=", 1)
        else:
            target = item
            label = re.sub(r"^.*@", "", target).split(":", 1)[0]
        label, target = label.strip(), target.strip()
        if label and target:
            hosts.append((label, target))
    return hosts
