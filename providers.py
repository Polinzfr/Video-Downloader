"""Platform-based 'Configuration Provider' layer.

Architecture: each Provider does NOT implement the fetch/download mechanics
itself — that stays in one place, in downloader.py's analyze() and
download_media() (yt-dlp already does this via its own extractors). A
Provider answers three questions:

    1. "Does this URL belong to me?"                -> matches(url)
    2. "Do you want to clean the URL before use?"    -> clean_url(url)
       (default: leaves it as-is; platforms that need tracking-parameter
       cleanup override this, see TikTokProvider)
    3. "Do you have any extra yt-dlp options?"       -> get_extra_ydl_opts()

get_info() and download() are shared, concrete methods on BaseProvider (not
abstract) — every provider inherits them and calls downloader.py's
analyze()/download_media() with its own extra_ydl_opts. This keeps text
sanitization, error handling, and TaskQueueManager compatibility in one
place; providers are just configuration.

Adding a new platform: write a small class deriving from BaseProvider that
overrides matches() and (if needed) clean_url()/get_extra_ydl_opts(), then
add it to _PROVIDERS (before GenericProvider). downloader.py and
task_queue.py are never touched.
"""

from __future__ import annotations

import atexit
import os
import platform
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from downloader import (
    PlaylistInfo,
    ProgressInfo,
    VideoInfo,
    analyze,
    download_media,
)


def _host(url: str) -> str:
    """Returns the URL's host (without 'www.', lowercased).

    Returns an empty string instead of raising for a malformed/incomplete
    URL, so matches() checks never crash on it.
    """
    try:
        netloc = urlparse(url.strip()).netloc.lower()
    except Exception:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


# Chromium-family (Brave/Chrome/Edge) browsers' "User Data" ROOT directory,
# per OS. We deliberately keep the root, not just the Cookies file — since
# decrypting cookies also needs the 'Local State' file (see
# _stage_chromium_cookie_copy).
_CHROMIUM_USER_DATA_DIRS: dict[str, dict[str, str]] = {
    "Windows": {
        "brave": r"AppData\Local\BraveSoftware\Brave-Browser\User Data",
        "chrome": r"AppData\Local\Google\Chrome\User Data",
        "edge": r"AppData\Local\Microsoft\Edge\User Data",
    },
    "Darwin": {
        "brave": "Library/Application Support/BraveSoftware/Brave-Browser",
        "chrome": "Library/Application Support/Google/Chrome",
        "edge": "Library/Application Support/Microsoft Edge",
    },
    "Linux": {
        "brave": ".config/BraveSoftware/Brave-Browser",
        "chrome": ".config/google-chrome",
        "edge": ".config/microsoft-edge",
    },
}

# Firefox doesn't encrypt cookies (see _stage_chromium_cookie_copy's
# docstring) and its profile folder name is randomized (e.g.
# xxxxxxxx.default-release), so no staged copy is done for it — we just
# check it exists and let yt-dlp locate the profile itself.
_FIREFOX_PROFILES_DIR: dict[str, str] = {
    "Windows": r"AppData\Roaming\Mozilla\Firefox\Profiles",
    "Darwin": "Library/Application Support/Firefox/Profiles",
    "Linux": ".mozilla/firefox",
}

# Browser order tried in "automatic" mode. Firefox is deliberately first:
# since July 2024, Chrome 127+ and its derivatives (likely including Brave)
# use 'app-bound encryption' which can block external tools from decrypting
# cookies (see _stage_chromium_cookie_copy's docstring); Firefox doesn't
# have this issue, giving it the best odds in "automatic" mode.
_AUTO_BROWSER_PRIORITY: tuple[str, ...] = ("firefox", "brave", "chrome", "edge")

# Temporary cookie copies created this session — cleaned up on process exit
# (see atexit.register below).
_TEMP_COOKIE_DIRS: list[str] = []


def _cleanup_temp_cookie_dirs() -> None:
    for temp_dir in _TEMP_COOKIE_DIRS:
        shutil.rmtree(temp_dir, ignore_errors=True)


atexit.register(_cleanup_temp_cookie_dirs)


