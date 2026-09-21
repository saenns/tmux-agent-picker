#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


INSTRUCTIONS = (
    "Summarize the completed work or current blocker in at most 14 words. "
    "Return one plain-text line with no label or prefix. Omit credentials, tokens, "
    "secret values, and unnecessary file-system details."
)


def extract_output_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return ""


def request_summary(message: str, model: str, timeout: float = 20) -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return ""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = json.dumps(
        {
            "model": model,
            "store": False,
            "reasoning": {"effort": "none"},
            "instructions": INSTRUCTIONS,
            "input": message,
            "max_output_tokens": 64,
            "text": {"verbosity": "low"},
        }
    ).encode()
    request = urllib.request.Request(
        f"{base}/responses",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return extract_output_text(json.load(response))


def tmux_get(pane: str, name: str) -> str:
    result = subprocess.run(
        ["tmux", "show-option", "-pqv", "-t", pane, name],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip()


def tmux_set(pane: str, name: str, value: str) -> None:
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane, name, value],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    job_path = Path(args.job)
    try:
        job = json.loads(job_path.read_text())
        pane, turn_id = str(job["pane"]), str(job["turn_id"])
        if tmux_get(pane, "@agent_picker_summary_turn") != turn_id:
            return 0
        timeout_value = subprocess.run(
            ["tmux", "show-option", "-gqv", "@agent-picker-summary-timeout"],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        timeout = float(timeout_value or 20)
        summary = request_summary(str(job["message"]), str(job["model"]), timeout)
        if tmux_get(pane, "@agent_picker_summary_turn") != turn_id:
            return 0
        if summary:
            summary = " ".join(summary.split())[:160]
            tmux_set(pane, "@agent_picker_summary", summary)
            tmux_set(pane, "@agent_picker_summary_status", "model")
        else:
            tmux_set(pane, "@agent_picker_summary_status", "local")
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError):
        try:
            job = json.loads(job_path.read_text())
            if tmux_get(str(job["pane"]), "@agent_picker_summary_turn") == str(job["turn_id"]):
                tmux_set(str(job["pane"]), "@agent_picker_summary_status", "local")
        except Exception:
            pass
        return 1
    finally:
        try:
            job_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
