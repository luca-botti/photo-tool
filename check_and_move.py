#!/usr/bin/env python3
"""
sort_files.py - Sort files from SOURCE into DONE (already in DEST) or TODO (not in DEST).

Usage:
    python sort_files.py SOURCE DEST TODO DONE [--max-cache-mb 500] [--chunk-mb 1]

Flow:
  1. Build an in-RAM cache of DEST (size, extension, path), sorted by size so we
     can binary search it. Useful when DEST is on a slow remote SMB share.
  2. Scan SOURCE (skipping TODO / DONE if they live inside it).
  3. For each source file:
       - no cached file with same size + extension -> move to TODO
       - candidates found -> byte-compare against the real file(s);
         identical -> move to DONE, otherwise -> TODO
     The relative folder structure of SOURCE is preserved in TODO / DONE.
  4. Print a summary.

Requires Python 3.8+, no third-party packages.
"""

import argparse
import bisect
import difflib
import os
import pickle
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def norm(path):
    """Normalized absolute path for comparisons (case-insensitive on Windows)."""
    return os.path.normcase(os.path.abspath(path))


def is_inside(child, parent):
    """True if `child` is the same as, or located inside, `parent`."""
    child, parent = norm(child), norm(parent)
    if child == parent:
        return True
    return child.startswith(parent.rstrip("\\/") + os.sep)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_time(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h {m:02d}m {s:02d}s" if h else f"{m:d}m {s:02d}s"


def term_width():
    return shutil.get_terminal_size((100, 20)).columns


def clear_line():
    sys.stdout.write("\r" + " " * (term_width() - 1) + "\r")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Destination cache
# --------------------------------------------------------------------------- #


class DestCache:
    """
    Sorted array of files keyed by size.
      self.sizes   : sorted list of ints (used for bisect)
      self.entries : parallel list of (extension, path)
    """

    # Rough per-entry overhead (tuple + int + list slots) on top of the path string.
    ENTRY_OVERHEAD = 160

    def __init__(self):
        self.sizes = []
        self.entries = []
        self.est_bytes = 0
        self.truncated = False

    def __len__(self):
        return len(self.sizes)

    def build(self, root, max_bytes, progress):
        """Walk `root` collecting (size, ext, path). Stops if RAM limit is hit."""
        raw = []
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                size = entry.stat(follow_symlinks=False).st_size
                                ext = os.path.splitext(entry.name)[1].lower()
                                path = entry.path
                                cost = sys.getsizeof(path) + self.ENTRY_OVERHEAD
                                if self.est_bytes + cost > max_bytes:
                                    self.truncated = True
                                    stack.clear()
                                    break
                                self.est_bytes += cost
                                raw.append((size, sys.intern(ext), path))
                                progress["count"] = len(raw)
                        except OSError:
                            continue
            except OSError:
                continue

        progress["phase"] = "sorting"
        raw.sort(key=lambda t: t[0])
        self.sizes = [t[0] for t in raw]
        self.entries = [(t[1], t[2]) for t in raw]

    # ---- persistence: avoids re-scanning the remote share on every run ---- #
    def save(self, path, root):
        data = {
            "version": 1,
            "root": norm(root),
            "created": time.time(),
            "sizes": self.sizes,
            "entries": self.entries,
            "est_bytes": self.est_bytes,
            "truncated": self.truncated,
        }
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, root, max_age_s, max_bytes):
        """Return a DestCache loaded from `path`, or None if missing/stale/unusable."""
        try:
            with open(path, "rb") as fh:
                data = pickle.load(fh)
            if data.get("version") != 1 or data.get("root") != norm(root):
                return None
            age = time.time() - data["created"]
            if age > max_age_s or data["est_bytes"] > max_bytes:
                return None
            c = cls()
            c.sizes, c.entries = data["sizes"], data["entries"]
            c.est_bytes, c.truncated = data["est_bytes"], data["truncated"]
            c.age = age
            return c
        except Exception:
            return None

    def candidates(self, size, ext):
        """Binary search on size, then filter by extension. Returns list of paths."""
        lo = bisect.bisect_left(self.sizes, size)
        hi = bisect.bisect_right(self.sizes, size)
        return [p for e, p in self.entries[lo:hi] if e == ext]


