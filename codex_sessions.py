#!/usr/bin/env python3
"""
Keyboard TUI for managing local Codex sessions.

Keys:
  Up/Down: select a session
  Enter:   open action menu
  Esc:     quit or close action menu
  r:       reload sessions
  q:       quit
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
SYSTEM_TITLE_PREFIXES = (
    "You are Codex",
    "You are ChatGPT",
    "# AGENTS.md",
    "<environment_context>",
    "Knowledge cutoff:",
)
MENU_ACTIONS = (
    ("delete", "删除"),
    ("archive", "存档"),
    ("resume", "进入"),
)
CONFIRM_ACTIONS = (
    (True, "确认"),
    (False, "退出"),
)
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
ESC_KEY = 27


@dataclass
class Session:
    session_id: str
    title: str = ""
    timestamp: str = ""
    cwd: str = ""
    state: str = "active"
    path: Path | None = None


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def one_line(value: Any, limit: int = 96) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def display_date(timestamp: str) -> str:
    return timestamp[:10] if len(timestamp) >= 10 else timestamp or "-"


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1


def truncate_cells(text: str, max_cells: int) -> str:
    if max_cells <= 0:
        return ""

    cells = 0
    result: list[str] = []
    for char in text:
        width = char_width(char)
        if cells + width > max_cells:
            break
        result.append(char)
        cells += width

    if len(result) == len(text):
        return text

    ellipsis = "..."
    while result and cells + len(ellipsis) > max_cells:
        cells -= char_width(result.pop())
    if len(ellipsis) <= max_cells:
        result.append(ellipsis)
    return "".join(result)


def first_string_by_key(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, str) and child.strip():
                return child.strip()
        for child in value.values():
            found = first_string_by_key(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_string_by_key(child, keys)
            if found:
                return found
    return ""


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def useful_title(value: Any) -> str:
    text = one_line(value)
    if not text:
        return ""
    if UUID_RE.fullmatch(text):
        return ""
    for prefix in SYSTEM_TITLE_PREFIXES:
        if text.startswith(prefix):
            return ""
    return text


def text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return useful_title(value)
    if isinstance(value, dict):
        for key in ("text", "input_text", "message", "content", "prompt"):
            found = text_from_content(value.get(key))
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = text_from_content(child)
            if found:
                return found
    return ""


def explicit_session_title(item: Any) -> str:
    for data in iter_dicts(item):
        for key in ("session_name", "sessionName", "title"):
            found = useful_title(data.get(key))
            if found:
                return found
        kind = str(data.get("type", "")).lower()
        if any(word in kind for word in ("session", "chat", "thread", "conversation")):
            found = useful_title(data.get("name"))
            if found:
                return found
    return ""


def user_message_title(item: Any) -> str:
    for data in iter_dicts(item):
        if data.get("role") == "user":
            found = text_from_content(data.get("content"))
            if found:
                return found
            found = text_from_content(data)
            if found:
                return found

    for data in iter_dicts(item):
        kind = str(data.get("type", "")).lower()
        if "user" not in kind:
            continue
        if not any(word in kind for word in ("message", "input", "prompt")):
            continue
        found = text_from_content(data)
        if found:
            return found

    return ""


def session_title(item: Any) -> str:
    return explicit_session_title(item) or user_message_title(item)


def first_session_id(value: Any) -> str:
    found = first_string_by_key(value, {"session_id", "sessionId", "id"})
    if found and UUID_RE.fullmatch(found):
        return found

    match = UUID_RE.search(json.dumps(value, ensure_ascii=False, default=str))
    return match.group(0) if match else ""


def merge_session(sessions: dict[str, Session], incoming: Session) -> None:
    current = sessions.get(incoming.session_id)
    if current is None:
        sessions[incoming.session_id] = incoming
        return

    if not current.title and incoming.title:
        current.title = incoming.title
    if not current.timestamp and incoming.timestamp:
        current.timestamp = incoming.timestamp
    if not current.cwd and incoming.cwd:
        current.cwd = incoming.cwd
    if current.state == "active" and incoming.state != "active":
        current.state = incoming.state
    if current.path is None and incoming.path is not None:
        current.path = incoming.path


def history_entries(home: Path) -> Iterable[Session]:
    history = home / "history.jsonl"
    if not history.is_file():
        return

    with history.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = first_session_id(item)
            if not session_id:
                continue

            yield Session(
                session_id=session_id,
                title=session_title(item),
                timestamp=one_line(
                    first_string_by_key(item, {"ts", "timestamp", "created_at", "createdAt"})
                ),
                cwd=one_line(first_string_by_key(item, {"cwd", "workdir", "working_dir"})),
            )


def session_file_entries(home: Path, max_lines_per_file: int) -> Iterable[Session]:
    roots = (
        (home / "sessions", "active"),
        (home / "archived_sessions", "archived"),
    )

    for root, state in roots:
        if not root.is_dir():
            continue

        for path in root.rglob("*.jsonl"):
            session = Session(session_id="", state=state, path=path)

            name_match = UUID_RE.search(path.name)
            if name_match:
                session.session_id = name_match.group(0)

            try:
                with path.open("r", encoding="utf-8", errors="replace") as file:
                    for index, line in enumerate(file):
                        if index >= max_lines_per_file:
                            break
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if not session.session_id:
                            session.session_id = first_session_id(item)
                        if not session.timestamp:
                            session.timestamp = one_line(
                                first_string_by_key(
                                    item,
                                    {"timestamp", "created_at", "createdAt", "ts"},
                                )
                            )
                        if not session.cwd:
                            session.cwd = one_line(
                                first_string_by_key(item, {"cwd", "workdir", "working_dir"})
                            )
                        if not session.title:
                            session.title = session_title(item)

                        if session.session_id and session.title and session.timestamp:
                            break
            except OSError:
                continue

            if session.session_id:
                yield session


def load_sessions(home: Path, max_lines_per_file: int) -> list[Session]:
    sessions: dict[str, Session] = {}

    for session in session_file_entries(home, max_lines_per_file) or []:
        merge_session(sessions, session)
    for session in history_entries(home) or []:
        if session.session_id in sessions:
            merge_session(sessions, session)

    return sorted(
        sessions.values(),
        key=lambda item: item.timestamp or str(item.path or ""),
        reverse=True,
    )


def run_codex(action: str, session_id: str) -> tuple[bool, str]:
    command = ["codex", action, session_id]
    if action == "delete":
        command.append("--force")

    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "找不到 codex 命令，请先确认 Codex CLI 已安装并在 PATH 中。"

    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        detail = output or f"codex {action} 返回 {completed.returncode}"
        return False, f"{detail}；该条目可能不是 CLI 可管理的保存会话，或会话仍在使用中"
    return True, output or "完成"


def resume_codex(session_id: str, cwd: str, state: str) -> int:
    command = ["codex", "resume", session_id]
    session_dir = Path(cwd).expanduser() if cwd else None

    if state == "archived":
        ok, message = run_codex("unarchive", session_id)
        if not ok:
            print(f"取消存档失败: {message}")
            return 1

    if session_dir and session_dir.is_dir():
        command = ["codex", "resume", "-C", str(session_dir), session_id]
    elif cwd:
        print(f"会话目录不存在，使用当前目录进入: {cwd}")

    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print("找不到 codex 命令，请先确认 Codex CLI 已安装并在 PATH 中。")
        return 127


def addstr_safe(window: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    text = truncate_cells(text, max(0, width - x - 1))
    if not text:
        return
    try:
        window.addstr(y, x, text, attr)
    except curses.error:
        pass


def confirm(window: curses.window, prompt: str) -> bool:
    selected = 0

    while True:
        height, width = window.getmaxyx()
        prompt = one_line(prompt, max(12, width - 8))
        menu_width = min(max(32, len(prompt) + 4), max(12, width - 4))
        menu_height = 6
        top = max(0, (height - menu_height) // 2)
        left = max(0, (width - menu_width) // 2)

        for row in range(menu_height):
            addstr_safe(window, top + row, left, " " * menu_width)

        border = "+" + "-" * max(0, menu_width - 2) + "+"
        addstr_safe(window, top, left, border)
        for row in range(1, menu_height - 1):
            addstr_safe(window, top + row, left, "|")
            addstr_safe(window, top + row, left + menu_width - 1, "|")
        addstr_safe(window, top + menu_height - 1, left, border)

        addstr_safe(window, top + 1, left + 2, prompt, curses.A_BOLD)
        addstr_safe(window, top + 2, left + 2, "Enter确认  Esc退出")

        option_x = left + 2
        for index, (_, label) in enumerate(CONFIRM_ACTIONS):
            marker = ">" if index == selected else " "
            attr = curses.A_REVERSE if index == selected else 0
            addstr_safe(window, top + 4, option_x, f"{marker} {label}", attr)
            option_x += len(label) + 6

        window.refresh()
        key = window.getch()

        if key == ESC_KEY:
            return False
        if key in ENTER_KEYS:
            return CONFIRM_ACTIONS[selected][0]
        if key in (curses.KEY_UP, curses.KEY_LEFT):
            selected = (selected - 1) % len(CONFIRM_ACTIONS)
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT):
            selected = (selected + 1) % len(CONFIRM_ACTIONS)


def choose_action(window: curses.window, session: Session) -> str | None:
    selected = 0

    while True:
        height, width = window.getmaxyx()
        menu_width = min(max(28, len(session.title) + 4), max(12, width - 4))
        menu_height = len(MENU_ACTIONS) + 4
        top = max(0, (height - menu_height) // 2)
        left = max(0, (width - menu_width) // 2)

        for row in range(menu_height):
            addstr_safe(window, top + row, left, " " * menu_width)

        border = "+" + "-" * max(0, menu_width - 2) + "+"
        addstr_safe(window, top, left, border)
        for row in range(1, menu_height - 1):
            addstr_safe(window, top + row, left, "|")
            addstr_safe(window, top + row, left + menu_width - 1, "|")
        addstr_safe(window, top + menu_height - 1, left, border)

        addstr_safe(window, top + 1, left + 2, "选择操作")
        for index, (_, label) in enumerate(MENU_ACTIONS):
            marker = ">" if index == selected else " "
            attr = curses.A_REVERSE if index == selected else 0
            addstr_safe(window, top + 2 + index, left + 2, f"{marker} {label}", attr)

        window.refresh()
        key = window.getch()

        if key == ESC_KEY:
            return None
        if key in ENTER_KEYS:
            return MENU_ACTIONS[selected][0]
        if key in (curses.KEY_UP, curses.KEY_LEFT):
            selected = (selected - 1) % len(MENU_ACTIONS)
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT):
            selected = (selected + 1) % len(MENU_ACTIONS)


def draw(
    window: curses.window,
    sessions: list[Session],
    selected: int,
    scroll: int,
    home: Path,
    status: str,
) -> None:
    window.erase()
    height, width = window.getmaxyx()
    usable_rows = max(1, height - 4)

    addstr_safe(window, 0, 0, f"Codex sessions: {home}", curses.A_BOLD)
    addstr_safe(window, 1, 0, "↑/↓选择  Enter操作  Esc/q退出  r刷新")

    if not sessions:
        addstr_safe(window, 3, 0, "没有找到 Codex 会话。")
        addstr_safe(window, height - 1, 0, status)
        window.refresh()
        return

    row = 0
    index = scroll
    while row < usable_rows and index < len(sessions):

        session = sessions[index]
        marker = ">" if index == selected else " "
        title = session.title or "(无标题)"
        stamp = display_date(session.timestamp)
        line = f"{marker} [{session.state}] {stamp}  {title}"
        attr = curses.A_REVERSE if index == selected else 0
        addstr_safe(window, row + 3, 0, line, attr)
        row += 1

        if session.cwd and row < usable_rows:
            addstr_safe(window, row + 3, 2, f"目录: {session.cwd}", attr)
            row += 1

        if row < usable_rows:
            row += 1

        index += 1

    addstr_safe(window, height - 1, 0, " " * (width - 1))
    addstr_safe(window, height - 1, 0, status)
    window.refresh()


def tui(
    window: curses.window,
    home: Path,
    max_lines_per_file: int,
) -> tuple[str, str, str] | None:
    curses.curs_set(0)
    window.keypad(True)

    sessions = load_sessions(home, max_lines_per_file)
    selected = 0
    scroll = 0
    status = f"已加载 {len(sessions)} 个会话"

    while True:
        height, _ = window.getmaxyx()
        visible_rows = max(1, (height - 5) // 3)
        selected = min(selected, max(0, len(sessions) - 1))
        if selected < scroll:
            scroll = selected
        elif selected >= scroll + visible_rows:
            scroll = selected - visible_rows + 1

        draw(window, sessions, selected, scroll, home, status)
        key = window.getch()

        if key in (ord("q"), ord("Q"), ESC_KEY):
            return None
        if key == curses.KEY_RESIZE:
            continue
        if key in (ord("r"), ord("R")):
            sessions = load_sessions(home, max_lines_per_file)
            status = f"已刷新，加载 {len(sessions)} 个会话"
            continue
        if not sessions:
            continue

        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(len(sessions) - 1, selected + 1)
        elif key in ENTER_KEYS:
            session = sessions[selected]
            action = choose_action(window, session)
            if action is None:
                status = "已取消操作"
                continue

            if action == "resume":
                return session.session_id, session.cwd, session.state

            delete_prompt = f"确认永久删除 {session.title or session.session_id} ?"
            if action == "delete" and not confirm(window, delete_prompt):
                status = "已取消删除"
                continue

            ok, message = run_codex(action, session.session_id)
            verb = "删除" if action == "delete" else "存档"
            status = f"{verb}: {message}"
            if ok:
                sessions = load_sessions(home, max_lines_per_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List local Codex sessions and manage them from a keyboard menu."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=codex_home(),
        help="Codex home directory, defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--max-lines-per-file",
        type=int,
        default=80,
        help="Maximum transcript lines to inspect per session file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resume_target = curses.wrapper(tui, args.codex_home.expanduser(), args.max_lines_per_file)
    if resume_target:
        session_id, cwd, state = resume_target
        return resume_codex(session_id, cwd, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