def _copy_with_retry(src: Path, dst: Path, attempts: int = 3, delay: float = 0.15) -> bool:
    """Retries copying a file a few times, with short delays, to ride out
    the brief write-lock a browser holds during a SQLite WAL commit.

    Honest limit: if the lock is PERSISTENT (the browser process keeps the
    file exclusively open), these retries also fail — the only fix then is
    closing the browser or falling back to a cookie-less download (see
    _run_with_cookie_fallback in downloader.py).
    """
    for attempt in range(attempts):
        try:
            shutil.copy2(src, dst)
            return True
        except OSError:
            if attempt < attempts - 1:
                time.sleep(delay)
    return False


def _stage_chromium_cookie_copy(user_data_root: Path, profile: str = "Default") -> Optional[str]:
    """Copies the 'Local State' file plus the cookie database for a
    Chromium-family browser (Brave/Chrome/Edge) into a temp directory,
    mirroring the original User Data layout exactly; returns that temp
    directory's path on success.

    Why we copy 'Local State' too, not just the Cookies file: Chromium
    browsers encrypt cookie VALUES with a key stored in 'Local State'. When
    we give yt-dlp a profile path as cookiesfrombrowser's second element, it
    expects both '<profile>/Network/Cookies' AND the root-level
    'Local State' file at the same relative locations — decryption fails if
    either is missing.

    Honest limit (not guaranteed by this function): this only solves the
    "is the file readable right now" problem (transient WAL-related locks).
    Since July 2024, Chrome 127+ and its derivatives use 'app-bound
    encryption', where cookie VALUES can only be decrypted by the browser's
    own process — even a successfully copied file may still fail to decrypt
    in yt-dlp (or here). The only real fix then is falling back to a
    cookie-less download in downloader.py, or using Firefox (which doesn't
    encrypt this way) — which is also why "automatic" mode puts Firefox first.
    """
    local_state_src = user_data_root / "Local State"
    cookies_src = user_data_root / profile / "Network" / "Cookies"
    if not local_state_src.exists() or not cookies_src.exists():
        return None

    temp_root = Path(tempfile.mkdtemp(prefix="ytdlp_cookie_stage_"))
    dest_cookie_dir = temp_root / profile / "Network"
    dest_cookie_dir.mkdir(parents=True, exist_ok=True)

    ok = _copy_with_retry(local_state_src, temp_root / "Local State") and _copy_with_retry(
        cookies_src, dest_cookie_dir / "Cookies"
    )
    if not ok:
        shutil.rmtree(temp_root, ignore_errors=True)
        return None

    _TEMP_COOKIE_DIRS.append(str(temp_root))
    return str(temp_root)


def _resolve_chromium_browser(browser_name: str) -> Optional[tuple[str, Optional[str]]]:
    """Tries a specific Chromium-family browser (brave/chrome/edge).

    Returns None if the User Data root is missing or the staged copy
    fails — the caller then moves to the next browser (in automatic mode)
    or continues without cookies (if the user explicitly picked this browser).
    """
    relative_root = _CHROMIUM_USER_DATA_DIRS.get(platform.system(), {}).get(browser_name)
    if relative_root is None:
        return None
    user_data_root = Path.home() / relative_root
    try:
        if not user_data_root.exists():
            return None
    except OSError:
        return None
    staged_path = _stage_chromium_cookie_copy(user_data_root)
    return (browser_name, staged_path) if staged_path else None


def _resolve_firefox() -> Optional[tuple[str, Optional[str]]]:
    firefox_relative = _FIREFOX_PROFILES_DIR.get(platform.system())
    if not firefox_relative:
        return None
    try:
        if (Path.home() / firefox_relative).exists():
            return "firefox", None
    except OSError:
        pass
    return None


def _resolve_single_browser(browser_name: str) -> Optional[tuple[str, Optional[str]]]:
    """Tries the ONE browser the user explicitly picked in Settings."""
    if browser_name == "firefox":
        return _resolve_firefox()
    return _resolve_chromium_browser(browser_name)


def _detect_browser_for_cookies() -> Optional[tuple[str, Optional[str]]]:
    """For 'Automatic' mode: tries browsers in _AUTO_BROWSER_PRIORITY order
    (Firefox -> Brave -> Chrome -> Edge), returns the first one that's
    usable/reachable. None if none are.

    yt-dlp's 'cookiesfrombrowser' option expects a tuple with a SINGLE
    browser name — (browser, profile, keyring, container). Putting multiple
    browser names in one tuple doesn't work in yt-dlp. We handle the
    "try in order" logic ourselves here, with real filesystem checks and
    copy attempts.
    """
    for browser_name in _AUTO_BROWSER_PRIORITY:
        resolved = _resolve_single_browser(browser_name)
        if resolved:
            return resolved
    return None