def build_cache_with_spinner(dest, max_bytes):
    cache = DestCache()
    progress = {"count": 0, "phase": "scanning"}
    done = threading.Event()
    start = time.time()

    def spin():
        frames = "|/-\\"
        i = 0
        while not done.is_set():
            msg = (
                f"\r  {frames[i % 4]} Loading destination cache "
                f"({progress['phase']})... {progress['count']:,} files "
                f"[{fmt_time(time.time() - start)}]"
            )
            sys.stdout.write(msg[: term_width() - 1].ljust(term_width() - 1))
            sys.stdout.flush()
            i += 1
            time.sleep(0.1)

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        cache.build(dest, max_bytes, progress)
    finally:
        done.set()
        t.join()
        clear_line()
    print(
        f"Destination cache ready: {len(cache):,} files, "
        f"~{fmt_size(cache.est_bytes)} RAM, {fmt_time(time.time() - start)}"
    )
    if cache.truncated:
        print(
            "  WARNING: cache RAM limit reached - the cache is INCOMPLETE. "
            "Files not found in it will go to TODO."
        )
    return cache


# --------------------------------------------------------------------------- #
# Source scan, comparison, moving
# --------------------------------------------------------------------------- #


def list_source_files(source, excluded_dirs):
    """Return all files under `source`, pruning any directory in `excluded_dirs`."""
    excluded = {norm(d) for d in excluded_dirs}
    files = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [
            d for d in dirnames if norm(os.path.join(dirpath, d)) not in excluded
        ]
        for name in filenames:
            files.append(os.path.join(dirpath, name))
    return files


_READ_POOL = ThreadPoolExecutor(max_workers=1)


def files_identical(
    a, b, chunk_size, on_progress=None, mode="full", samples=32, b_size=None
):
    """
    Compare two files. Sizes are compared first, then the first and last chunk
    (quick reject for different files). Then:
      mode="full"   -> read and compare everything (both files read in parallel)
      mode="sample" -> compare `samples` chunks spread evenly across the file
                       (very fast, but not a 100% proof of equality)
    `on_progress(bytes_done, total)` is called as the comparison advances.
    """
    try:
        size = os.path.getsize(a)
        # b_size comes from the cache, so we don't ask the remote server for it
        if size != (b_size if b_size is not None else os.path.getsize(b)):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            probe = min(chunk_size if mode == "full" else 1024 * 1024, size)
            # head
            if fa.read(probe) != fb.read(probe):
                return False
            # tail
            if size > probe:
                fa.seek(-probe, os.SEEK_END)
                fb.seek(-probe, os.SEEK_END)
                if fa.read(probe) != fb.read(probe):
                    return False

            if mode == "sample":
                span = size - 2 * probe
                if span > 0 and samples > 0:
                    sample_len = min(chunk_size, 1024 * 1024)
                    for k in range(samples):
                        off = probe + int(span * (k + 0.5) / samples)
                        off = min(off, size - probe - sample_len)
                        fa.seek(off)
                        fb.seek(off)
                        if fa.read(sample_len) != fb.read(sample_len):
                            return False
                        if on_progress:
                            on_progress(k + 1, samples)
                return True

            # full compare (head already verified, continue after it)
            fa.seek(probe)
            fb.seek(probe)
            done = probe
            if on_progress:
                on_progress(done, size)
            while True:
                fut = _READ_POOL.submit(fa.read, chunk_size)  # read both at once
                cb = fb.read(chunk_size)
                ca = fut.result()
                if ca != cb:
                    return False
                if not ca:
                    return True
                done += len(ca)
                if on_progress:
                    on_progress(done, size)
    except OSError:
        return False


