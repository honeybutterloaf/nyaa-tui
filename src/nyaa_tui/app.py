#!/usr/bin/env python3
"""nyaa_tui - search nyaa.si, read descriptions, download with aria2c, play with mpv.

Required:  pip install textual feedparser
Optional:  pip install textual-image pillow numpy  (inline images in descriptions)
Tools:     aria2c, mpv (in PATH)
"""
from __future__ import annotations

import html
import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import feedparser
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, DirectoryTree, Footer, Header, Input, Markdown, Static
from textual.worker import get_current_worker

try:  # optional: inline image rendering
    from PIL import Image as PILImage
    from textual_image.widget import Image as TImage

    HAS_IMAGES = True
    IMAGE_ERROR = ""
except Exception as _exc:  # ImportError, or terminal-query problems
    HAS_IMAGES = False
    IMAGE_ERROR = f"{type(_exc).__name__}: {_exc}"

BASE_URL = "https://nyaa.si"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) nyaa-tui/3.0"
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v", ".ts"}
MAX_IMAGES = 12
MAX_IMAGE_BYTES = 10_000_000
TRACKERS = [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]
# (label, nyaa category code)
CATEGORIES = [
    ("Anime - English", "1_2"),
    ("Anime - All", "1_0"),
    ("Anime - Raw", "1_4"),
    ("Everything", "0_0"),
]
FILTERS = [("No filter", 0), ("No remakes", 1), ("Trusted only", 2)]

# Client-side language filter modes, cycled with "e"
LANG_MODES = [
    ("Any language", None),
    ("EN dub or sub", {"dub", "sub"}),
    ("EN dub only", {"dub"}),
    ("EN sub only (JP audio)", {"sub"}),
]

# Nyaa has no structured language metadata, so we guess from the release title.
DUB_RE = re.compile(
    r"eng(lish)?[\s._-]*dub(bed)?|dual[\s._-]*audio|multi[\s._-]*audio"
    r"|\[dub\]|\(dub\)",
    re.I,
)
SUB_RE = re.compile(
    r"eng(lish)?[\s._-]*sub(s|bed|title[sd]?)?|multi[\s._-]*sub(s|title[sd]?)?"
    r"|multiple[\s._-]*subtitles?|\besubs?\b|soft[\s._-]?subs?|hard[\s._-]?subs?"
    r"|subsplease|erai-raws|\[asw\]|\[judas\]|\[ember\]|\[animetime\]",
    re.I,
)


def classify(title: str) -> str | None:
    """Return 'dub' (English audio), 'sub' (English subs, JP audio) or None."""
    if DUB_RE.search(title):
        return "dub"
    if SUB_RE.search(title):
        return "sub"
    return None


def lang_style(lang: str | None) -> tuple[str, str]:
    if lang == "dub":
        return "bold yellow", "DUB"
    if lang == "sub":
        return "green", "SUB"
    return "", ""


SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ENTER_MARK = "⏎"
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
PROGRESS_RE = re.compile(
    r"\[#\w+\s+(?P<done>[\d.]+(?:[KMGT]i)?B)/(?P<total>[\d.]+(?:[KMGT]i)?B)"
    r"(?:\((?P<pct>\d+)%\))?(?:\s+CN:(?P<cn>\d+))?(?:\s+SD:(?P<sd>\d+))?"
    r"(?:\s+DL:(?P<dl>[\d.]+(?:[KMGT]i)?B))?(?:\s+ETA:(?P<eta>[^\]\s]+))?"
)


def spin_frame(speed: float = 10.0) -> str:
    return SPIN_FRAMES[int(time.time() * speed) % len(SPIN_FRAMES)]


def parse_size(text: str | None) -> float:
    m = re.fullmatch(r"([\d.]+)([KMGT]?i?B)", text or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1)) * _UNITS.get(m.group(2), 1)
    except ValueError:
        return 0.0


def fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GiB"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def progress_bar(pct: float, width: int = 20) -> Text:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    bar = Text("█" * filled, style="green")
    bar.append("░" * (width - filled), style="grey37")
    bar.append(f" {pct:3.0f}%")
    return bar


class Spinner(Static):
    """Animated braille spinner with a label."""

    def __init__(self, label: str = "", **kwargs):
        super().__init__("", **kwargs)
        self.label = label

    def on_mount(self) -> None:
        self.set_interval(0.08, self._tick)
        self._tick()

    def _tick(self) -> None:
        self.update(Text(f"{spin_frame(12.5)} {self.label}"))


def new_download_state(title: str, proc: subprocess.Popen) -> dict:
    return {
        "title": title,
        "proc": proc,
        "cancelled": False,
        "started": time.time(),
        "pct": None,  # None -> still connecting / fetching metadata
        "done": 0.0,
        "total": 0.0,
        "speed": 0.0,
        "eta": "",
        "conns": 0,
        "seeds": 0,
    }


def read_progress(d: dict) -> None:
    """Thread target: parse aria2c's console summary into the download dict."""
    try:
        for line in d["proc"].stdout:  # type: ignore[union-attr]
            m = PROGRESS_RE.search(line)
            if not m:
                continue
            total = parse_size(m["total"])
            d["done"] = parse_size(m["done"])
            d["total"] = total
            d["speed"] = parse_size(m["dl"])
            d["eta"] = m["eta"] or ""
            d["conns"] = int(m["cn"] or 0)
            d["seeds"] = int(m["sd"] or 0)
            if m["pct"] is not None and total > 0:
                d["pct"] = float(m["pct"])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
