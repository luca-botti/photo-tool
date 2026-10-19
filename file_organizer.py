#!/usr/bin/env python3
"""
File Organizer
--------------
1. Scan a folder (with a progress bar) -> in-memory list of files.
2. Extensions are grouped into categories (Images, Videos, Audio, ...).
   Drag a whole category - or a single extension - onto a destination path.
3. Destinations are plain folder paths you type or browse for. Any folder that
   doesn't exist yet is created automatically when files are moved.
4. Optional junk flag: moves .DS_Store, Thumbs.db, temp files etc. into a
   review folder (default "_Junk" inside the source folder) instead of deleting.
5. Preview, Move, and Undo.

Only the Python standard library is used (tkinter). Run:
    python file_organizer.py
    python file_organizer.py --folder "C:\\Users\\me\\Downloads" --junk --recursive
"""

import argparse
import os
import queue
import shutil
import stat
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

NO_EXT = "(no ext)"
OTHER = "Other"

# ---- Junk detection (conservative: things that are safe to throw away) ----
JUNK_NAMES = {
    ".ds_store",
    "thumbs.db",
    "ehthumbs.db",
    "ehthumbs_vista.db",
    "desktop.ini",
    "iconcache.db",
    ".localized",
    ".fseventsd",
    ".spotlight-v100",
    ".trashes",
    "npthumb.dat",
    "picasa.ini",
}
JUNK_EXTS = {
    ".tmp",
    ".temp",
    ".crdownload",
    ".part",
    ".partial",
    ".swp",
    ".swo",
    ".bak~",
    ".dmp",
    ".chk",
}
JUNK_PREFIXES = (
    "~$",
    "._",
    ".~lock.",
)  # Office lock files, macOS resource forks, LibreOffice locks

# ---- Macro categories (edit freely) ----
CATEGORIES = {
    "Images": [
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".svg",
        ".ico",
        ".raw",
        ".cr2",
        ".nef",
        ".arw",
        ".dng",
        ".psd",
    ],
    "Videos": [
        ".mp4",
        ".mkv",
        ".mov",
        ".avi",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpg",
        ".mpeg",
        ".3gp",
    ],
    "Audio": [
        ".mp3",
        ".wav",
        ".flac",
        ".aac",
        ".m4a",
        ".ogg",
        ".wma",
        ".opus",
        ".aiff",
    ],
    "Documents": [
        ".pdf",
        ".doc",
        ".docx",
        ".odt",
        ".rtf",
        ".txt",
        ".md",
        ".epub",
        ".mobi",
        ".pages",
        ".tex",
    ],
    "Spreadsheets": [".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv", ".numbers"],
    "Presentations": [".ppt", ".pptx", ".odp", ".key"],
    "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"],
    "Code": [
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".html",
        ".htm",
        ".css",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".cs",
        ".go",
        ".rs",
        ".php",
        ".rb",
        ".sh",
        ".bat",
        ".ps1",
        ".json",
        ".xml",
        ".yml",
        ".yaml",
        ".sql",
        ".ipynb",
    ],
    "Installers": [
        ".exe",
        ".msi",
        ".dmg",
        ".pkg",
        ".deb",
        ".rpm",
        ".iso",
        ".apk",
        ".appimage",
    ],
    "Fonts": [".ttf", ".otf", ".woff", ".woff2"],
}
EXT_TO_CAT = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
CAT_ORDER = list(CATEGORIES) + [OTHER]


def category_of(ext: str) -> str:
    return EXT_TO_CAT.get(ext, OTHER)


@dataclass
class FileEntry:
    path: Path
    ext: str
    size: int
    junk: bool


def is_junk(path: Path) -> bool:
    name = path.name.lower()
    if name in JUNK_NAMES or name.startswith(JUNK_PREFIXES):
        return True
    return path.suffix.lower() in JUNK_EXTS


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix, i = target.stem, target.suffix, 1
    while True:
        candidate = target.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b))
    )


