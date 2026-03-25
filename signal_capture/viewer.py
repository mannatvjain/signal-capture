#!/usr/bin/env python3
"""
TUI viewer for Signal Capture database.

Single-pane scrollable message list with search, date filtering,
and vim-style keybindings.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

DB_PATH = (
    Path.home()
    / "Documents"
    / "Obsidian Vaults"
    / "dot"
    / "CLAUDE"
    / "Artifacts"
    / "signal-capture"
    / "capture.db"
)


def relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to human-readable relative time."""
    dt = datetime.fromisoformat(iso_str)
    now = datetime.now()
    diff = now - dt

    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        m = seconds // 60
        return f"{m}m ago"
    elif seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    elif seconds < 604800:
        d = seconds // 86400
        return f"{d}d ago"
    else:
        return dt.strftime("%b %d")


def load_messages(query: str = "") -> list[tuple]:
    """Load messages from SQLite, optionally filtered by search query."""
    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(DB_PATH))
    try:
        if query:
            rows = conn.execute(
                "SELECT id, signal_timestamp, body, captured_at FROM messages "
                "WHERE body LIKE ? ORDER BY signal_timestamp DESC",
                (f"%{query}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, signal_timestamp, body, captured_at FROM messages "
                "ORDER BY signal_timestamp DESC"
            ).fetchall()
        return rows
    finally:
        conn.close()


class MessageTable(DataTable):
    """Message list with vim keybindings."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
    ]


class SignalViewer(App):
    """TUI viewer for Signal Capture messages."""

    CSS = """
    Screen {
        background: $surface;
    }

    #search-bar {
        dock: top;
        height: 3;
        padding: 0 1;
        display: none;
    }

    #search-bar.visible {
        display: block;
    }

    #search-input {
        width: 100%;
    }

    #status {
        dock: top;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    MessageTable {
        height: 1fr;
    }

    #empty-state {
        width: 100%;
        height: 100%;
        content-align: center middle;
        color: $text-muted;
    }
    """

    TITLE = "Signal Capture"
    theme = "textual-ansi"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
    ]

    search_query: reactive[str] = reactive("")
    search_visible: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="search-bar"):
            yield Input(id="search-input", placeholder="Search messages...")
        yield Label("", id="status")
        yield MessageTable(id="messages")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#messages", MessageTable)
        table.add_columns("Time", "Message")
        table.cursor_type = "row"
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#messages", MessageTable)
        table.clear()

        rows = load_messages(self.search_query)
        status = self.query_one("#status", Label)

        if not rows:
            if self.search_query:
                status.update(f"  No matches for \"{self.search_query}\"")
            else:
                status.update("  No messages yet. Send a Note to Self on Signal.")
            return

        for row in rows:
            _id, signal_ts, body, captured_at = row
            time_str = relative_time(captured_at)
            # Truncate long messages for table display
            display_body = body if len(body) <= 120 else body[:117] + "..."
            table.add_row(time_str, display_body, key=str(_id))

        count = len(rows)
        if self.search_query:
            status.update(f"  {count} result{'s' if count != 1 else ''} for \"{self.search_query}\"")
        else:
            status.update(f"  {count} message{'s' if count != 1 else ''}")

    def action_search(self) -> None:
        search_bar = self.query_one("#search-bar")
        search_bar.add_class("visible")
        self.search_visible = True
        search_input = self.query_one("#search-input", Input)
        search_input.focus()

    def action_clear_search(self) -> None:
        if self.search_visible:
            search_bar = self.query_one("#search-bar")
            search_bar.remove_class("visible")
            self.search_visible = False
            search_input = self.query_one("#search-input", Input)
            search_input.value = ""
            self.search_query = ""
            self._refresh_table()
            self.query_one("#messages", MessageTable).focus()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        self.search_query = event.value
        self._refresh_table()
        search_bar = self.query_one("#search-bar")
        search_bar.remove_class("visible")
        self.search_visible = False
        self.query_one("#messages", MessageTable).focus()

    def action_refresh(self) -> None:
        self._refresh_table()

    def action_help(self) -> None:
        self.notify(
            "j/k: navigate  /: search  Esc: clear  r: refresh  q: quit",
            title="Keybindings",
            timeout=5,
        )


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        print("Run signal_capture.py first to initialize it.", file=sys.stderr)
        sys.exit(1)

    app = SignalViewer()
    app.run()


if __name__ == "__main__":
    main()
