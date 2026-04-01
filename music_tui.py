"""Music Deduplicator – Interactive TUI

Run directly:
    python music_tui.py

Or via the main engine (no CLI args):
    python musicorganise.py
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import List, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
    RadioButton,
    RadioSet,
    Static,
)

# ---------------------------------------------------------------------------
# Shared state passed between screens
# ---------------------------------------------------------------------------

class _SessionState:
    """Holds all data shared across TUI screens."""

    music_path: str = ""
    action: str = "list"          # list | move | delete
    move_dir: str = ""
    dry_run: bool = False
    clear_cache: bool = False
    use_multiprocessing: bool = True

    # Results
    duplicates: List[List[str]] = []
    start_time: float = 0.0
    elapsed: float = 0.0

    # Summary stats snapshot (populated after engine run)
    stats: dict = {}


_state = _SessionState()


# ---------------------------------------------------------------------------
# Custom logging handler that writes to a Textual Log widget
# ---------------------------------------------------------------------------

class _TuiLogHandler(logging.Handler):
    """Forwards log records to a Textual Log widget."""

    def __init__(self, log_widget: Log) -> None:
        super().__init__()
        self._log = log_widget

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # Log widget must be called from the app thread via call_from_thread
            self._log.app.call_from_thread(self._log.write_line, msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helper: build a "best dir" summary for a duplicate set
# ---------------------------------------------------------------------------

def _summarise_duplicate_set(dup_set: List[str]) -> tuple[str, List[str], int, float]:
    """Return (best_dir, dirs_to_remove, total_files, total_mb)."""
    import musicorganise as engine  # lazy import

    best_dir: Optional[str] = None
    best_size: int = -1
    dir_stats: dict = {}

    for d in dup_set:
        stats = {"size": 0, "has_flac": False}
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp):
                        stats["size"] += os.path.getsize(fp)
                        if f.lower().endswith(".flac"):
                            stats["has_flac"] = True
        dir_stats[d] = stats

    for d, s in dir_stats.items():
        if (
            best_dir is None
            or (s["has_flac"] and not dir_stats[best_dir]["has_flac"])
            or (
                s["has_flac"] == dir_stats[best_dir]["has_flac"]
                and s["size"] > best_size
            )
        ):
            best_dir = d
            best_size = s["size"]

    dirs_to_remove = [d for d in dup_set if d != best_dir]
    total_files = 0
    total_bytes = 0
    for d in dirs_to_remove:
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp):
                        total_files += 1
                        total_bytes += os.path.getsize(fp)

    return best_dir or dup_set[0], dirs_to_remove, total_files, total_bytes / (1024 * 1024)


# ===========================================================================
# Screen 1: Setup
# ===========================================================================

SETUP_CSS = """
#setup-screen {
    align: center middle;
}
#setup-panel {
    width: 80;
    height: auto;
    border: solid $accent;
    padding: 1 2;
}
.field-label {
    margin-top: 1;
    color: $text-muted;
}
#action-set {
    margin-top: 1;
    margin-bottom: 1;
}
#move-dir-row {
    height: auto;
}
#btn-row {
    margin-top: 2;
    align: center middle;
    height: auto;
}
"""


class SetupScreen(Screen):
    """Initial configuration screen."""

    CSS = SETUP_CSS

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="setup-screen"):
            with Vertical(id="setup-panel"):
                yield Static("🎵  Music Deduplicator", id="title")
                yield Label("Music Directory", classes="field-label")
                yield Input(
                    placeholder="/path/to/your/music",
                    id="music-path",
                    value=_state.music_path,
                )
                yield Label("Action", classes="field-label")
                with RadioSet(id="action-set"):
                    yield RadioButton("List duplicates (safe – no files changed)", id="action-list", value=True)
                    yield RadioButton("Move duplicates to a folder", id="action-move")
                    yield RadioButton("Delete duplicates permanently", id="action-delete")
                with Vertical(id="move-dir-row"):
                    yield Label("Move Duplicates To", classes="field-label", id="move-dir-label")
                    yield Input(
                        placeholder="/path/to/duplicates",
                        id="move-dir",
                        value=_state.move_dir,
                    )
                yield Checkbox("Dry run (preview – no files will be changed)", id="dry-run", value=_state.dry_run)
                yield Checkbox("Clear cache before scanning", id="clear-cache", value=_state.clear_cache)
                yield Checkbox("Use multiprocessing (faster, uses more CPU)", id="use-mp", value=_state.use_multiprocessing)
                with Horizontal(id="btn-row"):
                    yield Button("⚙  Settings", id="btn-settings", variant="default")
                    yield Button("▶  Start Scan", id="btn-start", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_move_dir_visibility()

    def _refresh_move_dir_visibility(self) -> None:
        show = self.query_one("#action-move", RadioButton).value
        self.query_one("#move-dir-row").display = show
        self.query_one("#move-dir-label").display = show

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._refresh_move_dir_visibility()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-settings":
            self.app.push_screen(SettingsScreen())
        elif event.button.id == "btn-start":
            self._start()

    def _start(self) -> None:
        music_path = self.query_one("#music-path", Input).value.strip()
        if not music_path:
            self.notify("Please enter a music directory path.", severity="error")
            return
        if not os.path.isdir(music_path):
            self.notify(f"Directory not found: {music_path}", severity="error")
            return

        action_set = self.query_one("#action-set", RadioSet)
        if action_set.pressed_index == 1:
            action = "move"
        elif action_set.pressed_index == 2:
            action = "delete"
        else:
            action = "list"

        move_dir = self.query_one("#move-dir", Input).value.strip()
        if action == "move" and not move_dir:
            self.notify("Please enter a destination directory for moved files.", severity="error")
            return

        _state.music_path = music_path
        _state.action = action
        _state.move_dir = move_dir
        _state.dry_run = self.query_one("#dry-run", Checkbox).value
        _state.clear_cache = self.query_one("#clear-cache", Checkbox).value
        _state.use_multiprocessing = self.query_one("#use-mp", Checkbox).value

        if action == "delete" and not _state.dry_run:
            self.app.push_screen(ConfirmDeleteScreen())
        else:
            self.app.push_screen(ScanScreen())


# ===========================================================================
# Screen 2: Settings (AcoustID key, thresholds, etc.)
# ===========================================================================

SETTINGS_CSS = """
#settings-screen {
    align: center middle;
}
#settings-panel {
    width: 70;
    height: auto;
    border: solid $accent;
    padding: 1 2;
}
.field-label { margin-top: 1; color: $text-muted; }
#settings-btn-row {
    margin-top: 2;
    align: center middle;
    height: auto;
}
"""


class SettingsScreen(Screen):
    """Settings: API key, fuzzy threshold, batch size."""

    CSS = SETTINGS_CSS

    def compose(self) -> ComposeResult:
        import musicorganise as engine
        yield Header(show_clock=True)
        with Container(id="settings-screen"):
            with Vertical(id="settings-panel"):
                yield Static("⚙  Settings")
                yield Label("AcoustID API Key", classes="field-label")
                yield Input(
                    placeholder="Your AcoustID API key",
                    id="api-key",
                    value=engine.ACOUSTID_API_KEY or "",
                    password=True,
                )
                yield Label("Fuzzy Match Threshold  (0–100, default 90)", classes="field-label")
                yield Input(
                    placeholder="90",
                    id="fuzzy-threshold",
                    value=str(engine.FUZZY_THRESHOLD),
                )
                yield Label("Batch Size  (directories per batch, default 1000)", classes="field-label")
                yield Input(
                    placeholder="1000",
                    id="batch-size",
                    value=str(engine.BATCH_SIZE),
                )
                with Horizontal(id="settings-btn-row"):
                    yield Button("Cancel", id="btn-cancel", variant="default")
                    yield Button("Save", id="btn-save", variant="primary")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        import musicorganise as engine
        if event.button.id == "btn-cancel":
            self.app.pop_screen()
        elif event.button.id == "btn-save":
            api_key = self.query_one("#api-key", Input).value.strip()
            try:
                fuzzy = int(self.query_one("#fuzzy-threshold", Input).value.strip() or 90)
                batch = int(self.query_one("#batch-size", Input).value.strip() or 1000)
            except ValueError:
                self.notify("Threshold and batch size must be integers.", severity="error")
                return

            engine.ACOUSTID_API_KEY = api_key
            engine.FUZZY_THRESHOLD = fuzzy
            engine.BATCH_SIZE = batch

            config = engine.load_config()
            config["acoustid_api_key"] = api_key
            config["fuzzy_threshold"] = fuzzy
            config["batch_size"] = batch
            engine.save_config(config)

            self.notify("Settings saved.", severity="information")
            self.app.pop_screen()


# ===========================================================================
# Modal: Confirm delete
# ===========================================================================

CONFIRM_CSS = """
#confirm-overlay {
    align: center middle;
}
#confirm-panel {
    width: 60;
    height: auto;
    border: solid $error;
    padding: 1 2;
}
#confirm-btn-row {
    margin-top: 2;
    align: center middle;
    height: auto;
}
"""


class ConfirmDeleteScreen(ModalScreen):
    """Modal confirmation before destructive delete."""

    CSS = CONFIRM_CSS

    def compose(self) -> ComposeResult:
        with Container(id="confirm-overlay"):
            with Vertical(id="confirm-panel"):
                yield Static("⚠  Confirm Permanent Delete", id="confirm-title")
                yield Static(
                    "\nThis will [bold red]permanently delete[/] duplicate directories "
                    "and all their contents.\n\nThis action [bold]cannot be undone[/]. "
                    "Make sure you have a backup.\n"
                )
                with Horizontal(id="confirm-btn-row"):
                    yield Button("Cancel", id="btn-cancel", variant="default")
                    yield Button("Delete", id="btn-confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.app.pop_screen()
        elif event.button.id == "btn-confirm":
            self.app.pop_screen()
            self.app.push_screen(ScanScreen())


# ===========================================================================
# Screen 3: Scan progress
# ===========================================================================

SCAN_CSS = """
#scan-screen {
    align: center middle;
}
#scan-panel {
    width: 90;
    height: 40;
    border: solid $accent;
    padding: 1 2;
}
#scan-status {
    margin-top: 1;
    color: $text-muted;
}
#scan-progress {
    margin-top: 1;
}
#scan-stats {
    margin-top: 1;
    color: $text;
}
#scan-log {
    height: 20;
    margin-top: 1;
    border: solid $panel;
}
#scan-btn-row {
    margin-top: 1;
    align: center middle;
    height: auto;
}
"""


class ScanScreen(Screen):
    """Progress screen shown while the engine scans for duplicates."""

    CSS = SCAN_CSS

    _progress: reactive[int] = reactive(0)
    _total: reactive[int] = reactive(1)
    _status_msg: reactive[str] = reactive("Initialising…")
    _done: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="scan-screen"):
            with Vertical(id="scan-panel"):
                yield Static("🔍  Scanning Your Music Library", id="scan-title")
                yield Label("", id="scan-status")
                yield ProgressBar(id="scan-progress", show_eta=True)
                yield Label("", id="scan-stats")
                yield Log(id="scan-log", auto_scroll=True, highlight=True)
                with Horizontal(id="scan-btn-row"):
                    yield Button("Cancel", id="btn-cancel", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self._attach_log_handler()
        self.run_scan()

    def _attach_log_handler(self) -> None:
        log_widget = self.query_one("#scan-log", Log)
        self._tui_handler = _TuiLogHandler(log_widget)
        self._tui_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s  %(message)s", datefmt="%H:%M:%S")
        )
        logging.getLogger().addHandler(self._tui_handler)

    def _detach_log_handler(self) -> None:
        if hasattr(self, "_tui_handler"):
            logging.getLogger().removeHandler(self._tui_handler)

    def watch__progress(self, value: int) -> None:
        pb = self.query_one("#scan-progress", ProgressBar)
        pb.update(progress=value, total=self._total)

    def watch__total(self, value: int) -> None:
        pb = self.query_one("#scan-progress", ProgressBar)
        pb.update(progress=self._progress, total=value)

    def watch__status_msg(self, value: str) -> None:
        self.query_one("#scan-status", Label).update(value)

    def watch__done(self, done: bool) -> None:
        if done:
            self.query_one("#btn-cancel", Button).label = "Continue →"
            self.query_one("#btn-cancel", Button).variant = "primary"
            self._update_stats()

    def _update_stats(self) -> None:
        import musicorganise as engine
        stats = engine.summary_stats
        files = stats.get("total_files_processed", 0)
        dups = stats.get("total_duplicates_found", 0)
        self.query_one("#scan-stats", Label).update(
            f"Directories scanned: {self._progress} / {self._total}  │  "
            f"Files: {files}  │  Duplicate sets: {dups}"
        )

    def _progress_cb(self, current: int, total: int, message: str) -> None:
        self.call_from_thread(setattr, self, "_progress", current)
        self.call_from_thread(setattr, self, "_total", total)
        self.call_from_thread(setattr, self, "_status_msg", message)
        self.call_from_thread(self._update_stats)

    @work(thread=True)
    def run_scan(self) -> None:
        import musicorganise as engine

        engine.setup_logging(logging.INFO)
        engine.load_or_prompt_config(interactive=False)

        if not engine.check_fpcalc():
            self.call_from_thread(
                self.notify,
                "fpcalc not found. Install chromaprint (see README).",
                severity="error",
            )
            return

        if _state.clear_cache:
            engine.init_cache_db()
            engine.clear_cache()

        _state.start_time = time.time()

        try:
            _state.duplicates = engine.find_duplicates(
                _state.music_path,
                verbose=False,
                use_multiprocessing=_state.use_multiprocessing,
                batch_size=engine.BATCH_SIZE,
                progress_callback=self._progress_cb,
            )
        except Exception as exc:
            logging.error(f"Scan failed: {exc}")
            _state.duplicates = []

        _state.elapsed = time.time() - _state.start_time
        _state.stats = dict(engine.summary_stats)
        self.call_from_thread(setattr, self, "_done", True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            if self._done:
                self._detach_log_handler()
                if _state.duplicates:
                    self.app.push_screen(ReviewScreen())
                else:
                    self.app.push_screen(SummaryScreen())
            else:
                # TODO: cancellation support – for now just notify
                self.notify("Scan in progress, please wait…", severity="warning")


# ===========================================================================
# Screen 4: Review duplicates
# ===========================================================================

REVIEW_CSS = """
#review-screen {
    align: center middle;
}
#review-panel {
    width: 100;
    height: 45;
    border: solid $accent;
    padding: 1 2;
}
#review-table {
    height: 30;
    margin-top: 1;
}
#review-summary {
    margin-top: 1;
    color: $text-muted;
}
#review-btn-row {
    margin-top: 1;
    align: center middle;
    height: auto;
}
"""


class ReviewScreen(Screen):
    """Shows all detected duplicate sets before taking any action."""

    CSS = REVIEW_CSS

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="review-screen"):
            with Vertical(id="review-panel"):
                yield Static("📋  Review Duplicate Sets", id="review-title")
                yield DataTable(id="review-table")
                yield Label("", id="review-summary")
                with Horizontal(id="review-btn-row"):
                    yield Button("← Back", id="btn-back", variant="default")
                    yield Button("✓ Proceed", id="btn-proceed", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._populate_table()

    def _populate_table(self) -> None:
        table = self.query_one("#review-table", DataTable)
        table.add_columns("Set", "Keep (best)", "Remove", "Files", "Size (MB)")
        total_files = 0
        total_mb = 0.0
        for i, dup_set in enumerate(_state.duplicates, 1):
            best, to_remove, files, mb = _summarise_duplicate_set(dup_set)
            total_files += files
            total_mb += mb
            table.add_row(
                str(i),
                os.path.basename(best) or best,
                "\n".join(os.path.basename(d) or d for d in to_remove),
                str(files),
                f"{mb:.1f}",
            )

        action_label = {
            "list": "listed",
            "move": f"moved to {_state.move_dir}",
            "delete": "PERMANENTLY DELETED",
        }.get(_state.action, _state.action)

        dry = " (dry run – no changes)" if _state.dry_run else ""
        self.query_one("#review-summary", Label).update(
            f"{len(_state.duplicates)} duplicate set(s) │ "
            f"{total_files} files │ {total_mb:.1f} MB │ "
            f"Action: {action_label}{dry}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.pop_screen()
        elif event.button.id == "btn-proceed":
            self.app.push_screen(ProcessScreen())


# ===========================================================================
# Screen 5: Processing (move/delete/list)
# ===========================================================================

PROCESS_CSS = """
#process-screen {
    align: center middle;
}
#process-panel {
    width: 90;
    height: 40;
    border: solid $accent;
    padding: 1 2;
}
#process-progress {
    margin-top: 1;
}
#process-status {
    margin-top: 1;
    color: $text-muted;
}
#process-log {
    height: 22;
    margin-top: 1;
    border: solid $panel;
}
#process-btn-row {
    margin-top: 1;
    align: center middle;
    height: auto;
}
"""


class ProcessScreen(Screen):
    """Applies the chosen action to the detected duplicates."""

    CSS = PROCESS_CSS

    _progress: reactive[int] = reactive(0)
    _total: reactive[int] = reactive(1)
    _status_msg: reactive[str] = reactive("Starting…")
    _done: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="process-screen"):
            with Vertical(id="process-panel"):
                yield Static("⚙  Processing Duplicates", id="process-title")
                yield ProgressBar(id="process-progress", show_eta=True)
                yield Label("", id="process-status")
                yield Log(id="process-log", auto_scroll=True, highlight=True)
                with Horizontal(id="process-btn-row"):
                    yield Button("Please wait…", id="btn-done", variant="default", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self._attach_log_handler()
        self.run_process()

    def _attach_log_handler(self) -> None:
        log_widget = self.query_one("#process-log", Log)
        self._tui_handler = _TuiLogHandler(log_widget)
        self._tui_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s  %(message)s", datefmt="%H:%M:%S")
        )
        logging.getLogger().addHandler(self._tui_handler)

    def _detach_log_handler(self) -> None:
        if hasattr(self, "_tui_handler"):
            logging.getLogger().removeHandler(self._tui_handler)

    def watch__progress(self, value: int) -> None:
        pb = self.query_one("#process-progress", ProgressBar)
        pb.update(progress=value, total=self._total)

    def watch__total(self, value: int) -> None:
        pb = self.query_one("#process-progress", ProgressBar)
        pb.update(progress=self._progress, total=value)

    def watch__status_msg(self, value: str) -> None:
        self.query_one("#process-status", Label).update(value)

    def watch__done(self, done: bool) -> None:
        if done:
            btn = self.query_one("#btn-done", Button)
            btn.label = "View Summary →"
            btn.disabled = False
            btn.variant = "primary"

    def _progress_cb(self, current: int, total: int, message: str) -> None:
        self.call_from_thread(setattr, self, "_progress", current)
        self.call_from_thread(setattr, self, "_total", total)
        self.call_from_thread(setattr, self, "_status_msg", message)

    @work(thread=True)
    def run_process(self) -> None:
        import musicorganise as engine

        try:
            engine.resolve_duplicates(
                _state.duplicates,
                action=_state.action,
                move_dir=_state.move_dir,
                base_dir=os.path.abspath(_state.music_path),
                verbose=False,
                dry_run=_state.dry_run,
                progress_callback=self._progress_cb,
            )
        except Exception as exc:
            logging.error(f"Processing failed: {exc}")

        _state.stats = dict(engine.summary_stats)
        engine.close_cache_connection()
        self.call_from_thread(setattr, self, "_done", True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-done" and self._done:
            self._detach_log_handler()
            self.app.push_screen(SummaryScreen())


# ===========================================================================
# Screen 6: Summary
# ===========================================================================

SUMMARY_CSS = """
#summary-screen {
    align: center middle;
}
#summary-panel {
    width: 70;
    height: auto;
    border: solid $success;
    padding: 1 2;
}
.summary-row {
    margin: 0;
    padding: 0;
}
#summary-btn-row {
    margin-top: 2;
    align: center middle;
    height: auto;
}
"""


class SummaryScreen(Screen):
    """Final summary after deduplication."""

    CSS = SUMMARY_CSS

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="summary-screen"):
            with Vertical(id="summary-panel"):
                yield Static("✅  Deduplication Complete")
                yield Static("")
                yield Static("", id="summary-body")
                with Horizontal(id="summary-btn-row"):
                    yield Button("🔁 Scan Again", id="btn-again", variant="default")
                    yield Button("Exit", id="btn-exit", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._render_summary()

    def _render_summary(self) -> None:
        stats = _state.stats
        elapsed = _state.elapsed
        files = stats.get("total_files_processed", 0)
        dups = stats.get("total_duplicates_found", 0)
        to_remove = stats.get("total_files_to_remove", 0)
        saved_mb = stats.get("total_storage_to_save", 0) / (1024 * 1024)
        by_fmt = stats.get("files_by_format", {})

        fmt_lines = "\n".join(
            f"    {ext.upper()}: {count} files" for ext, count in by_fmt.items()
        )
        dry_note = "\n  ⚠  Dry run – no files were modified." if _state.dry_run else ""

        body = (
            f"  Directories scanned    : {files} files\n"
            f"  Duplicate sets found   : {len(_state.duplicates)}\n"
            f"  Files flagged          : {to_remove}\n"
            f"  Estimated space saved  : {saved_mb:.2f} MB\n"
            f"  Time elapsed           : {elapsed:.1f}s\n"
            f"\n  Format breakdown:\n{fmt_lines or '    (none)'}"
            f"{dry_note}"
        )
        self.query_one("#summary-body", Static).update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-again":
            self.app.restart()
        elif event.button.id == "btn-exit":
            self.app.exit()


# ===========================================================================
# Application
# ===========================================================================

APP_CSS = """
Header {
    background: $accent;
    color: $text;
}
Footer {
    background: $panel;
}
#title {
    text-align: center;
    text-style: bold;
    color: $accent;
    padding-bottom: 1;
}
"""


class MusicDeduplicatorApp(App):
    """Textual TUI for Music Deduplicator."""

    CSS = APP_CSS
    TITLE = "Music Deduplicator"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("q", "quit", "Quit"),
    ]

    def on_mount(self) -> None:
        # Load engine config silently (no interactive prompts)
        try:
            import musicorganise as engine
            engine.load_or_prompt_config(interactive=False)
        except SystemExit:
            pass  # API key not set yet – user will configure via Settings screen

    def on_ready(self) -> None:
        self.push_screen(SetupScreen())

    def restart(self) -> None:
        """Return to a fresh SetupScreen, resetting all accumulated stats."""
        import musicorganise as engine

        # Reset engine summary stats for the new run
        engine.summary_stats.update({
            "total_files_processed": 0,
            "total_duplicates_found": 0,
            "total_files_to_remove": 0,
            "total_storage_to_save": 0,
            "files_by_format": {},
        })
        _state.duplicates = []
        _state.stats = {}

        # Pop screens until we reach SetupScreen (or the stack is exhausted)
        while not isinstance(self.screen, SetupScreen):
            try:
                self.pop_screen()
            except Exception:
                # Stack exhausted without finding SetupScreen – push a new one
                self.push_screen(SetupScreen())
                return


def main() -> None:
    app = MusicDeduplicatorApp()
    app.run()


if __name__ == "__main__":
    main()