def _cookie_opts_from_resolution(resolved: Optional[tuple[str, Optional[str]]]) -> dict:
    """Converts a (browser_name, profile_path) pair into yt-dlp's expected
    cookiesfrombrowser tuple format."""
    if not resolved:
        return {}
    browser_name, profile_path = resolved
    cookiesfrombrowser = (
        (browser_name, profile_path, None, None) if profile_path else (browser_name,)
    )
    return {"cookiesfrombrowser": cookiesfrombrowser}


class BaseProvider(ABC):
    """Base interface for all platform providers.

    Subclasses only override matches() (required) and get_extra_ydl_opts()
    (optional, defaults to an empty dict). get_info()/download() should
    never be overridden — the shared pipeline lives there.
    """

    display_name: str = "Generic"

    def __init__(self, cookie_source: str = "auto", cookie_file_path: Optional[str] = None) -> None:
        # The 'Cookie / Session Source' preference from Settings — passed
        # in via ProviderRegistry.resolve() (see task_queue.py:_process_task).
        # Subclasses don't override __init__, so they all inherit this.
        self._cookie_source = cookie_source
        self._cookie_file_path = cookie_file_path

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool:
        """Tells whether this URL belongs to this platform."""
        raise NotImplementedError

    def get_extra_ydl_opts(self) -> dict:
        """Platform-specific extra yt-dlp options (extractor_args, http_headers, etc).

        Default: empty dict — for most platforms, yt-dlp's own extractor
        already works without extra options.
        """
        return {}

    def clean_url(self, url: str) -> str:
        """Cleans the URL before analysis/download (tracking parameters etc).

        Default: returns the URL unchanged. Platforms whose share links add
        unnecessary/breaking query parameters override this (see
        TikTokProvider). If cleaning fails for any reason (unexpected URL
        format), it's always safer to continue with the original URL —
        overriding providers must guarantee this themselves.
        """
        return url

    def _resolve_cookie_opts(self) -> dict:
        """Produces the right yt-dlp cookie option based on
        self._cookie_source / self._cookie_file_path. Providers that need
        cookies (Instagram, age-restricted YouTube content) call this from
        get_extra_ydl_opts() and merge it into their own opts — a new
        platform needing cookie support just needs this one-line call.

        Note on warning scope: failures here print a console warning — this
        is not an "instant" UI warning (that would need a field on
        task_queue.py's DownloadTask and ui.py showing it in the status
        label/toast). The console warning at least makes failures visible
        rather than fully silent.
        """
        if self._cookie_source == "disabled":
            return {}

        if self._cookie_source == "file":
            # os.path.isfile (Path.exists() also matches a directory,
            # isfile is more precise) + file size > 0: an empty cookies.txt
            # would silently look "successful" but give yt-dlp no actual
            # cookies. If either check fails, we warn explicitly and
            # continue without cookies — we never fall back to
            # cookiesfrombrowser here (the user said "manual file", not
            # "automatic").
            path = self._cookie_file_path
            if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                # This branch ONLY ever returns 'cookiefile' — the
                # 'cookiesfrombrowser' key never appears in this dict, so
                # get_extra_ydl_opts()'s opts.update(...) never sends both
                # to yt-dlp at once.
                return {"cookiefile": path}
            reason = "file not found" if not (path and os.path.isfile(path)) else "file is empty (0 bytes)"
            print(
                f"[providers] Warning: 'Manual cookies.txt' selected but {reason} "
                f"({path!r}) — continuing without cookies (not falling back to "
                f"cookiesfrombrowser, since the user didn't choose automatic mode)."
            )
            return {}

        if self._cookie_source in ("firefox", "brave", "chrome", "edge"):
            # The user explicitly picked one browser — we only try that one;
            # if it fails (locked/missing) we continue without cookies
            # rather than silently trying another browser. An explicit
            # choice should be respected as such.
            resolution = _resolve_single_browser(self._cookie_source)
            if resolution is None:
                print(
                    f"[providers] Warning: could not read '{self._cookie_source}' cookies "
                    f"(not installed, locked, or profile not found) — continuing without "
                    f"cookies. Try closing the browser and retrying, or switch to "
                    f"'Automatic' mode in Settings."
                )
            return _cookie_opts_from_resolution(resolution)

        # "auto" (default) — try Firefox -> Brave -> Chrome -> Edge in order,
        # use the first one that works (see _detect_browser_for_cookies).
        resolution = _detect_browser_for_cookies()
        if resolution is None:
            print(
                "[providers] Warning: all browsers tried in 'Automatic' mode "
                "(Firefox, Brave, Chrome, Edge) failed — continuing without "
                "cookies. If this content requires a session/cookies, the "
                "download may fail."
            )
        return _cookie_opts_from_resolution(resolution)

    # ------------------------------------------------------------------
    # Shared, concrete implementation — every provider inherits this as-is
    # ------------------------------------------------------------------
    def get_info(self, url: str) -> Union[VideoInfo, PlaylistInfo]:
        """Analyzes the URL (returns VideoInfo or PlaylistInfo)."""
        return analyze(self.clean_url(url), extra_ydl_opts=self.get_extra_ydl_opts())

    def download(
        self,
        url: str,
        output_dir: Path,
        media_format: str,
        quality_key: str,
        audio_bitrate: Optional[str] = None,
        progress_callback: Optional[callable] = None,
        filename_template: str = "%(title)s",
        embed_thumbnail: bool = True,
        concurrent_fragments: int = 4,
        status_messages: Optional[dict] = None,
        cancel_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
        download_subtitles: bool = False,
        subtitle_langs: Optional[list[str]] = None,
        speed_limit_kbps: int = 0,
    ) -> Path:
        """Downloads the video. Signature matches downloader.download_media()
        exactly (minus extra_ydl_opts, which the provider adds itself)."""
        return download_media(
            url=self.clean_url(url),
            output_dir=output_dir,
            media_format=media_format,
            quality_key=quality_key,
            audio_bitrate=audio_bitrate,
            progress_callback=progress_callback,
            filename_template=filename_template,
            embed_thumbnail=embed_thumbnail,
            concurrent_fragments=concurrent_fragments,
            status_messages=status_messages,
            extra_ydl_opts=self.get_extra_ydl_opts(),
            cancel_event=cancel_event,
            pause_event=pause_event,
            download_subtitles=download_subtitles,
            subtitle_langs=subtitle_langs,
            speed_limit_kbps=speed_limit_kbps,
        )