def valid_path(p: Path) -> bool:
    """Reject paths the OS can't create (mainly a Windows concern)."""
    if p.exists() and not p.is_dir():
        return False
    if sys.platform != "win32":
        return True
    invalid = set('<>:"|?*')
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    for part in p.parts[1:]:  # parts[0] is the drive / root
        if part != part.rstrip(" .") or any(c in invalid or ord(c) < 32 for c in part):
            return False
        if part.split(".")[0].lower() in reserved:
            return False
    return True


class App(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.title("File Organizer")
        self.geometry("1100x680")
        self.minsize(900, 520)

        self.entries: list[FileEntry] = []
        self.mapping: dict[str, str] = {}  # extension -> destination path
        self.folders: list[str] = []  # destination paths
        self.move_log: list[tuple[Path, Path]] = []  # (new, old)
        self.created_dirs: list[Path] = (
            []
        )  # folders created by the last move (for undo)
        self._ext_stats: dict[str, list[int]] = {}
        self._folder_iids: dict[str, str] = {}

        # background-scan state
        self._scanning = False
        self._cancel = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._total = 0
        self._done_msg: str | None = None

        # background move/undo state
        self._moving = False
        self._job_files = 0
        self._job_verb = ""
        self._on_jobs_finished = None

        self.src_var = tk.StringVar(value=args.folder or "")
        self.recursive_var = tk.BooleanVar(value=args.recursive)
        self.junk_var = tk.BooleanVar(value=args.junk)
        self.junk_folder_var = tk.StringVar(value=args.junk_folder)

        self._drag_items: list[str] = []
        self._drag_win: tk.Toplevel | None = None
        self._drag_started = False
        self._press_row = ""

        self._build_ui()
        if args.folder:
            self.scan()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Source folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.src_var).grid(
            row=0, column=1, sticky="ew", padx=4
        )
        ttk.Button(top, text="Browse…", command=self.browse_src).grid(row=0, column=2)
        self.scan_btn = ttk.Button(top, text="Scan", command=self.scan)
        self.scan_btn.grid(row=0, column=3, padx=(4, 0))
        ttk.Checkbutton(
            top, text="Include subfolders", variable=self.recursive_var
        ).grid(row=0, column=4, padx=(8, 0))

        junk = ttk.Frame(top)
        junk.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(
            junk,
            text="Move junk/temp files (.DS_Store, Thumbs.db, ~$*, .tmp, …) to:",
            variable=self.junk_var,
            command=self.refresh_ext_tree,
        ).pack(side="left")
        ttk.Entry(junk, textvariable=self.junk_folder_var, width=34).pack(
            side="left", padx=4
        )
        ttk.Button(junk, text="Browse…", command=self.browse_junk).pack(side="left")
        self.junk_label = ttk.Label(junk, text="")
        self.junk_label.pack(side="left", padx=8)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=6)

        # Left: categories -> extensions
        left = ttk.LabelFrame(
            panes,
            text="Categories & extensions  (drag onto a destination →)",
            padding=4,
        )
        panes.add(left, weight=3)
        self.ext_tree = ttk.Treeview(
            left, columns=("count", "size", "dest"), selectmode="extended"
        )
        self.ext_tree.heading("#0", text="Category / extension")
        self.ext_tree.heading("count", text="Files")
        self.ext_tree.heading("size", text="Size")
        self.ext_tree.heading("dest", text="Destination")
        self.ext_tree.column("#0", width=170)
        self.ext_tree.column("count", width=60, anchor="e")
        self.ext_tree.column("size", width=80, anchor="e")
        self.ext_tree.column("dest", width=240)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.ext_tree.yview)
        self.ext_tree.configure(yscrollcommand=sb.set)
        self.ext_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.ext_tree.tag_configure(
            "assigned", background="#e3f1e3", foreground="#000000"
        )
        self.ext_tree.tag_configure(
            "partial", background="#fff4d6", foreground="#000000"
        )

        # Right: destination paths
        right = ttk.LabelFrame(
            panes, text="Destination folders  (drop here)", padding=4
        )
        panes.add(right, weight=2)
        self.folder_tree = ttk.Treeview(
            right, columns=("files", "status"), selectmode="browse"
        )
        self.folder_tree.heading("#0", text="Path")
        self.folder_tree.heading("files", text="Files")
        self.folder_tree.heading("status", text="Status")
        self.folder_tree.column("#0", width=260)
        self.folder_tree.column("files", width=50, anchor="e")
        self.folder_tree.column("status", width=100)
        self.folder_tree.pack(fill="both", expand=True)

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Add destination…", command=self.add_destination).pack(
            side="left"
        )
        ttk.Button(btns, text="Remove", command=self.remove_folder).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Auto-organize into…", command=self.auto_organize).pack(
            side="left"
        )

        # Progress row (only shown while scanning)
        self.prog_frame = ttk.Frame(self, padding=(8, 0, 8, 0))
        self.progress = ttk.Progressbar(self.prog_frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(self.prog_frame, text="", width=36, anchor="w")
        self.progress_label.pack(side="left", padx=8)
        ttk.Button(self.prog_frame, text="Cancel", command=self._cancel_scan).pack(
            side="left"
        )

        self.bottom = ttk.Frame(self, padding=8)
        self.bottom.pack(fill="x")
        self.status = ttk.Label(self.bottom, text="Choose a folder and click Scan.")
        self.status.pack(side="left")
        ttk.Button(self.bottom, text="Undo last move", command=self.undo).pack(
            side="right"
        )
        ttk.Button(self.bottom, text="Move files", command=self.run_move).pack(
            side="right", padx=4
        )
        ttk.Button(self.bottom, text="Preview", command=self.preview).pack(side="right")

        # Drag & drop bindings (pure tkinter, no extra packages)
        self.ext_tree.bind("<ButtonPress-1>", self._drag_press)
        self.ext_tree.bind("<B1-Motion>", self._drag_motion)
        self.ext_tree.bind("<ButtonRelease-1>", self._drag_release)

    # --------------------------------------------------- Paths & dialogs
    def browse_src(self):
        d = filedialog.askdirectory(title="Choose folder to scan")
        if d:
            self.src_var.set(os.path.normpath(d))
            self.scan()

    def browse_junk(self):
        d = filedialog.askdirectory(title="Choose junk folder")
        if d:
            self.junk_folder_var.set(os.path.normpath(d))

    def ask_path(self, title: str, prompt: str, initial: str = "") -> str | None:
        """Small modal dialog: type a folder path (it may not exist yet) or browse."""
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.resizable(True, False)
        win.columnconfigure(0, weight=1)
        var = tk.StringVar(value=initial)
        result = {"v": None}

        ttk.Label(win, text=prompt).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 4)
        )
        entry = ttk.Entry(win, textvariable=var, width=64)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(10, 4))

        def browse():
            kw = {"parent": win, "title": "Choose folder"}
            if os.path.isdir(var.get()):
                kw["initialdir"] = var.get()
            d = filedialog.askdirectory(**kw)
            if d:
                var.set(os.path.normpath(d))

        def ok(*_):
            result["v"] = var.get().strip()
            win.destroy()

        ttk.Button(win, text="Browse…", command=browse).grid(
            row=1, column=2, padx=(0, 10)
        )
        row = ttk.Frame(win)
        row.grid(row=2, column=0, columnspan=3, sticky="e", padx=10, pady=10)
        ttk.Button(row, text="OK", command=ok).pack(side="left")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(
            side="left", padx=(4, 0)
        )
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())
        entry.focus_set()
        entry.icursor("end")
        win.grab_set()
        self.wait_window(win)
        return result["v"] or None

    def _resolve_path(self, text: str, quiet: bool = False) -> Path | None:
        """Turn user text into an absolute, normalised Path. Relative paths are
        taken relative to the source folder."""
        text = os.path.expandvars(os.path.expanduser(text.strip().strip('"')))
        if not text:
            return None
        p = Path(text)
        if not p.is_absolute():
            src = self.src_var.get().strip()
            if not src:
                if not quiet:
                    messagebox.showerror(
                        "Path", "Set the source folder first, or use a full path."
                    )
                return None
            p = Path(src).expanduser() / p
        p = Path(os.path.normpath(p))
        if not valid_path(p):
            if not quiet:
                messagebox.showerror("Path", f"'{p}' is not a valid folder path.")
            return None
        return p

    # ----------------------------------------------------------- Scanning
    def scan(self, done_msg: str | None = None):
        if self._scanning or self._moving:
            return
        src = Path(self.src_var.get()).expanduser()
        if not src.is_dir():
            messagebox.showerror("Scan", "Source folder does not exist.")
            return

        # Read all tk variables here (main thread); the worker must not touch tkinter.
        skip_dirs = {Path(f) for f in self.folders}
        jp = self._resolve_path(
            self.junk_folder_var.get().strip() or "_Junk", quiet=True
        )
        if jp:
            skip_dirs.add(jp)
        recursive = self.recursive_var.get()

        self._scanning = True
        self._done_msg = done_msg
        self._cancel.clear()
        self._q = queue.Queue()
        self._total = 0

        self.scan_btn.config(state="disabled")
        self.progress.config(mode="indeterminate", value=0)
        self.progress.start(12)
        self.progress_label.config(text="Listing files…")
        self.prog_frame.pack(fill="x", before=self.bottom)
        self.status.config(text=f"Scanning {src} …")

        threading.Thread(
            target=self._scan_worker,
            args=(src, recursive, skip_dirs, self._q),
            daemon=True,
        ).start()
        self.after(50, self._poll_scan)

    def _scan_worker(self, src: Path, recursive: bool, skip_dirs: set, q: queue.Queue):
        """Phase 1: list paths (unknown total -> indeterminate bar).
        Phase 2: read file info (known total -> real percentage)."""
        try:
            paths: list[Path] = []
            last = time.monotonic()
            for root, dirs, files in os.walk(
                src
            ):  # errors (e.g. permission denied) are skipped
                if self._cancel.is_set():
                    q.put(("cancelled", None))
                    return
                rootp = Path(root)
                dirs[:] = [d for d in dirs if rootp / d not in skip_dirs]
                if not recursive:
                    dirs.clear()
                paths.extend(rootp / f for f in files)
                now = time.monotonic()
                if now - last > 0.05:
                    q.put(("listing", len(paths)))
                    last = now

            total = len(paths)
            q.put(("total", total))
            entries: list[FileEntry] = []
            for i, p in enumerate(paths, 1):
                if self._cancel.is_set():
                    q.put(("cancelled", None))
                    return
                try:
                    st = os.lstat(
                        p
                    )  # lstat: symlinks are not regular files, so they're skipped
                    if stat.S_ISREG(st.st_mode):
                        entries.append(
                            FileEntry(
                                p, p.suffix.lower() or NO_EXT, st.st_size, is_junk(p)
                            )
                        )
                except OSError:
                    pass
                now = time.monotonic()
                if now - last > 0.05 or i == total:
                    q.put(("progress", i))
                    last = now
            q.put(("done", entries))
        except Exception as ex:  # pragma: no cover
            q.put(("error", str(ex)))

    def _poll_scan(self):
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "listing":
                    self.progress_label.config(text=f"Listing files… {val:,} found")
                elif kind == "total":
                    self._total = val
                    self.progress.stop()
                    self.progress.config(
                        mode="determinate", maximum=max(val, 1), value=0
                    )
                    self.progress_label.config(text=f"Reading file info… 0 / {val:,}")
                elif kind == "progress":
                    self.progress.config(value=val)
                    self.progress_label.config(
                        text=f"Reading file info… {val:,} / {self._total:,}"
                    )
                elif kind == "done":
                    self._finish_scan(entries=val)
                    return
                elif kind == "cancelled":
                    self._finish_scan(cancelled=True)
                    return
                elif kind == "error":
                    self._finish_scan(error=val)
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll_scan)

    def _cancel_scan(self):
        if self._scanning or self._moving:
            self._cancel.set()
            self.progress_label.config(text="Cancelling…")

    def _finish_scan(self, entries=None, cancelled=False, error=None):
        self.progress.stop()
        self.prog_frame.pack_forget()
        self.scan_btn.config(state="normal")
        self._scanning = False
        if error:
            self.status.config(text="Scan failed.")
            messagebox.showerror("Scan", f"Scan failed: {error}")
        elif cancelled:
            self.status.config(text="Scan cancelled (previous results kept).")
        else:
            self.entries = entries
            self.refresh_ext_tree()
            self.status.config(
                text=self._done_msg
                or f"Scanned {len(entries):,} files in {self.src_var.get()}"
            )

    def _busy(self) -> bool:
        busy = self._scanning or self._moving
        if busy:
            messagebox.showinfo("Please wait", "An operation is still running.")
        return busy

    # ------------------------------------------------------- Tree updates
    def refresh_ext_tree(self):
        junk_on = self.junk_var.get()
        stats: dict[str, list[int]] = {}
        junk_count = 0
        for e in self.entries:
            if e.junk:
                junk_count += 1
                if junk_on:
                    continue
            s = stats.setdefault(e.ext, [0, 0])
            s[0] += 1
            s[1] += e.size
        self._ext_stats = stats
        self.junk_label.config(text=f"({junk_count} junk files detected)")

        tree = self.ext_tree
        open_cats = {i for i in tree.get_children() if tree.item(i, "open")}
        tree.delete(*tree.get_children())

        groups: dict[str, list[str]] = {}
        for ext in stats:
            groups.setdefault(category_of(ext), []).append(ext)

        for cat in CAT_ORDER:
            exts = groups.get(cat)
            if not exts:
                continue
            exts.sort(key=lambda x: (-stats[x][0], x))
            count = sum(stats[x][0] for x in exts)
            size = sum(stats[x][1] for x in exts)
            dests = {self.mapping.get(x) for x in exts}
            if dests == {None}:
                label, tags = "", ()
            elif len(dests) == 1:
                label, tags = next(iter(dests)), ("assigned",)
            else:
                label, tags = "(mixed)", ("partial",)
            iid = f"cat:{cat}"
            tree.insert(
                "",
                "end",
                iid=iid,
                text=f"{cat}  ({len(exts)} types)",
                values=(count, human_size(size), label),
                tags=tags,
                open=iid in open_cats,
            )
            for ext in exts:
                d = self.mapping.get(ext, "")
                tree.insert(
                    iid,
                    "end",
                    iid=f"ext:{ext}",
                    text=ext,
                    values=(stats[ext][0], human_size(stats[ext][1]), d),
                    tags=("assigned",) if d else (),
                )
        self.refresh_folder_tree()

    def refresh_folder_tree(self):
        tree = self.folder_tree
        sel = tree.selection()
        sel_path = self._folder_iids.get(sel[0]) if sel else None

        counts: dict[str, int] = {}
        for ext, f in self.mapping.items():
            if ext in self._ext_stats:
                counts[f] = counts.get(f, 0) + self._ext_stats[ext][0]

        tree.delete(*tree.get_children())
        self._folder_iids = {}
        for i, f in enumerate(self.folders):
            iid = f"d{i}"
            self._folder_iids[iid] = f
            status = "exists" if os.path.isdir(f) else "will be created"
            tree.insert(
                "", "end", iid=iid, text="📁 " + f, values=(counts.get(f, 0), status)
            )
            if f == sel_path:
                tree.selection_set(iid)

    # ------------------------------------------------------- Destinations
    def add_destination(self):
        text = self.ask_path(
            "Add destination",
            "Folder path (it will be created automatically if it doesn't exist):",
            initial=self.src_var.get(),
        )
        if text:
            self._add_folder(text)

    def _add_folder(self, text: str) -> str | None:
        p = self._resolve_path(text)
        if p is None:
            return None
        s = str(p)
        for f in self.folders:
            if same_path(f, s):
                return f
        self.folders.append(s)
        self.refresh_folder_tree()
        return s

    def remove_folder(self):
        sel = self.folder_tree.selection()
        if not sel:
            return
        path = self._folder_iids.get(sel[0])
        if path in self.folders:
            self.folders.remove(path)
        self.mapping = {e: f for e, f in self.mapping.items() if f != path}
        self.refresh_ext_tree()

    def auto_organize(self):
        """Pick a base folder; one sub-folder per category is added and assigned."""
        if not self._ext_stats:
            messagebox.showinfo("Auto-organize", "Scan a folder first.")
            return
        base = self.ask_path(
            "Auto-organize",
            "Base folder - a sub-folder per category (Images, Videos, …) is created inside it:",
            initial=self.src_var.get(),
        )
        if not base:
            return
        basep = self._resolve_path(base)
        if basep is None:
            return
        for cat in CAT_ORDER:
            exts = [
                x
                for x in self._ext_stats
                if category_of(x) == cat and x not in self.mapping
            ]
            if not exts:
                continue
            dest = self._add_folder(str(basep / cat))
            if dest is None:
                return
            for x in exts:
                self.mapping[x] = dest
        self.refresh_ext_tree()

    # --------------------------------------------------------- Drag & drop
    def _items_to_exts(self, iids) -> set[str]:
        exts: set[str] = set()
        for iid in iids:
            if iid.startswith("cat:"):
                exts.update(c[4:] for c in self.ext_tree.get_children(iid))
            elif iid.startswith("ext:"):
                exts.add(iid[4:])
        return exts

    def _drag_press(self, e):
        if "indicator" in str(self.ext_tree.identify_element(e.x, e.y)):
            return  # let the expand/collapse arrow work normally
        row = self.ext_tree.identify_row(e.y)
        self._press_row = row
        self._drag_started = False
        if not row:
            return
        ctrl_or_shift = e.state & 0x0005  # Shift=0x1, Ctrl=0x4
        if row in self.ext_tree.selection() and not ctrl_or_shift:
            # keep multi-selection intact so the whole group can be dragged
            self._drag_items = list(self.ext_tree.selection())
            return "break"
        self._drag_items = []  # set after the default selection handling has run
        self.after_idle(
            lambda: setattr(self, "_drag_items", list(self.ext_tree.selection()))
        )

    def _drag_motion(self, e):
        if not self._drag_items:
            return
        if not self._drag_started:
            self._drag_started = True
            self._drag_win = tk.Toplevel(self)
            self._drag_win.overrideredirect(True)
            self._drag_win.attributes("-topmost", True)
            names = [
                self.ext_tree.item(i, "text").split("  (")[0] for i in self._drag_items
            ]
            txt = ", ".join(names[:4]) + (
                f" +{len(names) - 4}" if len(names) > 4 else ""
            )
            tk.Label(
                self._drag_win,
                text=txt,
                bg="#ffffcc",
                fg="#000000",
                relief="solid",
                bd=1,
                padx=6,
                pady=2,
            ).pack()
        self._drag_win.geometry(f"+{e.x_root + 14}+{e.y_root + 10}")

        target = self.winfo_containing(e.x_root, e.y_root)
        if target is self.folder_tree:
            row = self.folder_tree.identify_row(
                e.y_root - self.folder_tree.winfo_rooty()
            )
            if row:
                self.folder_tree.selection_set(row)

    def _drag_release(self, e):
        if self._drag_win:
            self._drag_win.destroy()
            self._drag_win = None

        if not self._drag_started:
            # plain click on an already-selected row inside a multi-selection
            if (
                self._press_row
                and self._press_row in self.ext_tree.selection()
                and len(self.ext_tree.selection()) > 1
                and not (e.state & 0x0005)
            ):
                self.ext_tree.selection_set(self._press_row)
            return

        self._drag_started = False
        exts = self._items_to_exts(self._drag_items)
        target = self.winfo_containing(e.x_root, e.y_root)
        if target is self.folder_tree:
            row = self.folder_tree.identify_row(
                e.y_root - self.folder_tree.winfo_rooty()
            )
            path = self._folder_iids.get(row)
            if path:
                for ext in exts:
                    self.mapping[ext] = path
        elif target is self.ext_tree:
            for ext in exts:
                self.mapping.pop(ext, None)  # dropping back on the left = unassign
        self._drag_items = []
        self.refresh_ext_tree()

    # ----------------------------------------------------- Plan / execute
    def build_plan(self) -> list[tuple[Path, Path]] | None:
        junk_on = self.junk_var.get()
        junk_dest = None
        if junk_on and any(e.junk for e in self.entries):
            junk_dest = self._resolve_path(
                self.junk_folder_var.get().strip() or "_Junk"
            )
            if junk_dest is None:
                return None
        plan = []
        for en in self.entries:
            if junk_on and en.junk:
                target = junk_dest
            else:
                d = self.mapping.get(en.ext)
                target = Path(d) if d else None
            if target is None or same_path(en.path.parent, target):
                continue
            plan.append((en.path, target))
        return plan

    def preview(self):
        if self._busy():
            return
        plan = self.build_plan()
        if plan is None:
            return
        win = tk.Toplevel(self)
        win.title(f"Preview – {len(plan)} files will move")
        win.geometry("820x460")
        text = tk.Text(win, wrap="none")
        text.pack(fill="both", expand=True)
        if not plan:
            text.insert(
                "end",
                "Nothing to move. Drag some categories or extensions onto a destination first.",
            )
        else:
            new_dirs = sorted({t for _, t in plan if not t.exists()}, key=str)
            if new_dirs:
                text.insert("end", f"Folders that will be created ({len(new_dirs)}):\n")
                for d in new_dirs:
                    text.insert("end", f"  + {d}\n")
                text.insert("end", "\n")
            text.insert("end", "Moves:\n")
            for src, tdir in plan:
                text.insert("end", f"  {src}  →  {tdir}\n")
        text.config(state="disabled")

    # ---- Moving / undoing (background thread + progress bar) ----
    PER_FILE_WEIGHT = 64 * 1024  # so tiny files still advance the bar

    def run_move(self):
        if self._busy():
            return
        plan = self.build_plan()
        if plan is None:
            return
        if not plan:
            messagebox.showinfo("Move", "Nothing to move.")
            return
        new_dirs = {t for _, t in plan if not t.exists()}
        extra = f"\n{len(new_dirs)} new folder(s) will be created." if new_dirs else ""
        if not messagebox.askyesno(
            "Move files",
            f"Move {len(plan)} files now?{extra}\n(You can undo afterwards.)",
        ):
            return

        sizes = {e.path: e.size for e in self.entries}
        jobs = [(src, tdir, src.name, sizes.get(src, 0)) for src, tdir in plan]

        def finished(moved, errors, log, created, cancelled):
            self.move_log = log
            self.created_dirs = created
            head = (
                f"Cancelled - moved {moved} of {len(jobs)} files."
                if cancelled
                else f"Moved {moved} files."
            )
            msg = head
            if errors:
                msg += f"\n{len(errors)} errors:\n" + "\n".join(errors[:10])
            self.scan(done_msg=head)
            messagebox.showinfo("Done", msg)

        self._start_jobs(jobs, "Moving", finished)

    def undo(self):
        if self._busy():
            return
        if not self.move_log:
            messagebox.showinfo("Undo", "Nothing to undo.")
            return
        jobs = []
        for new, old in reversed(self.move_log):
            try:
                size = os.path.getsize(new)
            except OSError:
                size = 0
            jobs.append((new, old.parent, old.name, size))
        previous_log = list(self.move_log)

        def finished(restored, errors, log, created, cancelled):
            restored_srcs = {
                src for _dst, src in log
            }  # log entries are (new_location, original_location)
            self.move_log = [(n, o) for n, o in previous_log if n not in restored_srcs]
            # remove folders we created, deepest first, only if now empty
            for d in sorted(
                self.created_dirs, key=lambda p: len(p.parts), reverse=True
            ):
                try:
                    d.rmdir()
                except OSError:
                    pass
            self.created_dirs = [d for d in self.created_dirs if d.exists()]
            head = (
                f"Cancelled - restored {restored} of {len(jobs)} files."
                if cancelled
                else f"Restored {restored} files."
            )
            self.scan(done_msg=head)
            if errors:
                messagebox.showwarning(
                    "Undo", head + f"\n{len(errors)} errors:\n" + "\n".join(errors[:10])
                )

        self._start_jobs(jobs, "Restoring", finished)

    def _start_jobs(self, jobs, verb, on_finished):
        """jobs: list of (src_path, dest_dir, dest_name, size_in_bytes)."""
        self._moving = True
        self._cancel.clear()
        self._q = queue.Queue()
        self._job_files = len(jobs)
        self._job_verb = verb
        self._on_jobs_finished = on_finished
        weight = sum(j[3] + self.PER_FILE_WEIGHT for j in jobs)

        self.scan_btn.config(state="disabled")
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=max(weight, 1), value=0)
        self.progress_label.config(text=f"{verb} 0 / {len(jobs):,} files")
        self.prog_frame.pack(fill="x", before=self.bottom)
        self.status.config(text=f"{verb} files …")

        threading.Thread(
            target=self._jobs_worker, args=(jobs, self._q), daemon=True
        ).start()
        self.after(50, self._poll_jobs)

    def _jobs_worker(self, jobs, q: queue.Queue):
        moved, errors, log, created = 0, [], [], []
        done_weight, cancelled = 0, False
        last = time.monotonic()
        for i, (src, dest_dir, name, size) in enumerate(jobs, 1):
            if self._cancel.is_set():
                cancelled = True
                break
            try:
                self._make_dirs(dest_dir, created)
                dst = unique_path(dest_dir / name)
                shutil.move(str(src), str(dst))
                log.append((dst, src))
                moved += 1
            except OSError as ex:
                errors.append(f"{src.name}: {ex}")
            done_weight += size + self.PER_FILE_WEIGHT
            now = time.monotonic()
            if now - last > 0.05 or i == len(jobs):
                q.put(("progress", i, done_weight))
                last = now
        q.put(("finished", moved, errors, log, created, cancelled))

    @staticmethod
    def _make_dirs(d: Path, created: list):
        """mkdir -p, remembering which folders we actually created (for undo)."""
        missing = []
        cur = d
        while not cur.exists() and cur.parent != cur:
            missing.append(cur)
            cur = cur.parent
        d.mkdir(parents=True, exist_ok=True)
        for m in reversed(missing):
            if m not in created:
                created.append(m)

    def _poll_jobs(self):
        try:
            while True:
                msg = self._q.get_nowait()
                if msg[0] == "progress":
                    _, i, weight = msg
                    self.progress.config(value=weight)
                    self.progress_label.config(
                        text=f"{self._job_verb} {i:,} / {self._job_files:,} files"
                    )
                elif msg[0] == "finished":
                    self._finish_jobs(*msg[1:])
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll_jobs)

    def _finish_jobs(self, moved, errors, log, created, cancelled):
        self.prog_frame.pack_forget()
        self.scan_btn.config(state="normal")
        self._moving = False
        callback, self._on_jobs_finished = self._on_jobs_finished, None
        if callback:
            callback(moved, errors, log, created, cancelled)


def main():
    ap = argparse.ArgumentParser(description="Drag-and-drop file organizer")
    ap.add_argument("--folder", help="Folder to scan on startup")
    ap.add_argument(
        "--junk", action="store_true", help="Pre-enable moving junk/temp files"
    )
    ap.add_argument(
        "--junk-folder",
        default="_Junk",
        help="Junk destination path (relative = inside the source folder; default: _Junk)",
    )
    ap.add_argument(
        "--recursive", action="store_true", help="Include subfolders when scanning"
    )
    args = ap.parse_args()
    if args.folder:
        args.folder = str(Path(args.folder).expanduser())

    # Crisp text on high-DPI Windows displays (otherwise tkinter looks blurry)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    App(args).mainloop()


if __name__ == "__main__":
    main()
