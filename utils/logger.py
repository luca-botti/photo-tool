import sys
from typing import TextIO
from enum import Enum, auto

try:
    from rich.console import Console
    from rich.progress import (
        Progress,
        BarColumn,
        MofNCompleteColumn,
        TimeRemainingColumn,
        TaskProgressColumn,
    )
except Exception:
    sys.exit(1)


class LogLevel(Enum):
    TRACE = auto()
    DEBUG = auto()
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


class Logger:
    def __init__(
        self,
        name: str,
        level: LogLevel = LogLevel.INFO,
        stream: "TextIO" = sys.stdout,
        use_rich: bool = True,
    ):
        self.name = name
        self.level = level
        self.stream = stream

        # basic state for fallback progress
        self._progress_active_fallback = False

        self.console = (
            Console(file=self.stream, highlight=False, soft_wrap=False)
            if use_rich
            else None
        )
        self._progress = None
        self._task_id = None
        self._progress_total = None

    # ---------- internal helpers ----------
    def _should_log(self, level: LogLevel) -> bool:
        return level.value >= self.level.value

    def _fmt(self, level: LogLevel | None, message: str) -> str:
        if level is None:
            return message
        return f"[{self.name}] [{level.name}] {message}"

    def _end_fallback_progress_line(self):
        if self._progress_active_fallback:
            print(file=self.stream)
            self._progress_active_fallback = False

    def _log_plain(self, level: LogLevel | None, message: str):
        self._end_fallback_progress_line()
        print(self._fmt(level, message), file=self.stream, flush=True)

    def _log(self, level: LogLevel, message: str):
        if not self._should_log(level):
            return
        self.log(level, message)

    def _log_no_header(self, level: LogLevel, message: str):
        if not self._should_log(level):
            return
        self.log(None, message)

    # ---------- public logging ----------
    def log(self, level: LogLevel | None, message: str):
        if self.console:
            self.console.log(self._fmt(level, message), markup=False, highlight=False)
            # refresh the progress if active
            if self._progress is not None:
                self._progress.refresh()
        else:
            self._log_plain(level, message)

    def trace(self, message: str):
        self._log(LogLevel.TRACE, message)

    def debug(self, message: str):
        self._log(LogLevel.DEBUG, message)

    def info(self, message: str):
        self._log(LogLevel.INFO, message)

    def warning(self, message: str):
        self._log(LogLevel.WARNING, message)

    def error(self, message: str):
        self._log(LogLevel.ERROR, message)

    def critical(self, message: str):
        self._log(LogLevel.CRITICAL, message)
        sys.exit(1)

    def no_header(self, message: str):
        self.log(None, message)

    def trace_no_header(self, message: str):
        self._log_no_header(LogLevel.TRACE, message)

    def debug_no_header(self, message: str):
        self._log_no_header(LogLevel.DEBUG, message)

    def info_no_header(self, message: str):
        self._log_no_header(LogLevel.INFO, message)

    def warning_no_header(self, message: str):
        self._log_no_header(LogLevel.WARNING, message)

    def error_no_header(self, message: str):
        self._log_no_header(LogLevel.ERROR, message)

    def critical_no_header(self, message: str):
        self._log_no_header(LogLevel.CRITICAL, message)
        sys.exit(1)

    def trace_unprintable_chars(self, message: str):
        """Log a message with unprintable characters replaced by their hex codes."""
        message = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in message)
        self.trace(message)

    # ---------- progress (INFO category) ----------
    def info_progress(
        self, current: int, total: int, prefix: str = "", bar_length: int = 30
    ):
        if total <= 0:
            raise ValueError("Total must be greater than zero")
        if not self._should_log(LogLevel.INFO):
            return

        if self.console:
            # initialize progress if not active
            if self._progress is None:
                self._progress = Progress(
                    f"[bold green]{prefix or 'Working'}[/]",
                    BarColumn(),
                    TaskProgressColumn(),
                    MofNCompleteColumn(),
                    TimeRemainingColumn(),
                    console=self.console,
                    transient=False,
                    expand=True,
                )
                self._progress.start()
                self._task_id = self._progress.add_task(
                    prefix or "Working", total=total
                )
                self._progress_total = total

            # update total if changed
            if self._task_id is not None and total != self._progress_total:
                self._progress.update(self._task_id, total=total)
                self._progress_total = total

            # update progress
            if self._task_id is not None:
                self._progress.update(
                    self._task_id, completed=current, description=prefix or "Working"
                )

            # stop progress if done
            if current >= total:
                self._progress.stop()
                self._progress = None
                self._task_id = None
                self._progress_total = None
        else:
            # Fallback: simple single-line progress
            percentage = (current / total) * 100
            filled = int(bar_length * current // total)
            bar = "#" * filled + "-" * (bar_length - filled)
            msg = f"{prefix} [{bar}] {current}/{total} ({percentage:.1f}%)"
            end_char = "\n" if current >= total else "\r"
            print(
                self._fmt(LogLevel.INFO, msg),
                file=self.stream,
                end=end_char,
                flush=True,
            )
            self._progress_active_fallback = current < total

    def end_progress(self, final_message: str | None = None):
        if self.console and self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task_id = None
            self._progress_total = None
        if not self.console and self._progress_active_fallback:
            self._end_fallback_progress_line()
        if final_message:
            self.info(final_message)