def name_words(path):
    """Lowercase words of the file name (without extension), split on any separator."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return re.findall(r"[^\W_]+", stem)


def name_similarity(a, b):
    """
    Fuzzy word-based similarity between two file names, from 0.0 to 1.0.
    Each word of the name with fewer words is looked up in the other name
    (exact match or >= 80% similar, so small typos still count). Extra tags in
    the longer name (1080p, x264, release group...) do not lower the score.
    """
    wa, wb = name_words(a), name_words(b)
    if not wa or not wb:
        return 1.0 if wa == wb else 0.0
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    matched = 0
    for w in small:
        if any(
            w == x or difflib.SequenceMatcher(None, w, x).ratio() >= 0.8 for x in big
        ):
            matched += 1
    return matched / len(small)


def unique_path(path):
    """If `path` exists, append ' (1)', ' (2)', ... before the extension."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def move_preserving_structure(src_file, source_root, target_root):
    rel = os.path.relpath(src_file, source_root)
    target = os.path.join(target_root, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    target = unique_path(target)
    shutil.move(src_file, target)
    return target


class ProgressBar:
    def __init__(self, total):
        self.total = max(total, 1)
        self.start = time.time()
        self.last = 0

    def update(self, current, label="", force=False, extra=""):
        now = time.time()
        if not force and now - self.last < 0.1:
            return
        self.last = now
        frac = current / self.total
        elapsed = now - self.start
        eta = (elapsed / current) * (self.total - current) if current else 0
        width = term_width() - 1
        info = f" {current:,}/{self.total:,} {frac * 100:5.1f}% ETA {fmt_time(eta)} {extra} "
        bar_len = max(10, min(30, width - len(info) - 4))
        filled = int(bar_len * frac)
        bar = "#" * filled + "-" * (bar_len - filled)
        line = f"[{bar}]{info}"
        room = width - len(line)
        if room > 8 and label:
            label = os.path.basename(label)
            line += label if len(label) <= room else "..." + label[-(room - 3) :]
        sys.stdout.write("\r" + line[:width].ljust(width))
        sys.stdout.flush()

    def finish(self):
        clear_line()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main():
    ap = argparse.ArgumentParser(
        description="Sort source files into TODO / DONE based on a destination folder."
    )
    ap.add_argument("source")
    ap.add_argument("dest")
    ap.add_argument("todo")
    ap.add_argument("done")
    ap.add_argument(
        "--max-cache-mb",
        type=int,
        default=500,
        help="RAM limit for the destination cache (default 500)",
    )
    ap.add_argument(
        "--chunk-mb",
        type=int,
        default=8,
        help="chunk size used when comparing files (default 8)",
    )
    ap.add_argument(
        "--mode",
        choices=("full", "sample"),
        default="full",
        help="full = compare every byte (default); sample = compare head, tail and "
        "evenly spaced chunks only (much faster, not a 100%% proof)",
    )
    ap.add_argument(
        "--samples",
        type=int,
        default=32,
        help="number of chunks compared in sample mode (default 32)",
    )
    ap.add_argument(
        "--name-threshold",
        type=float,
        default=0.5,
        help="sample mode only: minimum file-name word similarity 0..1 (default 0.5)",
    )
    ap.add_argument(
        "--cache-file",
        metavar="FILE",
        help="save the destination cache here and reuse it on later runs "
        "(avoids re-scanning a remote share)",
    )
    ap.add_argument(
        "--cache-max-age-hours",
        type=float,
        default=24,
        help="ignore a cache file older than this (default 24)",
    )
    ap.add_argument(
        "--refresh-cache",
        action="store_true",
        help="ignore the existing cache file and rebuild it",
    )
    ap.add_argument(
        "--report",
        metavar="FILE",
        help="also write the list of non-matching candidates to this text file",
    )
    args = ap.parse_args()

    source, dest, todo, done = (
        os.path.abspath(p) for p in (args.source, args.dest, args.todo, args.done)
    )

    for label, p in (("source", source), ("dest", dest)):
        if not os.path.isdir(p):
            sys.exit(f"Error: {label} folder does not exist: {p}")
    if norm(todo) == norm(done):
        sys.exit("Error: todo and done must be different folders")
    for label, p in (("todo", todo), ("done", done)):
        if is_inside(source, p):
            sys.exit(f"Error: source cannot be inside the {label} folder")
        os.makedirs(p, exist_ok=True)

    chunk = args.chunk_mb * 1024 * 1024

    # In sample mode a match is not a 100% proof, so those files go in done/maybe-done
    done_target = os.path.join(done, "maybe-done") if args.mode == "sample" else done
    t0 = time.time()

    # 1) destination cache
    max_cache_bytes = args.max_cache_mb * 1024 * 1024
    cache = None
    if args.cache_file and not args.refresh_cache:
        cache = DestCache.load(
            args.cache_file, dest, args.cache_max_age_hours * 3600, max_cache_bytes
        )
        if cache:
            print(
                f"Destination cache loaded from {args.cache_file}: {len(cache):,} files, "
                f"age {fmt_time(cache.age)} (no request sent to the destination)"
            )
            if cache.truncated:
                print(
                    "  WARNING: this cache was truncated by the RAM limit - it is INCOMPLETE."
                )
    if cache is None:
        cache = build_cache_with_spinner(dest, max_cache_bytes)
        if args.cache_file:
            try:
                cache.save(args.cache_file, dest)
                print(f"Cache saved to {args.cache_file}")
            except OSError as exc:
                print(f"  Could not save cache file: {exc}")

    # 2) source file list (excluding todo / done when they're inside source)
    excluded = [p for p in (todo, done) if is_inside(p, source)]
    print("Listing source files...", end="", flush=True)
    files = list_source_files(source, excluded)
    clear_line()
    print(
        f"Source files to process: {len(files):,}"
        + (f" (excluded: {', '.join(excluded)})" if excluded else "")
    )

    # 3) process
    stats = {
        "done": 0,
        "done_bytes": 0,
        "todo_nomatch": 0,
        "todo_mismatch": 0,
        "todo_name": 0,
        "todo_bytes": 0,
        "errors": 0,
    }
    differ = []  # (source_path, [(candidate_path, reason), ...])
    errors = []
    bar = ProgressBar(len(files))
    bar.update(0, force=True)

    for i, path in enumerate(files, 1):
        bar.update(i - 1, path, force=True)
        try:
            size = os.path.getsize(path)
            ext = os.path.splitext(path)[1].lower()
            cands = cache.candidates(size, ext)
            if len(cands) > 1:  # try the most likely candidate first
                cands.sort(key=lambda c: -name_similarity(path, c))

            if not cands:
                move_preserving_structure(path, source, todo)
                stats["todo_nomatch"] += 1
                stats["todo_bytes"] += size
                continue

            def on_progress(done_bytes, total, _i=i, _path=path):
                pct = int(done_bytes * 100 / total) if total else 100
                bar.update(_i - 1, _path, extra=f"comparing {pct}%")

            match = False
            details = []
            content_checked = False
            for c in cands:
                if args.mode == "sample":
                    score = name_similarity(path, c)
                    if score < args.name_threshold:
                        details.append(
                            (c, f"file name too different (similarity {score:.2f})")
                        )
                        continue
                content_checked = True
                if files_identical(
                    path, c, chunk, on_progress, args.mode, args.samples, b_size=size
                ):
                    match = True
                    break
                details.append((c, "content differs"))

            if match:
                move_preserving_structure(path, source, done_target)
                stats["done"] += 1
                stats["done_bytes"] += size
            else:
                move_preserving_structure(path, source, todo)
                stats["todo_mismatch" if content_checked else "todo_name"] += 1
                stats["todo_bytes"] += size
                differ.append((path, details))
        except Exception as exc:  # keep going on any per-file error
            stats["errors"] += 1
            errors.append((path, str(exc)))

    bar.update(len(files), force=True)
    bar.finish()

    # 4) summary
    todo_total = stats["todo_nomatch"] + stats["todo_mismatch"] + stats["todo_name"]
    print()
    print("=" * 60)
    print(" SUMMARY")
    print("=" * 60)
    print(
        f" Comparison mode        : {args.mode}"
        + (f" ({args.samples} samples)" if args.mode == "sample" else "")
    )
    print(f" Source files processed : {len(files):,}")
    done_label = (
        "Moved to DONE/maybe-done" if args.mode == "sample" else "Moved to DONE"
    )
    print(f" {done_label:<23}: {stats['done']:,}  ({fmt_size(stats['done_bytes'])})")
    print(
        f" Moved to TODO          : {todo_total:,}  ({fmt_size(stats['todo_bytes'])})"
    )
    print(f"    - no match in dest  : {stats['todo_nomatch']:,}")
    print(f"    - same size/ext but different content: {stats['todo_mismatch']:,}")
    if args.mode == "sample":
        print(
            f"    - same size/ext but file name too different: {stats['todo_name']:,}"
        )
    print(f" Errors                 : {stats['errors']:,}")
    print(
        f" Dest cache             : {len(cache):,} files, ~{fmt_size(cache.est_bytes)}"
        + ("  (TRUNCATED - RAM limit hit)" if cache.truncated else "")
    )
    print(f" Total time             : {fmt_time(time.time() - t0)}")

    if differ:
        lines = []
        for src, details in differ:
            lines.append(f"SOURCE: {src}")
            for cand, reason in details:
                lines.append(f"   vs DEST: {cand}")
                lines.append(f"            -> {reason}")
        print(
            f"\n Files with same size/extension but judged DIFFERENT ({len(differ)}) - verify manually:"
        )
        for line in lines:
            print("  " + line)
        if args.report:
            try:
                with open(args.report, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                print(f"\n (list also saved to {args.report})")
            except OSError as exc:
                print(f"\n Could not write report file: {exc}")
    if errors:
        print("\n Errors detail:")
        for p, e in errors[:20]:
            print(f"   {p}: {e}")
        if len(errors) > 20:
            print(f"   ... and {len(errors) - 20} more")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear_line()
        print("\nInterrupted by user.")
        sys.exit(130)