class YouTubeProvider(BaseProvider):
    display_name = "YouTube"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = _host(url)
        return "youtube.com" in host or host == "youtu.be" or host.endswith(".youtu.be")

    def get_extra_ydl_opts(self) -> dict:
        # extractor_args needs the nested dict[str, dict[str, list[str]]]
        # format that yt-dlp's Python API expects (see TwitterProvider's
        # {"twitter": {"api": [...]}} below for the same pattern) — a flat
        # list of strings isn't reliably parsed.
        #
        # Multiple player clients are listed as a fallback chain rather than
        # a single one: YouTube occasionally blocks/restricts specific
        # clients (especially android); giving several lets yt-dlp skip a
        # failing one and try the next.
        #
        # Note: includes 'web', which can trigger a "No supported
        # JavaScript runtime could be found" warning (the web client's
        # n-parameter resolution needs a JS interpreter). This is just a
        # warning, not a download blocker — installing a JS runtime (e.g.
        # Deno) or removing 'web' from the list silences it.
        opts: dict = {
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}}
        }
        # For age-restricted videos: added when the user picked a cookie
        # source in Settings (default "auto"); not added at all if "disabled".
        opts.update(self._resolve_cookie_opts())
        return opts


@lru_cache(maxsize=1)
def _probe_impersonate_targets() -> list:
    """Returns the raw list of all currently-usable impersonate targets
    (mobile + desktop mixed) if curl_cffi is installed AND within the
    version range yt-dlp supports. Empty list if none are usable.

    Shared/cached probe: both TikTok's desktop-first selection and
    Instagram's mobile-first selection (see
    _resolve_instagram_impersonate_target) use this instead of each running
    the expensive probe (spinning up a YoutubeDL instance and its internal
    request handlers) separately. @lru_cache(maxsize=1): the curl_cffi
    installation won't change during the app's run, so computing this once
    per process is enough.

    Important: elements are deliberately ImpersonateTarget OBJECTS, not
    strings. yt-dlp's Python API requires ydl_opts['impersonate'] to be an
    ImpersonateTarget object; a plain string is rejected with an
    AssertionError instead of a normal YoutubeDLError.
    """
    try:
        from yt_dlp import YoutubeDL

        probe = YoutubeDL({"quiet": True, "no_warnings": True})
        available = probe._get_available_impersonate_targets()
    except Exception as exc:
        print(
            f"[providers] Unexpected error while probing impersonate targets: "
            f"{exc!r} — falling back to a User-Agent-based approach."
        )
        return []

    if not available:
        try:
            import curl_cffi

            installed_version = getattr(curl_cffi, "__version__", "unknown")
            print(
                f"[providers] curl_cffi is installed (version {installed_version}) BUT "
                f"yt-dlp doesn't see any usable impersonate target — most likely "
                f"it's outside the curl_cffi version range this yt-dlp version "
                f"supports (yt-dlp generally supports a specific range, not "
                f"'whatever is newest'). To check the supported range, run: "
                f"`python -c \"from yt_dlp.networking._curlcffi import *\"` "
                f"and look at the version range in the error message, then pin "
                f"curl_cffi to that range, e.g. "
                f"`pip install \"curl_cffi>=0.10,<0.16\"` (the EXACT range "
                f"depends on your yt-dlp version — verify with the command above)."
            )
        except ImportError:
            print(
                "[providers] curl_cffi is not installed — impersonate disabled, "
                "falling back to a User-Agent-based approach."
            )
        return []

    return available