def http_get(url: str, max_bytes: int = 3_000_000, timeout: int = 15, truncate_ok: bool = True) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": BASE_URL + "/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        if not truncate_ok:
            raise ValueError("response too large")
        data = data[:max_bytes]
    return data


def build_magnet(infohash: str, title: str) -> str:
    parts = [f"magnet:?xt=urn:btih:{infohash}", f"dn={urllib.parse.quote(title)}"]
    parts += [f"tr={urllib.parse.quote(t, safe='')}" for t in TRACKERS]
    return "&".join(parts)


def fetch_feed(query: str, category: str, nyaa_filter: int) -> list[dict]:
    """Blocking. Run in a thread. Raises on failure."""
    params = {"page": "rss", "q": query, "c": category, "f": nyaa_filter, "s": "seeders", "o": "desc"}
    url = f"{BASE_URL}/?" + urllib.parse.urlencode(params)
    feed = feedparser.parse(http_get(url))
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"Could not parse feed: {feed.bozo_exception}")

    results = []
    for e in feed.entries:
        infohash = e.get("nyaa_infohash", "")
        title = e.get("title", "Unknown")
        view_url = e.get("id", "")
        if not view_url.startswith("http"):
            view_url = ""
        results.append(
            {
                "title": title,
                "size": e.get("nyaa_size", "?"),
                "seeders": int(e.get("nyaa_seeders", 0) or 0),
                "leechers": int(e.get("nyaa_leechers", 0) or 0),
                "downloads": e.get("nyaa_downloads", "0"),
                "magnet": build_magnet(infohash, title) if infohash else e.get("link", ""),
                "lang": classify(title),
                "view_url": view_url,
                "category": e.get("nyaa_category", ""),
                "published": e.get("published", ""),
                "trusted": e.get("nyaa_trusted", "") == "Yes",
                "remake": e.get("nyaa_remake", "") == "Yes",
                "comments": e.get("nyaa_comments", "0"),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# Description parsing: nyaa page -> list of ("md", text) / ("img", url) blocks
# --------------------------------------------------------------------------- #
def extract_description_html(page: str) -> str:
    """Return the inner HTML of <div id="torrent-description">."""
    m = re.search(r'<div[^>]*id=["\']torrent-description["\'][^>]*>', page, re.I)
    if not m:
        return ""
    pos = m.end()
    depth = 1
    for t in re.finditer(r"<(/?)div\b[^>]*>", page[pos:], re.I):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return page[pos : pos + t.start()]
    return page[pos:]


class DescriptionParser(HTMLParser):
    """Minimal HTML -> Markdown converter that pulls images out as separate blocks."""

    def __init__(self, base: str = BASE_URL):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.blocks: list[tuple[str, str]] = []
        self.md: list[str] = []
        self.links: list[tuple[str, list[str], int, int]] = []
        self.lists: list[list] = []
        self.in_pre = False
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    # -- helpers
    def _buf(self) -> list[str]:
        return self.cell if self.cell is not None else self.md

    def _text(self, s: str) -> None:
        self._buf().append(s)

    def _ends(self, suffix: str) -> bool:
        return "".join(self.md).endswith(suffix)

    def _break(self) -> None:
        if self.cell is not None:
            return
        if self.md and not self._ends("\n\n"):
            self.md.append("\n\n")

    def _newline(self) -> None:
        if self.cell is not None:
            return
        if self.md and not self._ends("\n"):
            self.md.append("\n")

    def flush(self) -> None:
        text = "".join(self.md).strip()
        if text:
            self.blocks.append(("md", text))
        self.md.clear()

    def _abs(self, url: str) -> str:
        return urllib.parse.urljoin(self.base + "/", url)

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._break()
            self._text("#" * int(tag[1]) + " ")
        elif tag in ("p", "div"):
            self._break()
        elif tag == "br":
            self._text(" " if self.cell is not None else "  \n")
        elif tag == "hr":
            self._break()
            self._text("---")
            self._break()
        elif tag in ("strong", "b"):
            self._text("**")
        elif tag in ("em", "i"):
            self._text("*")
        elif tag in ("del", "s", "strike"):
            self._text("~~")
        elif tag == "code" and not self.in_pre:
            self._text("`")
        elif tag == "pre":
            self._break()
            self._text("```\n")
            self.in_pre = True
        elif tag == "blockquote":
            self._break()
            self._text("> ")
        elif tag in ("ul", "ol"):
            if not self.lists:
                self._break()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._newline()
            if self.lists:
                self.lists[-1][1] += 1
                marker = "- " if self.lists[-1][0] == "ul" else f"{self.lists[-1][1]}. "
                self._text("  " * (len(self.lists) - 1) + marker)
        elif tag == "a":
            buf = self._buf()
            self.links.append((self._abs(a.get("href") or ""), buf, len(buf), len(self.blocks)))
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            if src:
                url = self._abs(src)
                if self.cell is not None:
                    self.cell.append(f"[image]({url})")
                else:
                    self.flush()
                    self.blocks.append(("img", url))
        elif tag == "table":
            self._break()
            self.flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.table is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "blockquote"):
            self._break()
        elif tag in ("strong", "b"):
            self._text("**")
        elif tag in ("em", "i"):
            self._text("*")
        elif tag in ("del", "s", "strike"):
            self._text("~~")
        elif tag == "code" and not self.in_pre:
            self._text("`")
        elif tag == "pre":
            self._text("\n```")
            self.in_pre = False
            self._break()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self._break()
        elif tag == "a" and self.links:
            href, buf, start, nblocks = self.links.pop()
            label = "".join(buf[start:]).strip()
            del buf[start:]
            if len(self.blocks) != nblocks:  # the link wrapped an image
                self.flush()
                if href:
                    self.blocks.append(("md", f"[link]({href})"))
            elif href:
                buf.append(f"[{label or href}]({href})")
            else:
                buf.append(label)
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip().replace("|", "\\|").replace("\n", " "))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            rows = [r for r in self.table if r]
            if rows:
                n = max(len(r) for r in rows)
                rows = [r + [""] * (n - len(r)) for r in rows]
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * n]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                self._text("\n".join(lines))
                self._break()
            self.table = None

    def handle_data(self, data):
        if self.in_pre:
            self._text(data)
            return
        data = re.sub(r"\s+", " ", data)
        if not data.strip() and (not self._buf() or "".join(self._buf()).endswith(("\n", " "))):
            return
        self._text(data)

    def result(self) -> list[tuple[str, str]]:
        self.flush()
        return self.blocks


