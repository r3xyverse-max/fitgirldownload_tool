#!/usr/bin/env python3
"""
main.py

Two-stage Playwright batch downloader (asyncio only) for AUTHORIZED sites.

State machine per URL
---------------------
 1  open main page
 2  locate exact main DOWNLOAD control (text-anchored, so "Index" etc. is ignored)
 3  click #1  -> watch for a DOWNLOAD EVENT (direct download is possible here)
              -> otherwise close ad popups (never the main page)
 4  re-find main DOWNLOAD control
 5  listeners for context "download" + "page" (+ page-level "download" on every
    page) are installed ONCE, right after the context is created, i.e. before
    click #1 and click #2
 6  click #2
 7  race:  download event  -> direct download
        :  new page event   -> inspect it CONCURRENTLY (never closed before
                               inspection finishes)
 8  landing detection: configured selector(+text) -> role=button -> role=link
    -> generic button/a text, scanned across all frames, polled while rendering
 9  control found -> keep page alive -> click it (listener already armed)
10  Download object -> filename -> exact-name duplicate check (no "(2)")
11  context stays alive until save_as() returns
12  verify file, then close pages + context
13  isolated BrowserContext per URL, asyncio.Semaphore for workers
14  cancellation-safe shutdown: cancel tasks -> gather -> browser.close -> stop
15  report.txt / report.csv / report.json

Install
-------
python -m pip install -U playwright
python -m playwright install chromium

Example
-------
python main.py --headed --workers 1 --retries 2 ^
    --urls urls.txt --out downloads ^
    --main-selector "a, button" ^
    --landing-selector "a, button" ^
    --landing-text "^\\s*Download File\\s*$"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


# ============================================================
# CONFIGURATION
# ============================================================

PAGE_TIMEOUT_MS = 45_000
DOM_TIMEOUT_MS = 20_000
MAIN_CONTROL_WAIT_S = 15.0

DEFAULT_WORKERS = 1
DEFAULT_RETRIES = 1

DEFAULT_MAIN_TEXT = r"^\s*DOWNLOAD\s*$"
# "Download", "Download File", "Download File (12 MB)"
DEFAULT_LANDING_TEXT = r"^\s*Download(?:\s+File)?\b(?:\s*[\(\[].*[\)\]])?\s*$"

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class Config:
    main_selector: str
    main_text: str
    landing_selector: Optional[str]
    landing_text: Optional[str]
    click1_wait_s: float
    landing_wait_s: float
    stage2_timeout_s: float
    download_timeout_s: float


@dataclass
class Result:
    index: int
    url: str
    status: str  # DOWNLOADED | ALREADY_EXISTS | FAILED
    filename: Optional[str]
    path: Optional[str]
    attempts: int
    bytes: int
    seconds: float
    error: Optional[str]


# ============================================================
# VERSION / URL / FILENAME HELPERS
# ============================================================

def check_playwright_version() -> None:
    try:
        installed = version("playwright")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed.\n"
            "  python -m pip install -U playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    try:
        major, minor = (int(x) for x in installed.split(".")[:2])
    except ValueError:
        print(f"Warning: cannot parse Playwright version {installed}", file=sys.stderr)
        return

    if (major, minor) < (1, 40):
        raise RuntimeError(
            f"Playwright {installed} is too old. Run: python -m pip install -U playwright"
        )
    print(f"Playwright version: {installed}", flush=True)


def load_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"URL file not found: {path.resolve()}")

    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")):
            print(f"Warning: ignoring invalid URL: {line}", file=sys.stderr)
            continue
        urls.append(line)
    return urls


def filename_hint_from_url(url: str) -> Optional[str]:
    try:
        fragment = unquote(urlsplit(url).fragment).strip()
    except Exception:
        return None
    return fragment or None


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return "download.bin"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


# ============================================================
# CONTEXT WATCHER  (listeners installed BEFORE any click)
# ============================================================

class ContextWatcher:
    """
    Collects ("download", Download) and ("page", Page) events for one
    BrowserContext into a single asyncio queue.

    * context.on("page")      -> new pages / popups
    * context.on("download")  -> context-level download events (if emitted)
    * page.on("download")     -> hooked onto EVERY page the moment it appears

    Downloads are de-duplicated, so receiving the same download through
    more than one route is harmless.
    """

    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self.queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self._seen_downloads: list[Download] = []
        self._hooked_pages: list[Page] = []

    # -- handlers ------------------------------------------------------

    def _on_download(self, download: Download) -> None:
        if any(download is d for d in self._seen_downloads):
            return
        self._seen_downloads.append(download)
        self.queue.put_nowait(("download", download))

    def _hook_page(self, page: Page) -> None:
        if any(page is p for p in self._hooked_pages):
            return
        self._hooked_pages.append(page)
        page.on("download", self._on_download)

    def _on_page(self, page: Page) -> None:
        self._hook_page(page)
        self.queue.put_nowait(("page", page))

    # -- lifecycle -----------------------------------------------------

    def install(self) -> None:
        self.context.on("page", self._on_page)
        self.context.on("download", self._on_download)
        for page in list(self.context.pages):
            self._hook_page(page)

    def remove(self) -> None:
        for event, handler in (("page", self._on_page), ("download", self._on_download)):
            try:
                self.context.remove_listener(event, handler)
            except Exception:
                pass
        for page in self._hooked_pages:
            try:
                page.remove_listener("download", self._on_download)
            except Exception:
                pass

    # -- helpers -------------------------------------------------------

    async def wait_download(self, timeout_s: float) -> Optional[Download]:
        """Wait up to timeout_s for a download event; page events are skipped."""
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                kind, value = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if kind == "download":
                assert isinstance(value, Download)
                return value


# ============================================================
# DEBUG: VISIBLE CONTROLS
# ============================================================

async def show_controls(page: Page, label: str = "") -> None:
    try:
        url = page.url
    except Exception:
        url = "<unknown>"
    print(f"    Visible controls {label}(page: {url}):", flush=True)

    shown = 0
    try:
        frames = list(page.frames)
    except Exception:
        return

    for frame in frames:
        try:
            locator = frame.locator(
                "a, button, input[type='submit'], input[type='button'], [role='button']"
            )
            count = await locator.count()
        except Exception:
            continue

        for i in range(count):
            item = locator.nth(i)
            try:
                if not await item.is_visible():
                    continue
                tag = await item.evaluate("(el) => el.tagName")
                try:
                    text = (await item.inner_text()).strip()[:80]
                except Exception:
                    text = ""
                aria = await item.get_attribute("aria-label")
                href = await item.get_attribute("href")
                cls = await item.get_attribute("class")
                shown += 1
                print(
                    f"      [{shown}] {tag} text={text!r} aria={aria!r} "
                    f"class={cls!r} href={href!r}",
                    flush=True,
                )
                if shown >= 40:
                    return
            except Exception:
                continue

    if shown == 0:
        print("      (none)", flush=True)


# ============================================================
# NAVIGATION / PAGE HELPERS
# ============================================================

async def navigate(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="commit", timeout=PAGE_TIMEOUT_MS)
    except Exception as exc:
        current = page.url
        if not current or current == "about:blank":
            raise
        print(f"    navigation warning: {type(exc).__name__}: {exc}", flush=True)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=DOM_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        print("    DOMContentLoaded timeout; continuing", flush=True)


async def close_secondary_pages(
    context: BrowserContext,
    main_page: Page,
    index: int,
    keep: Optional[list[Page]] = None,
) -> None:
    keep = keep or []
    for page in list(context.pages):
        if page is main_page or any(page is k for k in keep):
            continue
        try:
            url = page.url
        except Exception:
            url = "<unknown>"
        print(f"[{index}] closing secondary page: {url}", flush=True)
        try:
            await page.close()
        except Exception:
            pass


async def first_visible(locator: Locator, limit: int = 20) -> Optional[Locator]:
    try:
        count = await locator.count()
    except Exception:
        return None
    for i in range(min(count, limit)):
        candidate = locator.nth(i)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def find_main_control(page: Page, cfg: Config) -> Optional[Locator]:
    """Exact main-page DOWNLOAD control (text-anchored; ignores 'Index' etc.)."""
    pattern = re.compile(cfg.main_text, re.IGNORECASE)
    deadline = time.monotonic() + MAIN_CONTROL_WAIT_S
    while True:
        if page.is_closed():
            return None
        found = await first_visible(
            page.locator(cfg.main_selector).filter(has_text=pattern)
        )
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


# ============================================================
# LANDING PAGE DETECTION
# ============================================================

async def scan_landing_once(
    page: Page,
    landing_selector: Optional[str],
    landing_text: Optional[str],
) -> Optional[tuple[Locator, str]]:
    """One pass over all frames and all detection tiers, in priority order."""
    text_re = re.compile(landing_text or DEFAULT_LANDING_TEXT, re.IGNORECASE)

    try:
        frames = list(page.frames)
    except Exception:
        return None

    tiers: list[tuple[str, object]] = []

    if landing_selector:
        def by_selector(frame):
            loc = frame.locator(landing_selector)
            if landing_text:
                loc = loc.filter(has_text=text_re)
            return loc
        tiers.append((f"configured selector {landing_selector!r}", by_selector))

    tiers.append(("role=button", lambda f: f.get_by_role("button", name=text_re)))
    tiers.append(("role=link", lambda f: f.get_by_role("link", name=text_re)))
    tiers.append((
        "generic button/a text",
        lambda f: f.locator(
            "button, a, input[type='submit'], input[type='button'], [role='button']"
        ).filter(has_text=text_re),
    ))

    for name, factory in tiers:
        for frame in frames:
            try:
                found = await first_visible(factory(frame))  # type: ignore[operator]
            except Exception:
                found = None
            if found is not None:
                return found, name
    return None


async def inspect_page(
    page: Page,
    index: int,
    cfg: Config,
) -> tuple[Optional[Locator], str]:
    """
    Wait for a new page to render and look for the landing download control.
    Never closes the page. Returns (control, reason_or_method).
    """
    print(f"[{index}] inspecting intermediate page...", flush=True)
    deadline = time.monotonic() + cfg.landing_wait_s

    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=int(min(10.0, cfg.landing_wait_s) * 1000),
        )
    except Exception:
        pass  # page may still be rendering; the polling loop handles it

    try:
        while True:
            if page.is_closed():
                return None, "page closed"

            found = await scan_landing_once(page, cfg.landing_selector, cfg.landing_text)
            if found is not None:
                return found

            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.4)
    except Exception as exc:
        return None, f"inspection error {type(exc).__name__}: {exc}"

    tried = ["role=button", "role=link", "generic button/a text"]
    if cfg.landing_selector:
        tried.insert(0, "configured selector")
    return None, (
        f"timeout while rendering: no download control matched after "
        f"{cfg.landing_wait_s:.0f}s (tried: {', '.join(tried)})"
    )


async def click_control(page: Page, control: Locator) -> None:
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        await control.click(no_wait_after=True, timeout=10_000)
    except Exception as exc:
        print(f"    normal click failed ({type(exc).__name__}); retrying with force", flush=True)
        await control.click(no_wait_after=True, timeout=10_000, force=True)


# ============================================================
# STAGE A: CLICK #1  (ads, but a download may start here too)
# ============================================================

async def perform_first_click(
    watcher: ContextWatcher,
    context: BrowserContext,
    main_page: Page,
    cfg: Config,
    index: int,
) -> Optional[Download]:
    control = await find_main_control(main_page, cfg)
    if control is None:
        await show_controls(main_page, "(main page) ")
        raise RuntimeError("Main DOWNLOAD control was not found.")

    print(f"[{index}] click #1...", flush=True)
    await control.click(no_wait_after=True, timeout=10_000)
    print(
        f"[{index}] click #1 completed; watching {cfg.click1_wait_s:.1f}s "
        f"for a download event / ad popups",
        flush=True,
    )

    # Click #1 may itself start the download. Do NOT close anything if so.
    download = await watcher.wait_download(cfg.click1_wait_s)
    if download is not None:
        print(
            f"[{index}] DOWNLOAD EVENT RECEIVED (during click #1): "
            f"{download.suggested_filename!r}",
            flush=True,
        )
        return download

    await close_secondary_pages(context, main_page, index)

    # A download event may have landed while pages were closing.
    download = await watcher.wait_download(0.2)
    if download is not None:
        print(
            f"[{index}] DOWNLOAD EVENT RECEIVED (late, click #1): "
            f"{download.suggested_filename!r}",
            flush=True,
        )
        return download

    control_after = await find_main_control(main_page, cfg)
    if control_after is None:
        await show_controls(main_page, "(main page after click #1) ")
        raise RuntimeError("Main DOWNLOAD control disappeared after click #1.")

    print(f"[{index}] main DOWNLOAD ready", flush=True)
    return None


# ============================================================
# STAGE B: CLICK #2  (direct download OR intermediate page)
# ============================================================

async def perform_second_stage(
    watcher: ContextWatcher,
    context: BrowserContext,
    main_page: Page,
    cfg: Config,
    index: int,
) -> Download:
    control = await find_main_control(main_page, cfg)
    if control is None:
        await show_controls(main_page, "(main page) ")
        raise RuntimeError("Main DOWNLOAD control not found for click #2.")

    print(f"[{index}] click #2...", flush=True)
    await control.click(no_wait_after=True, timeout=10_000)
    print(f"[{index}] click #2 completed", flush=True)

    deadline = time.monotonic() + cfg.stage2_timeout_s
    inspections: dict[asyncio.Future, Page] = {}
    getter: Optional[asyncio.Future] = None
    landing_page: Optional[Page] = None

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if landing_page is not None:
                    raise PlaywrightTimeoutError(
                        "Clicked the intermediate download control but no download "
                        f"event arrived within {cfg.download_timeout_s:.0f}s."
                    )
                raise PlaywrightTimeoutError(
                    "Neither a direct download nor a usable intermediate download "
                    f"page appeared within {cfg.stage2_timeout_s:.0f}s."
                )

            if getter is None:
                getter = asyncio.ensure_future(watcher.queue.get())

            done, _ = await asyncio.wait(
                {getter, *inspections.keys()},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                # ---------------- queue event ----------------
                if task is getter:
                    getter = None
                    kind, value = task.result()

                    if kind == "download":
                        assert isinstance(value, Download)
                        label = "DOWNLOAD EVENT RECEIVED"
                        if landing_page is None:
                            label += " (direct)"
                        print(f"[{index}] {label}: {value.suggested_filename!r}", flush=True)
                        return value

                    page = value
                    assert isinstance(page, Page)
                    if page is main_page or page.is_closed():
                        continue
                    if landing_page is not None:
                        # Already clicked the landing control; extra pages
                        # are left alone and cleaned up after the save.
                        continue

                    try:
                        page_url = page.url
                    except Exception:
                        page_url = "<unknown>"
                    print(f"[{index}] secondary page detected: {page_url}", flush=True)

                    inspections[asyncio.ensure_future(inspect_page(page, index, cfg))] = page
                    continue

                # ---------------- inspection finished ----------------
                page = inspections.pop(task)
                try:
                    found, info = task.result()
                except Exception as exc:
                    found, info = None, f"inspection crashed {type(exc).__name__}: {exc}"

                if found is None:
                    print(f"[{index}] secondary page REJECTED: {info}", flush=True)
                    if not page.is_closed():
                        await show_controls(page, "(rejected page) ")
                        print(f"[{index}] closing rejected page", flush=True)
                        try:
                            await page.close()
                        except Exception:
                            pass
                    continue

                if landing_page is not None:
                    continue  # a different page already won

                print(f"[{index}] intermediate download control found (via {info})", flush=True)
                print(f"[{index}] clicking intermediate download control...", flush=True)

                # The context watcher is already armed, so the download
                # event cannot be missed.
                await click_control(page, found)

                landing_page = page
                deadline = time.monotonic() + cfg.download_timeout_s

    finally:
        pending: list[asyncio.Future] = list(inspections.keys())
        if getter is not None:
            pending.append(getter)
        for fut in pending:
            if not fut.done():
                fut.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# ============================================================
# SAVE
# ============================================================

async def save_download(download: Download, destination: Path) -> int:
    """save_as() only returns when the transfer has completed."""
    temp = destination.with_name(destination.name + ".part")
    try:
        temp.unlink(missing_ok=True)
    except Exception:
        pass

    print(f"    saving: {destination}", flush=True)
    await download.save_as(str(temp))

    if not temp.exists():
        raise RuntimeError("save_as() returned but the temporary file does not exist.")

    size = temp.stat().st_size
    if size <= 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file is empty.")

    if destination.exists():
        temp.unlink(missing_ok=True)
        raise FileExistsError(f"Destination already exists: {destination}")

    temp.replace(destination)

    if not destination.exists() or destination.stat().st_size <= 0:
        raise RuntimeError("Final file verification failed.")
    return destination.stat().st_size


async def safe_close_context(context: BrowserContext) -> None:
    try:
        await asyncio.wait_for(context.close(), timeout=20)
    except Exception:
        pass


# ============================================================
# ONE ATTEMPT
# ============================================================

async def attempt_download(
    browser: Browser,
    index: int,
    url: str,
    outdir: Path,
    cfg: Config,
    filename_lock: asyncio.Lock,
) -> Result:
    started = time.monotonic()
    context: Optional[BrowserContext] = None
    watcher: Optional[ContextWatcher] = None

    def elapsed() -> float:
        return time.monotonic() - started

    try:
        # STATE 1 -------------------------------------------------------
        print(f"[{index}] opening", flush=True)
        context = await browser.new_context(accept_downloads=True)

        # STATE 5: listeners exist before ANY click.
        watcher = ContextWatcher(context)
        watcher.install()

        main_page = await context.new_page()
        main_page.set_default_timeout(PAGE_TIMEOUT_MS)

        await navigate(main_page, url)
        print(f"[{index}] loaded: {main_page.url}", flush=True)

        fragment_hint = filename_hint_from_url(url)

        # Early exact-name existence check.
        if fragment_hint:
            hinted = outdir / safe_filename(fragment_hint)
            if hinted.exists():
                print(f"[{index}] already exists: {hinted}", flush=True)
                return Result(
                    index, url, "ALREADY_EXISTS", hinted.name, str(hinted), 1,
                    hinted.stat().st_size, elapsed(), "Exact filename already exists.",
                )

        # STATES 2-4: click #1 (may itself yield a download)
        download = await perform_first_click(watcher, context, main_page, cfg, index)

        # STATES 6-9: click #2 / direct / landing page
        if download is None:
            download = await perform_second_stage(watcher, context, main_page, cfg, index)

        # STATE 10: filename
        suggested = (download.suggested_filename or "").strip()
        if suggested:
            filename = safe_filename(suggested)
        elif fragment_hint:
            filename = safe_filename(fragment_hint)
        else:
            filename = "download.bin"

        destination = outdir / filename
        print(f"[{index}] target: {filename}", flush=True)

        async with filename_lock:
            if destination.exists():
                print(f"[{index}] already exists: {destination}", flush=True)
                try:
                    await download.cancel()
                except Exception:
                    pass
                return Result(
                    index, url, "ALREADY_EXISTS", filename, str(destination), 1,
                    destination.stat().st_size, elapsed(), "Exact filename already exists.",
                )

            # STATE 11: context MUST stay alive until this returns.
            size = await save_download(download, destination)

        print(f"[{index}] saved successfully: {filename} ({size:,} bytes)", flush=True)
        return Result(index, url, "DOWNLOADED", filename, str(destination), 1, size, elapsed(), None)

    finally:
        # STATE 12: only now (save finished, or attempt failed/cancelled).
        if watcher is not None:
            watcher.remove()
        if context is not None:
            await safe_close_context(context)


# ============================================================
# PROCESS ONE URL (retries, per-filename locking)
# ============================================================

async def process_one(
    browser: Browser,
    index: int,
    url: str,
    outdir: Path,
    cfg: Config,
    retries: int,
    semaphore: asyncio.Semaphore,
    filename_locks: dict[str, asyncio.Lock],
    filename_locks_guard: asyncio.Lock,
) -> Result:
    async with semaphore:
        started = time.monotonic()
        last_error: Optional[str] = None

        hint = filename_hint_from_url(url)
        lock_key = safe_filename(hint) if hint else url

        async with filename_locks_guard:
            filename_lock = filename_locks.setdefault(lock_key, asyncio.Lock())

        for attempt in range(1, retries + 1):
            try:
                result = await attempt_download(browser, index, url, outdir, cfg, filename_lock)
                result.attempts = attempt
                return result
            except PlaywrightTimeoutError as exc:
                last_error = f"Playwright timeout: {exc}"
            except Exception as exc:  # CancelledError is NOT caught here
                last_error = f"{type(exc).__name__}: {exc}"

            print(f"[{index}] attempt {attempt}/{retries} failed: {last_error}", flush=True)

            if attempt < retries:
                delay = min(2 ** (attempt - 1), 15)
                print(f"[{index}] retrying in {delay}s...", flush=True)
                await asyncio.sleep(delay)

        return Result(
            index, url, "FAILED", None, None, retries, 0,
            time.monotonic() - started, last_error,
        )


# ============================================================
# REPORTS
# ============================================================

def write_txt_report(outdir: Path, results: list[Result], started: datetime) -> None:
    results = sorted(results, key=lambda r: r.index)
    downloaded = [r for r in results if r.status == "DOWNLOADED"]
    existing = [r for r in results if r.status == "ALREADY_EXISTS"]
    failed = [r for r in results if r.status == "FAILED"]
    total_bytes = sum(r.bytes for r in results)

    lines = [
        "BATCH DOWNLOAD REPORT",
        "=" * 72,
        f"Started : {started:%Y-%m-%d %H:%M:%S}",
        f"Finished: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        f"Total URLs     : {len(results)}",
        f"Downloaded     : {len(downloaded)}",
        f"Already exists : {len(existing)}",
        f"Failed         : {len(failed)}",
        f"Total size     : {total_bytes / (1024 ** 3):.2f} GiB",
        "",
        "DOWNLOADED",
        "-" * 72,
    ]
    lines += [
        f"[OK] #{r.index:03d} {r.filename} ({r.bytes / (1024 ** 2):.2f} MiB)"
        for r in downloaded
    ] or ["(none)"]

    lines += ["", "ALREADY EXISTS", "-" * 72]
    lines += [
        f"[EXISTS] #{r.index:03d} {r.filename} ({r.bytes / (1024 ** 2):.2f} MiB)"
        for r in existing
    ] or ["(none)"]

    lines += ["", "FAILED", "-" * 72]
    if failed:
        for r in failed:
            lines += [
                f"[FAIL] #{r.index:03d}",
                f"       URL: {r.url}",
                f"       Error: {r.error}",
                f"       Attempts: {r.attempts}",
            ]
    else:
        lines.append("(none)")

    (outdir / "report.txt").write_text("\n".join(lines), encoding="utf-8")


def write_json_report(outdir: Path, results: list[Result], started: datetime) -> None:
    results = sorted(results, key=lambda r: r.index)
    payload = {
        "started": started.isoformat(),
        "finished": datetime.now().isoformat(),
        "summary": {
            "total": len(results),
            "downloaded": sum(r.status == "DOWNLOADED" for r in results),
            "already_exists": sum(r.status == "ALREADY_EXISTS" for r in results),
            "failed": sum(r.status == "FAILED" for r in results),
        },
        "results": [asdict(r) for r in results],
    }
    (outdir / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_csv_report(outdir: Path, results: list[Result]) -> None:
    results = sorted(results, key=lambda r: r.index)
    with (outdir / "report.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "index", "url", "status", "filename", "path",
                "attempts", "bytes", "seconds", "error",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def write_reports(outdir: Path, results: list[Result], started: datetime) -> None:
    write_txt_report(outdir, results, started)
    write_json_report(outdir, results, started)
    write_csv_report(outdir, results)


# ============================================================
# ASYNC MAIN  (STATES 13 / 14)
# ============================================================

async def async_main(args: argparse.Namespace) -> int:
    check_playwright_version()

    urls = load_urls(args.urls)
    if not urls:
        raise RuntimeError(f"No valid URLs found in {args.urls.resolve()}")

    args.out.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        main_selector=args.main_selector,
        main_text=args.main_text,
        landing_selector=args.landing_selector,
        landing_text=args.landing_text,
        click1_wait_s=args.click1_wait,
        landing_wait_s=args.landing_wait,
        stage2_timeout_s=args.stage2_timeout,
        download_timeout_s=args.download_timeout,
    )

    started = datetime.now()

    print("\n" + "=" * 72)
    print("PLAYWRIGHT TWO-STAGE BATCH DOWNLOADER")
    print("=" * 72)
    print(f"URLs              : {len(urls)}")
    print(f"Workers           : {args.workers}")
    print(f"Retries           : {args.retries}")
    print(f"Main selector     : {cfg.main_selector}")
    print(f"Main text         : {cfg.main_text}")
    print(f"Landing selector  : {cfg.landing_selector or '(fallback detection)'}")
    print(f"Landing text      : {cfg.landing_text or DEFAULT_LANDING_TEXT}")
    print(f"Output            : {args.out.resolve()}")
    print(f"Headed            : {args.headed}")
    print("=" * 72 + "\n")

    semaphore = asyncio.Semaphore(args.workers)
    filename_locks: dict[str, asyncio.Lock] = {}
    filename_locks_guard = asyncio.Lock()

    tasks: list[asyncio.Task[Result]] = []
    results: list[Result] = []
    playwright_instance: Optional[Playwright] = None
    browser: Optional[Browser] = None

    try:
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.launch(headless=not args.headed)

        tasks = [
            asyncio.create_task(
                process_one(
                    browser, index, url, args.out, cfg, args.retries,
                    semaphore, filename_locks, filename_locks_guard,
                )
            )
            for index, url in enumerate(urls, start=1)
        ]

        for completed, fut in enumerate(asyncio.as_completed(tasks), start=1):
            try:
                result = await fut
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"[{completed}/{len(urls)}] worker exception: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue

            results.append(result)
            tag = {"DOWNLOADED": "OK", "ALREADY_EXISTS": "EXISTS"}.get(result.status, "FAILED")
            detail = result.filename if result.status != "FAILED" else f"#{result.index}: {result.error}"
            print(f"[{completed}/{len(urls)}] [{tag}] {detail}", flush=True)

    finally:
        # STATE 14: cancel tasks -> gather -> close browser -> stop playwright
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if browser is not None:
            try:
                if browser.is_connected():
                    await browser.close()
            except Exception as exc:
                print(f"Browser shutdown warning: {type(exc).__name__}: {exc}", file=sys.stderr)

        if playwright_instance is not None:
            try:
                await playwright_instance.stop()
            except Exception as exc:
                print(f"Playwright shutdown warning: {type(exc).__name__}: {exc}", file=sys.stderr)

        if results:
            try:
                write_reports(args.out, results, started)
            except Exception as exc:
                print(f"Report warning: {type(exc).__name__}: {exc}", file=sys.stderr)

    downloaded = sum(r.status == "DOWNLOADED" for r in results)
    existing = sum(r.status == "ALREADY_EXISTS" for r in results)
    failed = sum(r.status == "FAILED" for r in results)
    total_bytes = sum(r.bytes for r in results)

    print("\n" + "=" * 72)
    print("FINAL SUMMARY")
    print("=" * 72)
    print(f"Total          : {len(urls)}")
    print(f"Processed      : {len(results)}")
    print(f"Downloaded     : {downloaded}")
    print(f"Already exists : {existing}")
    print(f"Failed         : {failed}")
    print(f"Total size     : {total_bytes / (1024 ** 3):.2f} GiB")
    print()
    print(f"TXT : {(args.out / 'report.txt').resolve()}")
    print(f"CSV : {(args.out / 'report.csv').resolve()}")
    print(f"JSON: {(args.out / 'report.json').resolve()}")
    print("=" * 72)

    return 0 if failed == 0 and len(results) == len(urls) else 1


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Two-stage Playwright batch downloader for authorized sites."
    )
    parser.add_argument("--urls", type=Path, default=Path("urls.txt"))
    parser.add_argument("-o", "--out", type=Path, default=Path("downloads"))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--headed", action="store_true")

    parser.add_argument("--main-selector", required=True,
                        help="selector for candidate main-page controls")
    parser.add_argument("--main-text", default=DEFAULT_MAIN_TEXT,
                        help="regex the main control's text must match")
    parser.add_argument("--landing-selector", default=None,
                        help="selector for the intermediate-page download control")
    parser.add_argument("--landing-text", default=None,
                        help="regex for the intermediate-page control text")

    parser.add_argument("--click1-wait", type=float, default=3.0,
                        help="seconds to watch for download/ads after click #1")
    parser.add_argument("--landing-wait", type=float, default=20.0,
                        help="seconds a new page gets to render its download control")
    parser.add_argument("--stage2-timeout", type=float, default=45.0,
                        help="seconds after click #2 to see a download or landing control")
    parser.add_argument("--download-timeout", type=float, default=120.0,
                        help="seconds to wait for the download event after the landing click")

    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.retries < 1:
        parser.error("--retries must be >= 1")

    # Validate regexes early.
    for name in ("main_text", "landing_text"):
        value = getattr(args, name)
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                parser.error(f"--{name.replace('_', '-')} is not a valid regex: {exc}")

    try:
        raise SystemExit(asyncio.run(async_main(args)))
    except KeyboardInterrupt:
        print("\nStopped cleanly.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()