@lru_cache(maxsize=1)
def _resolve_desktop_impersonate_target():
    """Picks a DESKTOP (non-android/ios) Chrome/Edge target from
    _probe_impersonate_targets() — used by TikTokProvider, which prefers a
    desktop-flow fingerprint.

    Deliberately doesn't hardcode a specific target name (e.g. 'chrome110'):
    which targets exist depends on the installed curl_cffi version (see
    _probe_impersonate_targets' docstring).
    """
    available = _probe_impersonate_targets()
    if not available:
        return None

    for wanted_client in ("chrome", "edge"):
        for target, _handler in available:
            if target.client == wanted_client and target.os not in ("android", "ios"):
                print(f"[providers] Found impersonate target for TikTok: {target}")
                return target

    # No Chrome/Edge — fall back to any other desktop (non-mobile) target
    # (could be Safari or Firefox).
    for target, _handler in available:
        if target.os not in ("android", "ios"):
            print(f"[providers] Found impersonate target for TikTok (no chrome/edge, using alternative): {target}")
            return target

    print(
        "[providers] curl_cffi is installed and supported but no DESKTOP "
        "target was found (only mobile targets may be available) — falling "
        "back to a User-Agent-based approach."
    )
    return None


@lru_cache(maxsize=1)
def _resolve_instagram_impersonate_target():
    """Picks a target for Instagram from _probe_impersonate_targets().

    Priority order:
    1) Mobile Chrome/Edge (e.g. 'chrome-android') — a client family proven
       to work reliably, plus Instagram's mobile-flow advantage.
    2) Desktop Chrome/Edge — the same profile TikTokProvider uses, proven
       to work in this environment.
    3) If no Chrome/Edge target exists at all: does NOT fall back to
       Safari/iOS (this environment saw those fail with a TLS cipher/
       BAD_DECRYPT error during connect, while Chrome/Edge impersonation
       worked fine — a known fragility area in curl_cffi's Safari/iOS
       support). Returns None instead, so InstagramProvider falls back to
       its original User-Agent-based approach.
    """
    available = _probe_impersonate_targets()
    if not available:
        return None

    for target, _handler in available:
        if target.client in ("chrome", "edge") and target.os in ("ios", "android"):
            print(f"[providers] Found mobile Chrome/Edge impersonate target for Instagram: {target}")
            return target

    for target, _handler in available:
        if target.client in ("chrome", "edge"):
            print(
                f"[providers] No mobile Chrome/Edge for Instagram, using desktop "
                f"Chrome/Edge (same profile proven to work for TikTok): {target}"
            )
            return target

    print(
        "[providers] No Chrome/Edge impersonate target found for Instagram "
        "— deliberately skipping Safari/iOS targets (known TLS failure in "
        "this environment); falling back to a User-Agent-based approach."
    )
    return None