_IMG_MD = r'!\[[^\]]*\]\(\s*<?[^)\s>]+>?(?:\s+"[^"]*")?\s*\)'
_LINKED_IMG = re.compile(r"\[\s*(" + _IMG_MD + r")\s*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_IMG_CAPTURE = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')


def _hard_breaks(text: str) -> str:
    """Nyaa renders markdown with line breaks preserved; mimic that."""
    lines = text.split("\n")
    out, fence = [], False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            fence = not fence
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (
            not fence
            and line.strip()
            and nxt.strip()
            and not line.endswith("  ")
            and not line.lstrip().startswith("|")
        ):
            out.append(line + "  ")
        else:
            out.append(line)
    return "\n".join(out)


def blocks_from_markdown(text: str) -> list[tuple[str, str]]:
    text = html.unescape(text).replace("\r\n", "\n").strip()
    text = _LINKED_IMG.sub(r"\1\n\n[link](\2)", text)
    blocks: list[tuple[str, str]] = []
    last = 0
    for m in _IMG_CAPTURE.finditer(text):
        before = text[last : m.start()]
        if before.strip():
            blocks.append(("md", _hard_breaks(before.strip())))
        blocks.append(("img", urllib.parse.urljoin(BASE_URL + "/", m.group(1))))
        last = m.end()
    tail = text[last:]
    if tail.strip():
        blocks.append(("md", _hard_breaks(tail.strip())))
    return blocks


def parse_description(page: str) -> list[tuple[str, str]]:
    inner = extract_description_html(page)
    if not inner.strip():
        return []
    # Nyaa usually ships the raw markdown (rendered client-side); handle both cases.
    if re.search(r"<\s*(p|br|h[1-6]|ul|ol|li|a|img|table|strong|em|div|pre|blockquote)\b", inner, re.I):
        parser = DescriptionParser()
        parser.feed(inner)
        parser.close()
        return parser.result()
    return blocks_from_markdown(inner)


def fetch_description(view_url: str) -> list[tuple[str, str]]:
    page = http_get(view_url, max_bytes=3_000_000).decode("utf-8", errors="replace")
    return parse_description(page)


# --------------------------------------------------------------------------- #
# Widgets / screens
# --------------------------------------------------------------------------- #
class VideoTree(DirectoryTree):
    """Directory tree that hides dotfiles, aria2 control files and non-video files."""

    def filter_paths(self, paths):
        return [p for p in paths if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() in VIDEO_EXT)]

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        if self.has_focus and node is self.cursor_node:
            hint = "play" if node.data and not node.allow_expand else ("close" if node.is_expanded else "open")
            label = label.copy()
            label.append(f"  {ENTER_MARK} {hint}", style="bold yellow")
        return label


class DirectoryPromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DirectoryPromptScreen { align: center middle; }
    #dialog { padding: 1 3; background: $surface; border: thick $accent; width: 64; height: auto; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    """

    def __init__(self, current_dir: Path):
        super().__init__()
        self.current_dir = current_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Download directory (Enter to confirm, Esc to cancel):", id="dialog-title")
            yield Input(value=str(self.current_dir), id="dir-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        self.dismiss(str(Path(value).expanduser()) if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DetailScreen(Screen):
    """Torrent page: metadata, description (markdown, clickable links) and images."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
        Binding("backspace", "back", "Back", show=False),
        Binding("d", "download", "Download"),
        Binding("o", "open_page", "Open in browser"),
    ]

    DEFAULT_CSS = """
    DetailScreen { layout: vertical; }
    #d-title { text-style: bold; padding: 0 1; }
    #d-meta { color: $text-muted; padding: 0 1; }
    #body { height: 1fr; border: round $accent; padding: 0 1; }
    #body Markdown { margin: 0; padding: 0; }
    .ph { color: $text-muted; margin: 1 0; }
    .shot { width: auto; height: auto; max-height: 28; margin: 1 0; }
    """

    def __init__(self, result: dict):
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        r = self.result
        style, tag = lang_style(r["lang"])
        title = Text(f"[{tag}] " if tag else "", style=style)
        title.append(r["title"], style=style)
        flags = ["Trusted" if r["trusted"] else "", "Remake" if r["remake"] else ""]
        meta = " | ".join(
            x
            for x in [
                r["size"],
                f"S:{r['seeders']} L:{r['leechers']}",
                f"{r['downloads']} downloads",
                f"{r['comments']} comments",
                r["category"],
                r["published"],
                *flags,
            ]
            if x
        )
        yield Header()
        yield Static(title, id="d-title")
        yield Static(Text(meta), id="d-meta")
        with VerticalScroll(id="body"):
            yield Spinner("Loading description...", id="loading")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#body").focus()
        if self.result["view_url"]:
            self.load_description(self.result["view_url"])
        else:
            self.call_later(self.show_error, "No page URL available for this entry.")

    # -- description
    @work(thread=True, exclusive=True, group="detail")
    def load_description(self, url: str) -> None:
        worker = get_current_worker()
        try:
            blocks = fetch_description(url)
        except Exception as exc:
            if not worker.is_cancelled:
                self.app.call_from_thread(self.show_error, f"{type(exc).__name__}: {exc}")
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self.show_blocks, blocks)

    async def show_error(self, message: str) -> None:
        body = self.query_one("#body", VerticalScroll)
        await self.query_one("#loading").remove()
        await body.mount(Static(Text(f"Could not load description: {message}")))

    async def show_blocks(self, blocks: list[tuple[str, str]]) -> None:
        body = self.query_one("#body", VerticalScroll)
        await self.query_one("#loading").remove()
        if not blocks:
            await body.mount(Static(Text("No description provided.")))
            return

        widgets, pending, n_img = [], [], 0
        for kind, value in blocks:
            if kind == "md":
                widgets.append(Markdown(value))
            elif HAS_IMAGES and n_img < MAX_IMAGES:
                n_img += 1
                ph = Spinner(f"Loading image {n_img}: {value}", classes="ph")
                widgets.append(ph)
                pending.append((value, ph))
            else:
                widgets.append(Markdown(f"[Image]({value})"))
        await body.mount(*widgets)
        for url, ph in pending:
            self.load_image(url, ph)

    # -- images
    @work(thread=True, group="images")
    def load_image(self, url: str, placeholder: Static) -> None:
        worker = get_current_worker()
        try:
            data = http_get(url, max_bytes=MAX_IMAGE_BYTES, timeout=20, truncate_ok=False)
            img = PILImage.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGB")
            img.thumbnail((1600, 1600))
            outcome: tuple[str, object] = ("ok", img)
        except Exception as exc:
            outcome = ("err", exc)
        if not worker.is_cancelled:
            self.app.call_from_thread(self.image_ready, placeholder, url, outcome)

    async def image_ready(self, placeholder: Static, url: str, outcome: tuple[str, object]) -> None:
        if not placeholder.is_attached:
            return
        body = self.query_one("#body", VerticalScroll)
        if outcome[0] == "ok":
            widget = TImage(outcome[1], classes="shot")
        else:
            reason = str(outcome[1]).replace("[", "(").replace("]", ")")[:80]
            widget = Markdown(f"[Image failed: {reason} - open link]({url})")
        await body.mount(widget, before=placeholder)
        await placeholder.remove()

    # -- events / actions
    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        webbrowser.open(event.href)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_download(self) -> None:
        self.app.start_download(self.result)  # type: ignore[attr-defined]

    def action_open_page(self) -> None:
        if self.result["view_url"]:
            webbrowser.open(self.result["view_url"])


class DownloadsScreen(Screen):
    """Active downloads with progress bars and cancel."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
        Binding("x", "cancel", "Cancel selected"),
        Binding("delete", "cancel", "Cancel selected", show=False),
        Binding("X", "cancel_all", "Cancel all"),
    ]

    DEFAULT_CSS = """
    DownloadsScreen { layout: vertical; }
    #dl-table { height: 1fr; margin: 0 1; border: round $accent; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="dl-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dl-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Title", width=34)
        table.add_column("Progress", width=28)
        table.add_column("Size", width=17)
        table.add_column("Speed", width=10)
        table.add_column("ETA", width=8)
        table.add_column("Peers", width=5)
        table.focus()
        self.rows_shown = -1
        self.refresh_table()
        self.set_interval(0.25, self.refresh_table)

    @staticmethod
    def row_cells(d: dict) -> list:
        if d["cancelled"]:
            return [Text(d["title"]), Text("cancelling...", style="yellow"), "", "", "", ""]
        if d["pct"] is None:
            label = "fetching metadata" if d["total"] == 0 else "starting"
            return [Text(d["title"]), Text(f"{spin_frame()} {label}", style="cyan"), "", "", "", str(d["conns"])]
        size = f"{fmt_size(d['done'])}/{fmt_size(d['total'])}"
        return [
            Text(d["title"]),
            progress_bar(d["pct"]),
            size,
            f"{fmt_size(d['speed'])}/s",
            d["eta"] or "-",
            str(d["conns"]),
        ]

    def refresh_table(self) -> None:
        table = self.query_one("#dl-table", DataTable)
        active = self.app.active_downloads()  # type: ignore[attr-defined]
        if len(active) == self.rows_shown:  # update in place, no flicker / scroll reset
            for r, d in enumerate(active):
                for c, value in enumerate(self.row_cells(d)):
                    table.update_cell_at(Coordinate(r, c), value)
        else:
            row = table.cursor_row
            table.clear()
            for d in active:
                table.add_row(*self.row_cells(d))
            if active:
                table.move_cursor(row=min(row, len(active) - 1))
            self.rows_shown = len(active)
        self.sub_title = (
            f"Downloads ({len(active)} active) - x cancel, X cancel all" if active else "No active downloads"
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cancel(self) -> None:
        active = self.app.active_downloads()  # type: ignore[attr-defined]
        row = self.query_one("#dl-table", DataTable).cursor_row
        if 0 <= row < len(active):
            self.app.cancel_download(active[row])  # type: ignore[attr-defined]
            self.refresh_table()

    def action_cancel_all(self) -> None:
        for d in self.app.active_downloads():  # type: ignore[attr-defined]
            self.app.cancel_download(d)  # type: ignore[attr-defined]
        self.refresh_table()


class WatchHistoryScreen(Screen):
    """Playback history with progress, position, and last-played timestamps."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
    ]

    DEFAULT_CSS = """
    WatchHistoryScreen { layout: vertical; }
    #watch-heading { padding: 1 2 0 2; text-style: bold; color: $text; }
    #watch-summary { padding: 0 2 1 2; color: $text-muted; }
    #watch-table { height: 1fr; margin: 0 1 1 1; border: round $accent; }
    #watch-empty { height: 1fr; content-align: center middle; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("YOUR WATCH PROGRESS", id="watch-heading")
        yield Static("", id="watch-summary")
        yield Static(
            "Nothing watched yet — play a video from your files to start tracking.",
            id="watch-empty",
        )
        yield DataTable(id="watch-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#watch-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Video", width=44)
        table.add_column("Progress", width=25)
        table.add_column("Position", width=21)
        table.add_column("Last watched", width=20)
        table.focus()
        self.refresh_table()
        self.set_interval(2, self.refresh_table)

    def refresh_table(self) -> None:
        history = self.app.watch_history_snapshot()  # type: ignore[attr-defined]
        table = self.query_one("#watch-table", DataTable)
        empty = self.query_one("#watch-empty", Static)
        table.clear()
        entries = sorted(
            history.values(),
            key=lambda item: str(item.get("last_watched", "")),
            reverse=True,
        )
        watched_seconds = sum(float(item.get("position", 0)) for item in entries)
        finished = sum(
            1
            for item in entries
            if float(item.get("duration", 0)) > 0
            and float(item.get("position", 0)) / float(item["duration"]) >= 0.9
        )
        self.query_one("#watch-summary", Static).update(
            f"{len(entries)} videos  ·  {finished} finished  ·  "
            f"{fmt_duration(watched_seconds)} total progress"
        )
        for item in entries:
            duration = float(item.get("duration", 0))
            position = min(float(item.get("position", 0)), duration) if duration > 0 else 0
            pct = position / duration * 100 if duration > 0 else 0
            try:
                last_watched = datetime.fromisoformat(item["last_watched"]).astimezone().strftime("%Y-%m-%d %H:%M")
            except (KeyError, TypeError, ValueError):
                last_watched = "Unknown"
            table.add_row(
                item.get("title", Path(item.get("path", "")).name),
                progress_bar(pct, 17) if duration > 0 else Text("Reading file…", style="dim"),
                f"{fmt_duration(position)} / {fmt_duration(duration)}" if duration > 0 else "Duration unavailable",
                last_watched,
            )
        table.display = bool(entries)
        empty.display = not entries

    def action_back(self) -> None:
        self.app.pop_screen()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class NyaaTUI(App):
    TITLE = "Nyaa"

    CSS = """
    Screen { layout: vertical; background: $surface; }
    Header { background: $primary; color: $text; }
    Footer { background: $panel; }
    #search-bar { margin: 1 2 0 2; border: tall $primary; }
    #main { height: 1fr; margin: 1 2; }
    .pane { height: 1fr; border: round $primary; background: $panel; }
    DataTable, DirectoryTree { height: 1fr; background: transparent; }
    DataTable > .datatable--header { background: $boost; color: $text; text-style: bold; }
    DataTable > .datatable--cursor { background: $primary-darken-1; }
    #status-bar { height: 1; background: $boost; padding: 0 2; color: $text; }
    #dl-bar { height: 1; padding: 0 2; background: $panel; display: none; }
    """

    BINDINGS = [
        Binding("/", "focus_search", "Search"),
        Binding("t", "toggle_view", "Search/Files"),
        Binding("d", "download", "Download"),
        Binding("x", "downloads", "Downloads"),
        Binding("p", "play", "Play"),
        Binding("w", "watch_history", "Watch progress"),
        Binding("s", "set_dir", "Set dir"),
        Binding("c", "cycle_category", "Category"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("e", "cycle_lang", "EN sub/dub"),
        Binding("r", "refresh_tree", "Refresh files"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.download_dir = Path("~/Videos/Anime").expanduser()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.category_idx = 0
        self.filter_idx = 0
        self.lang_idx = 0
        self.results: list[dict] = []
        self.shown: list[dict] = []
        self.view = "search"
        self.last_query = ""
        self.busy = ""
        self.downloads: list[dict] = []
        self.watch_history_path = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "nyaa-tui"
            / "watch-history.json"
        )
        self.watch_lock = threading.Lock()
        self.watch_history = self.load_watch_history()

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(
            placeholder="Search nyaa.si - English or Japanese title (e.g. Frieren, 葬送のフリーレン)",
            id="search-bar",
        )
        with Container(id="main"):
            with Container(classes="pane", id="search-pane"):
                yield DataTable(id="results")
            with Container(classes="pane", id="files-pane"):
                yield VideoTree(self.download_dir, id="file-tree")
        yield Static("", id="dl-bar")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        # Keep widget references: App.query_one only sees the *current* screen,
        # which breaks while the detail screen is open.
        self.search_input = self.query_one("#search-bar", Input)
        self.results_table = self.query_one("#results", DataTable)
        self.files_tree = self.query_one(VideoTree)
        self.search_pane = self.query_one("#search-pane")
        self.files_pane = self.query_one("#files-pane")
        self.status_bar = self.query_one("#status-bar", Static)
        self.dl_bar = self.query_one("#dl-bar", Static)
        self.enter_row = -1

        self.results_table.cursor_type = "row"
        self.results_table.zebra_stripes = True
        self.results_table.add_column("", width=2)
        self.results_table.add_column("Lang", width=5)
        self.results_table.add_column("Title", width=52)
        self.results_table.add_column("Size", width=9)
        self.results_table.add_column("S", width=5)
        self.results_table.add_column("L", width=5)
        self.results_table.add_column("DLs", width=7)

        self.files_pane.display = False
        self.search_input.focus()
        self.set_interval(1.0, self.poll_downloads)
        self.spinner_timer = self.set_interval(0.1, self.tick_spinner, pause=True)
        self.set_interval(0.5, self.update_dl_bar)
        self.busy = ""
        self.refresh_status()

        if getattr(self, "watch_history_error", ""):
            self.note(self.watch_history_error, title="Watch history unavailable", severity="error", timeout=10)
        for tool in ("aria2c", "mpv"):
            if shutil.which(tool) is None:
                self.note(f"'{tool}' not found in PATH", severity="warning", timeout=8)
        if not HAS_IMAGES:
            self.note(
                f"Inline images off: {IMAGE_ERROR}\nPython: {sys.executable}\n"
                f"Install with that interpreter: {sys.executable} -m pip install textual-image pillow numpy",
                severity="warning",
                timeout=15,
            )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # While the detail screen is open only quit stays available app-wide;
        # the screen has its own bindings.
        if isinstance(self.screen, (DetailScreen, DownloadsScreen)):
            return action == "quit"
        return True

    # ---------- helpers ----------
    def note(self, message: str, **kwargs) -> None:
        """notify() without letting [brackets] in release titles be parsed as markup."""
        try:
            self.notify(message, markup=False, **kwargs)
        except TypeError:
            self.notify(message.replace("[", "\\["), **kwargs)

    def load_watch_history(self) -> dict[str, dict]:
        if not self.watch_history_path.exists():
            return {}
        try:
            data = json.loads(self.watch_history_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("watch history must be a JSON object")
            return {
                str(path): item
                for path, item in data.items()
                if isinstance(item, dict)
                and isinstance(item.get("position"), (int, float))
                and isinstance(item.get("duration"), (int, float))
            }
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.watch_history_error = f"{type(exc).__name__}: {exc}"
            return {}

    def watch_history_snapshot(self) -> dict[str, dict]:
        with self.watch_lock:
            return {path: item.copy() for path, item in self.watch_history.items()}

    def save_watch_history(self) -> None:
        with self.watch_lock:
            snapshot = {path: item.copy() for path, item in self.watch_history.items()}
        self.watch_history_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.watch_history_path.with_name(
            f"{self.watch_history_path.name}.{threading.get_ident()}.tmp"
        )
        temp_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        os.replace(temp_path, self.watch_history_path)

    def update_watch_entry(self, path: Path, position: float, duration: float) -> None:
        key = str(path.resolve())
        with self.watch_lock:
            previous = self.watch_history.get(key, {})
            self.watch_history[key] = {
                "path": key,
                "title": path.name,
                "position": max(0.0, position),
                "duration": max(0.0, duration),
                "last_watched": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        self.save_watch_history()

    def track_playback(self, path: Path, proc: subprocess.Popen, ipc_path: Path) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    return
                try:
                    connection.connect(str(ipc_path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.1)
            else:
                raise TimeoutError("mpv playback IPC did not become available")

            connection.settimeout(2)
            request_id = 0
            buffer = b""
            persistence_error_reported = False
            while proc.poll() is None:
                request_id += 1
                duration_id, position_id = request_id, request_id + 1
                request_id += 1
                for command_id, prop in ((duration_id, "duration"), (position_id, "time-pos")):
                    request = {"command": ["get_property", prop], "request_id": command_id}
                    connection.sendall((json.dumps(request) + "\n").encode())

                values: dict[int, float] = {}
                deadline = time.monotonic() + 2
                while len(values) < 2 and time.monotonic() < deadline:
                    try:
                        chunk = connection.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            response = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        command_id = response.get("request_id")
                        data = response.get("data")
                        if command_id in (duration_id, position_id) and isinstance(data, (int, float)):
                            values[command_id] = float(data)

                if position_id in values or duration_id in values:
                    old = self.watch_history_snapshot().get(str(path.resolve()), {})
                    position = values.get(position_id, float(old.get("position", 0)))
                    duration = values.get(duration_id, float(old.get("duration", 0)))
                    try:
                        self.update_watch_entry(path, position, duration)
                    except OSError as exc:
                        if not persistence_error_reported and self.is_running:
                            self.call_from_thread(
                                self.note,
                                f"Could not save watch history: {exc}",
                                title="Watch history save failed",
                                severity="error",
                            )
                        persistence_error_reported = True
                time.sleep(1)
        except (OSError, TimeoutError) as exc:
            if proc.poll() is None and self.is_running:
                self.call_from_thread(
                    self.note,
                    f"Playback progress tracking failed: {exc}",
                    title="Watch tracking unavailable",
                    severity="warning",
                )
        finally:
            connection.close()
            try:
                ipc_path.unlink()
            except FileNotFoundError:
                pass

    def refresh_status(self, message: str = "") -> None:
        cat = CATEGORIES[self.category_idx][0]
        flt = FILTERS[self.filter_idx][0]
        lang = LANG_MODES[self.lang_idx][0]
        active = sum(1 for d in self.downloads if d["proc"].poll() is None)
        text = f"{cat} | {flt} | {lang} | Dir: {self.download_dir} | Active DLs: {active}"
        if self.busy:
            message = f"{spin_frame()} {self.busy}"
        if message:
            text = f"{message}  ||  {text}"
        self.status_bar.update(Text(text))

    # ---------- search ----------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-bar":
            return
        query = event.value.strip()
        if not query:
            return
        self.last_query = query
        self.run_search(query)

    def tick_spinner(self) -> None:
        self.refresh_status()

    def set_busy(self, message: str) -> None:
        self.busy = message
        self.results_table.loading = bool(message)
        if message:
            self.spinner_timer.resume()
        else:
            self.spinner_timer.pause()
        self.refresh_status()

    def run_search(self, query: str) -> None:
        self.set_busy(f"Searching '{query}'...")
        self.do_search(query, CATEGORIES[self.category_idx][1], FILTERS[self.filter_idx][1])

    @work(thread=True, exclusive=True, group="search")
    def do_search(self, query: str, category: str, nyaa_filter: int) -> None:
        try:
            results = fetch_feed(query, category, nyaa_filter)
        except Exception as exc:
            self.call_from_thread(self.search_failed, f"{type(exc).__name__}: {exc}")
            return
        self.call_from_thread(self.search_done, query, results)

    def search_failed(self, error: str) -> None:
        self.set_busy("")
        self.refresh_status(f"Search failed - {error}")
        self.note(error, title="Search failed", severity="error", timeout=8)

    def search_done(self, query: str, results: list[dict]) -> None:
        self.set_busy("")
        self.results = results
        self.render_results(query)

    def render_results(self, query: str) -> None:
        """Apply the language filter and (re)fill the table from self.results."""
        allowed = LANG_MODES[self.lang_idx][1]
        self.shown = [r for r in self.results if allowed is None or r["lang"] in allowed]

        self.results_table.clear()
        self.enter_row = -1
        for r in self.shown:
            style, tag = lang_style(r["lang"])
            self.results_table.add_row(
                Text(""),
                Text(tag, style=style),
                Text(r["title"], style=style),
                Text(r["size"], style=style),
                Text(str(r["seeders"]), style=style),
                Text(str(r["leechers"]), style=style),
                Text(str(r["downloads"]), style=style),
            )
        hidden = len(self.results) - len(self.shown)
        extra = f" ({hidden} hidden by language filter)" if hidden else ""
        self.refresh_status(f"{len(self.shown)} results for '{query}'{extra}")
        if self.shown:
            if self.view != "search":
                self.action_toggle_view()
            if not isinstance(self.screen, DetailScreen):
                self.results_table.focus()
            self.results_table.move_cursor(row=0)
            self.refresh_enter_marker()
        else:
            self.note("No results", severity="warning")

    # ---------- "press Enter" cue ----------
    def refresh_enter_marker(self) -> None:
        table = self.results_table
        show = table.has_focus and self.view == "search" and not isinstance(self.screen, (DetailScreen, DownloadsScreen))
        target = table.cursor_row if show and 0 <= table.cursor_row < len(self.shown) else -1
        if target == self.enter_row:
            return
        if 0 <= self.enter_row < table.row_count:
            table.update_cell_at(Coordinate(self.enter_row, 0), Text(""))
        if target >= 0:
            table.update_cell_at(Coordinate(target, 0), Text(ENTER_MARK, style="bold yellow"))
        self.enter_row = target

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.results_table:
            self.refresh_enter_marker()

    def on_descendant_focus(self, event) -> None:
        self.call_after_refresh(self.refresh_enter_marker)

    def on_descendant_blur(self, event) -> None:
        self.call_after_refresh(self.refresh_enter_marker)

    # ---------- details ----------
    def selected_result(self) -> dict | None:
        row = self.results_table.cursor_row
        if 0 <= row < len(self.shown):
            return self.shown[row]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        result = self.selected_result()
        if result:
            self.push_screen(DetailScreen(result))

    # ---------- downloads ----------
    def action_download(self) -> None:
        if self.view != "search":
            return
        result = self.selected_result()
        if result:
            self.start_download(result)

    def start_download(self, result: dict) -> None:
        title, magnet = result["title"], result["magnet"]
        if not magnet:
            self.note("No magnet/torrent link for this entry", severity="error")
            return
        if shutil.which("aria2c") is None:
            self.note("aria2c is not installed", severity="error")
            return
        cmd = [
            "aria2c",
            "--seed-time=0",
            "--summary-interval=1",
            "--console-log-level=warn",
            "--download-result=hide",
            "--bt-save-metadata=false",
            f"--dir={self.download_dir}",
            magnet,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            self.note(str(exc), title="Failed to start aria2c", severity="error")
            return
        state = new_download_state(title, proc)
        threading.Thread(target=read_progress, args=(state,), daemon=True).start()
        self.downloads.append(state)
        self.note(title[:60], title="Download started")
        self.refresh_status()

    def poll_downloads(self) -> None:
        still_running = []
        for d in self.downloads:
            code = d["proc"].poll()
            title = d["title"]
            if code is None:
                still_running.append(d)
            elif d["cancelled"]:
                self.note(title[:60], title="Download cancelled", severity="warning")
            elif code == 0:
                self.note(title[:60], title="Download finished")
                self.action_refresh_tree()
            else:
                self.note(f"{title[:50]} (exit {code})", title="Download failed", severity="error")
        if len(still_running) != len(self.downloads):
            self.downloads = still_running
            self.refresh_status()

    def update_dl_bar(self) -> None:
        active = self.active_downloads()
        self.dl_bar.display = bool(active)
        if not active:
            return
        total = sum(d["total"] for d in active if d["pct"] is not None)
        done = sum(d["done"] for d in active if d["pct"] is not None)
        speed = sum(d["speed"] for d in active)
        line = Text(f"Downloads ({len(active)}): ", style="bold")
        if total > 0:
            line.append_text(progress_bar(done / total * 100, 30))
            line.append(f"  {fmt_size(done)}/{fmt_size(total)}  {fmt_size(speed)}/s")
        else:
            line.append(f"{spin_frame()} connecting / fetching metadata...", style="cyan")
        line.append("   (x: manage)", style="dim")
        self.dl_bar.update(line)

    def active_downloads(self) -> list[dict]:
        return [d for d in self.downloads if d["proc"].poll() is None]

    def cancel_download(self, d: dict) -> None:
        proc = d["proc"]
        if proc.poll() is not None:
            return
        d["cancelled"] = True
        proc.terminate()  # aria2c exits cleanly and keeps its .aria2 file so it can resume
        self.set_timer(5, lambda: proc.kill() if proc.poll() is None else None)

    def action_downloads(self) -> None:
        self.push_screen(DownloadsScreen())

    # ---------- playback ----------
    def action_play(self) -> None:
        if self.view != "files":
            return
        node = self.files_tree.cursor_node
        if node and node.data and Path(node.data.path).is_file():
            self.play(Path(node.data.path))

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.play(Path(event.path))

    def play(self, path: Path) -> None:
        if shutil.which("mpv") is None:
            self.note("mpv is not installed", severity="error")
            return
        ipc_path = Path(tempfile.gettempdir()) / f"nyaa-tui-{os.getpid()}-{uuid.uuid4().hex}.sock"
        try:
            proc = subprocess.Popen(
                ["mpv", "--force-window=yes", f"--input-ipc-server={ipc_path}", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.note(str(exc), title="Failed to start mpv", severity="error")
            return
        threading.Thread(
            target=self.track_playback,
            args=(path, proc, ipc_path),
            daemon=True,
        ).start()
        self.note(path.name[:60], title="Playing")

    # ---------- view / settings actions ----------
    def action_watch_history(self) -> None:
        self.push_screen(WatchHistoryScreen())

    def action_focus_search(self) -> None:
        self.search_input.focus()

    def action_toggle_view(self) -> None:
        if self.view == "search":
            self.search_pane.display = False
            self.files_pane.display = True
            self.view = "files"
            self.files_tree.focus()
        else:
            self.files_pane.display = False
            self.search_pane.display = True
            self.view = "search"
            self.results_table.focus()

    def action_refresh_tree(self) -> None:
        self.files_tree.reload()

    def action_cycle_category(self) -> None:
        self.category_idx = (self.category_idx + 1) % len(CATEGORIES)
        self.refresh_status()
        if self.last_query:
            self.run_search(self.last_query)

    def action_cycle_filter(self) -> None:
        self.filter_idx = (self.filter_idx + 1) % len(FILTERS)
        self.refresh_status()
        if self.last_query:
            self.run_search(self.last_query)

    def action_cycle_lang(self) -> None:
        self.lang_idx = (self.lang_idx + 1) % len(LANG_MODES)
        self.refresh_status()
        if self.results:
            self.render_results(self.last_query)

    def action_set_dir(self) -> None:
        def apply(new_path: str | None) -> None:
            if not new_path:
                return
            path = Path(new_path)
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.note(str(exc), title="Invalid directory", severity="error")
                return
            self.download_dir = path
            self.files_tree.path = path
            self.files_tree.reload()
            self.refresh_status()

        self.push_screen(DirectoryPromptScreen(self.download_dir), apply)

    # ---------- cleanup ----------
    def on_unmount(self) -> None:
        for d in self.downloads:
            if d["proc"].poll() is None:
                d["proc"].terminate()


def check_images() -> None:
    """python nyaa_tui.py --check-images"""
    print("python:      ", sys.executable)
    print("HAS_IMAGES:  ", HAS_IMAGES)
    print("import error:", IMAGE_ERROR or "none")
    print("TERM:        ", os.environ.get("TERM"), "| TERM_PROGRAM:", os.environ.get("TERM_PROGRAM"))
    print("in tmux/screen:", bool(os.environ.get("TMUX") or os.environ.get("STY")))
    if HAS_IMAGES:
        from textual_image.renderable import Image as RenderImage

        print("renderer:    ", f"{RenderImage.__module__}")
        try:
            data = http_get(BASE_URL + "/static/img/avatar/default.png", max_bytes=MAX_IMAGE_BYTES)
            PILImage.open(io.BytesIO(data)).load()
            print("image fetch+decode: ok")
        except Exception as exc:
            print("image fetch+decode failed:", type(exc).__name__, exc)


def main() -> None:
    if "--check-images" in sys.argv:
        check_images()
    else:
        NyaaTUI().run()


if __name__ == "__main__":
    main()