class TikTokProvider(BaseProvider):
    display_name = "TikTok"

    # Tracking parameters TikTok adds to share links (especially ones
    # copied via the mobile "Share" button) that aren't needed for
    # downloading. Deliberately a NAME-based list rather than stripping the
    # whole query string blindly — this way, if a genuinely functional
    # parameter appears later (e.g. a specific slide/photo index), it won't
    # be accidentally removed.
    _TRACKING_PARAMS = {
        "_r", "_t", "sender_device", "sender_web_id", "is_from_webapp",
        "web_id", "share_app_id", "timestamp", "checksum", "share_link_id",
        "language", "utm_source", "utm_medium", "utm_campaign", "source",
    }

    @classmethod
    def matches(cls, url: str) -> bool:
        # Shortened links (vt.tiktok.com / vm.tiktok.com) also pass this
        # check since _host() does a "tiktok.com" substring match — no
        # separate regex needed. Following their HTTP redirect is handled
        # natively by yt-dlp's own HTTP client, not by us.
        return "tiktok.com" in _host(url)

    def clean_url(self, url: str) -> str:
        """Strips tracking query parameters; doesn't touch the actual
        video/photo path (`/video/<id>` or `/photo/<id>`) or short links.

        If cleaning fails for any reason (unexpected format), returns the
        original URL unchanged — breaking a download by mangling a URL
        while "cleaning" it is much worse than not cleaning it at all.
        """
        try:
            parsed = urlparse(url.strip())
            kept_params = [
                (k, v)
                for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k not in self._TRACKING_PARAMS
            ]
            cleaned = parsed._replace(query=urlencode(kept_params), fragment="")
            return urlunparse(cleaned)
        except Exception:
            return url

    def get_extra_ydl_opts(self) -> dict:
        # Uses whatever desktop Chrome/Edge target the installed curl_cffi
        # version actually supports (see _resolve_desktop_impersonate_target)
        # rather than a hardcoded target name. If unavailable or the
        # installed version is unsupported, this returns None and we fall
        # back to the desktop User-Agent below — YoutubeDL(**opts)
        # construction never crashes either way.
        impersonate_target = _resolve_desktop_impersonate_target()

        if impersonate_target:
            # Deliberately not setting a User-Agent here: when impersonate
            # is active, curl_cffi automatically applies its own header set
            # (User-Agent included) matching the chosen target's real
            # TLS/HTTP2 fingerprint. Adding our own User-Agent on top would
            # create a mismatch ("TLS says chrome131 but User-Agent claims a
            # different version/browser") — exactly the kind of
            # inconsistency bot protection looks for. Referer/Origin are
            # TikTok-specific and fingerprint-independent, so we keep those.
            opts: dict = {
                "impersonate": impersonate_target,
                "http_headers": {
                    "Referer": "https://www.tiktok.com/",
                    "Origin": "https://www.tiktok.com",
                },
            }
        else:
            # No curl_cffi, or the installed version is incompatible with
            # yt-dlp — we can't impersonate at the TLS level. At least look
            # like a real, current desktop Chrome at the HTTP level
            # (sec-ch-ua and Accept-Language included — TikTok's web
            # protection can also flag their absence).
            opts = {
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.tiktok.com/",
                    "Origin": "https://www.tiktok.com",
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            }

        opts.update(self._resolve_cookie_opts())
        return opts


class InstagramProvider(BaseProvider):
    display_name = "Instagram"

    @classmethod
    def matches(cls, url: str) -> bool:
        # Host-based check only, doesn't look at the path
        # (/reel/, /reels/, /p/, /stories/...) — every path under
        # instagram.com already matches, and a path-specific regex here
        # would be both unnecessary and add maintenance burden whenever
        # Instagram changes its URL formats (which it does often).
        return "instagram.com" in _host(url)

    def get_extra_ydl_opts(self) -> dict:
        # Instagram returns empty/incomplete responses to requests that
        # don't look like a browser. Switching to an iPhone/Safari UA
        # INSTEAD OF a desktop Chrome UA: Instagram's mobile web flow
        # generally applies less aggressive bot protection than the
        # desktop flow — so we replace the desktop UA entirely rather than
        # using both (for one consistent "which device am I" signal).
        # The correct option key is 'http_headers' — there's no 'headers'
        # option in yt-dlp; using that name would be silently ignored.
        # downloader.py's _merge_ydl_opts() merges this key specially
        # (without overwriting).
        #
        # If a curl_cffi impersonate target is available (see
        # _resolve_instagram_impersonate_target — mobile-first, unlike
        # TikTok's desktop-first preference), requests go through
        # curl_cffi's own TLS/HTTP stack (different from Python's stdlib
        # ssl) instead. In that case we don't set our own User-Agent —
        # impersonate already applies a consistent header set for the
        # chosen target; adding a manual UA on top would create the same
        # kind of TLS-vs-header mismatch described in TikTokProvider. If no
        # curl_cffi/suitable target is available, we keep the existing
        # iPhone UA fallback.
        impersonate_target = _resolve_instagram_impersonate_target()

        if impersonate_target:
            opts: dict = {"impersonate": impersonate_target}
        else:
            opts = {
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
                        "Mobile/15E148 Safari/604.1"
                    )
                },
            }

        # legacy_server_connect: explicitly allows HTTPS connections to
        # servers that don't support RFC 5746 secure renegotiation. Added
        # as a low-risk compatibility flag; no confirmed evidence Instagram
        # needs it.
        opts["legacy_server_connect"] = True

        # Browser cookies (for private accounts/age-restricted content)
        # still come from the Settings-selected source via
        # _resolve_cookie_opts().
        opts.update(self._resolve_cookie_opts())
        return opts


class TwitterProvider(BaseProvider):
    display_name = "Twitter / X"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = _host(url)
        return host in {"twitter.com", "x.com"} or host.endswith(".twitter.com") or host.endswith(".x.com")

    def get_extra_ydl_opts(self) -> dict:
        # More stable using the syndication API, which doesn't require login.
        return {"extractor_args": {"twitter": {"api": ["syndication"]}}}


class GenericProvider(BaseProvider):
    """Fallback for any site yt-dlp supports but that has no dedicated Provider."""

    display_name = "Generic"

    @classmethod
    def matches(cls, url: str) -> bool:
        return True  # Always matches — must always be LAST in ProviderRegistry


class ProviderRegistry:
    """A simple router/factory that picks the right Provider for a URL.

    Adding a new platform: write a new class deriving from BaseProvider (as
    above) and add it to _PROVIDERS (before GenericProvider). Nothing else
    (downloader.py, task_queue.py, ui.py) needs to change.
    """

    _PROVIDERS: list[type[BaseProvider]] = [
        YouTubeProvider,
        TikTokProvider,
        InstagramProvider,
        TwitterProvider,
        GenericProvider,  # Always last — fallback, matches every URL
    ]

    @classmethod
    def resolve(
        cls,
        url: str,
        cookie_source: str = "auto",
        cookie_file_path: Optional[str] = None,
    ) -> BaseProvider:
        for provider_cls in cls._PROVIDERS:
            if provider_cls.matches(url):
                return provider_cls(cookie_source=cookie_source, cookie_file_path=cookie_file_path)
        # Never reached in practice (GenericProvider matches every URL).
        return GenericProvider(cookie_source=cookie_source, cookie_file_path=cookie_file_path)

    @classmethod
    def is_known_platform(cls, url: str) -> bool:
        """resolve() matches every URL (falling back to GenericProvider),
        so there's normally no concept of an "unsupported link". Clipboard
        auto-detection needs the OPPOSITE: only auto-fill the URL box for
        links belonging to a KNOWN platform (YouTube/TikTok/Instagram/
        Twitter) — unrelated text or e.g. an Amazon link copied to the
        clipboard shouldn't leak into the box.

        This doesn't implement new URL-matching logic — it reuses each
        provider's existing matches() classmethod (excluding
        GenericProvider), so "which domain belongs to which platform" stays
        defined in one place (this file); ui.py doesn't know any of this,
        it just calls this function.
        """
        for provider_cls in cls._PROVIDERS:
            if provider_cls is GenericProvider:
                continue
            if provider_cls.matches(url):
                return True
        return False
