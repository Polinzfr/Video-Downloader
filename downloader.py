"""Background module handling media download and conversion."""

from __future__ import annotations

import io
import re
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import prepend_extension
from PIL import Image

from i18n import AUDIO_FORMATS, VIDEO_FORMATS


class DownloadError(Exception):
    """Generic download error."""


class FFmpegNotFoundError(DownloadError):
    """Raised when FFmpeg isn't found on the system."""


class InvalidURLError(DownloadError):
    """Raised for an invalid or unreachable URL."""


class NetworkError(DownloadError):
    """Raised for network connectivity issues."""


class TaskCancelledError(DownloadError):
    """Raised internally (from the progress hook) when the user cancels a
    running download. Caught by task_queue.py via cancel_event, not shown
    as a real error to the user."""


@dataclass
class VideoInfo:
    """Holds analyzed media info."""

    title: str
    duration: int
    thumbnail_url: str
    platform: str
    upload_date_raw: str
    channel: str
    view_count: Optional[int]
    like_count: Optional[int]
    dislike_count: Optional[int]
    thumbnail_image: Optional[Image.Image] = None
    error: Optional[str] = None
    """When set, this VideoInfo represents an error/unavailability state
    (e.g. a private/deleted video). ui.py checks this field to show a clear
    status message to the user instead of 'analysis_done', without
    re-calling analyze_url repeatedly."""


@dataclass
class PlaylistEntryInfo:
    """Summary info (from extract_flat) for a single video within a playlist."""

    index: int
    video_id: str
    title: str
    duration: Optional[int]
    url: str
    thumbnail_url: str = ""


@dataclass
class PlaylistInfo:
    """Summary info and video list for an analyzed playlist."""

    title: str
    description: str
    uploader: str
    entry_count: int
    entries: list[PlaylistEntryInfo]
    thumbnail_url: str = ""
    thumbnail_image: Optional[Image.Image] = None


@dataclass
class ProgressInfo:
    """Holds download progress state."""

    percent: float
    speed: str
    eta: str
    status: str


ProgressCallback = Callable[[ProgressInfo], None]

PLATFORM_NAMES = {
    "youtube": "YouTube",
    "youtubetab": "YouTube",
    "twitter": "Twitter / X",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "vimeo": "Vimeo",
    "twitch": "Twitch",
    "reddit": "Reddit",
    "dailymotion": "Dailymotion",
    "soundcloud": "SoundCloud",
    "bilibili": "Bilibili",
    "vk": "VK",
    "rumble": "Rumble",
}

VIDEO_HEIGHT_MAP = {
    "quality_2160": 2160,
    "quality_1440": 1440,
    "quality_1080": 1080,
    "quality_720": 720,
    "quality_480": 480,
    "quality_360": 360,
    "quality_240": 240,
    "quality_144": 144,
}

AUDIO_BITRATE_MAP = {
    "audio_320": "320",
    "audio_256": "256",
    "audio_192": "192",
    "audio_128": "128",
    "audio_64": "64",
    "audio_32": "32",
}


class _AudioBitrateDowngradePP(FFmpegPostProcessor):
    """Downgrades the audio bitrate of video files while keeping the video
    stream copied (no re-encode).

    Hooked into yt-dlp's own postprocessor pipeline (when="after_move"). At
    this stage the file has already been moved to its final location and
    fully released by yt-dlp, which avoids the file-lock (WinError 32) and
    timing issues a manual subprocess call would cause.
    """

    def __init__(self, downloader, target_abr: str):
        super().__init__(downloader)
        self._target_abr = target_abr

    def run(self, info):
        filepath = info.get("filepath")
        if not filepath or not Path(filepath).exists():
            return [], info

        temp_path = prepend_extension(filepath, "abrtmp")

        try:
            self.run_ffmpeg(
                filepath,
                temp_path,
                ["-c:v", "copy", "-c:a", "aac", "-b:a", f"{self._target_abr}k",
                 "-strict", "experimental"],
            )
        except Exception as exc:
            if Path(temp_path).exists():
                Path(temp_path).unlink(missing_ok=True)
            self.report_warning(
                f"Ses kalitesi düşürme başarısız oldu, orijinal dosya korunuyor: {exc}"
            )
            return [], info

        Path(filepath).unlink(missing_ok=True)
        Path(temp_path).rename(filepath)
        self.to_screen(f"Ses kalitesi {self._target_abr}k değerine düşürüldü.")
        return [], info


def check_ffmpeg() -> None:
    "Checks whether FFmpeg is installed on the system."
    if not shutil.which("ffmpeg"):
        raise FFmpegNotFoundError(
            "FFmpeg sistemde bulunamadı."
            "Lütfen https://ffmpeg.org/download.html adresinden kurun."
            "ve PATH değişkenine ekleyin."
        )


def _thumbnail_embedding_available() -> bool:
    """Checks whether mutagen is available for the EmbedThumbnail postprocessor."""
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        return False


def _format_duration(seconds: int) -> str:
    """Converts seconds to HH:MM:SS format."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_speed(speed: Optional[float]) -> str:
    """Converts a yt-dlp speed value to a readable format."""
    if not speed:
        return "—"
    if speed >= 1_000_000:
        return f"{speed / 1_000_000:.1f} MB/s"
    if speed >= 1_000:
        return f"{speed / 1_000:.1f} KB/s"
    return f"{speed:.0f} B/s"


def _format_eta(eta: Optional[int]) -> str:
    """Converts remaining time to a readable format."""
    if eta is None:
        return "—"
    return _format_duration(eta)


def format_count(value: Optional[int]) -> str:
    """Formats large numbers in shorthand (1.2M, 5.4K)."""
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_upload_date(raw_date: str, language: str = "tr") -> str:
    """Converts a yt-dlp upload_date (YYYYMMDD) value to a readable date."""
    if not raw_date or len(raw_date) != 8:
        return "—"
    try:
        dt = datetime.strptime(raw_date, "%Y%m%d")
        if language == "en":
            return dt.strftime("%b %d, %Y")
        months = [
            "Oca", "Şub", "Mar", "Nis", "May", "Haz",
            "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara",
        ]
        return f"{dt.day} {months[dt.month - 1]} {dt.year}"
    except ValueError:
        return raw_date


def _resolve_platform(info: dict) -> str:
    """Produces a readable platform name from yt-dlp's output."""
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if extractor in PLATFORM_NAMES:
        return PLATFORM_NAMES[extractor]

    for key, name in PLATFORM_NAMES.items():
        if key in extractor:
            return name

    if extractor:
        return extractor.replace("_", " ").title()
    return "Web"


def _strip_ansi(text: str) -> str:
    """Strips yt-dlp terminal color codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _parse_quality_height(quality_key: str) -> Optional[int]:
    """Resolves a video height from whatever the UI passes in (index, text, key)."""
    q_str = str(quality_key).lower().strip()

    if q_str in VIDEO_HEIGHT_MAP:
        return VIDEO_HEIGHT_MAP[q_str]

    for k, v in VIDEO_HEIGHT_MAP.items():
        if k in q_str:
            return v

    numbers = re.findall(r'\d+', q_str)
    for num_str in numbers:
        num = int(num_str)
        if num in VIDEO_HEIGHT_MAP.values() or num in [2160, 1440, 1080, 720, 480, 360, 240, 144]:
            return num

    if "en düşük" in q_str or "low" in q_str or "worst" in q_str:
        return 144
    if "en yüksek" in q_str or "high" in q_str or "best" in q_str:
        return 1080

    return None


def _parse_audio_bitrate(quality_key: str) -> Optional[str]:
    """Resolves an audio bitrate from whatever the UI passes in."""
    q_str = str(quality_key).lower().strip()
    if q_str in AUDIO_BITRATE_MAP:
        return AUDIO_BITRATE_MAP[q_str]

    for k, v in AUDIO_BITRATE_MAP.items():
        if k in q_str:
            return v

    numbers = re.findall(r'\d+', q_str)
    for num_str in numbers:
        if num_str in AUDIO_BITRATE_MAP.values() or num_str in ["320", "256", "192", "128", "64", "32"]:
            return num_str

    return None


def _quality_label_for_filename(quality_key: str, media_format: str) -> str:
    """Quality label appended to the filename (1080p, 192k, etc.)."""
    if media_format in VIDEO_FORMATS:
        height = _parse_quality_height(quality_key)
        return f"{height}p" if height else "best"

    abr = _parse_audio_bitrate(quality_key)
    return f"{abr}k" if abr else "best"


def _build_format_string(media_format: str, quality_key: str, audio_bitrate: str = None) -> str:
    """Builds yt-dlp's -f parameter from the UI's quality selection."""
    if media_format in VIDEO_FORMATS:
        # "quality_best" (the menu's 'Highest') is a special sentinel — it
        # represents the true best, not a specific resolution. Deliberately
        # short-circuited here BEFORE hitting _parse_quality_height's
        # generic keyword fallback, which would otherwise match the
        # substring "best" and cap it at a fixed 1080 — meaning a user who
        # picked "Highest" would only ever get up to 1080p even if the
        # video offers 4K/1440p. Here it goes straight to yt-dlp's real
        # 'best' selector with no ceiling.
        q_str = str(quality_key).lower().strip()
        if q_str == "quality_best":
            return "bestvideo+bestaudio/best"

        height = _parse_quality_height(quality_key)
        if height:
            return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        return "bestvideo+bestaudio/best"

    # Audio-only download.
    abr = audio_bitrate.lower().replace('bps', '').replace('k', '').strip() if audio_bitrate else _parse_audio_bitrate(quality_key)
    if abr:
        return f"bestaudio[abr<={abr}]/bestaudio/best"
    return "bestaudio/best"


def _make_unique_path(path: Path) -> Path:
    """Produces a unique path by appending (1), (2), ... if the file already exists."""
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _build_unique_outtmpl(
    output_path: Path,
    info: dict,
    filename_template: str,
    media_format: str,
    quality_key: str,
    ext_map: dict[str, str],
) -> str:
    """Builds a collision-free outtmpl template."""
    template = filename_template.strip() or "%(title)s"
    quality_label = _quality_label_for_filename(quality_key, media_format)
    base_pattern = f"{template}_{quality_label}"

    preview_opts = {
        "quiet": True,
        "noplaylist": True,
        "no_cache": True,
    }
    with yt_dlp.YoutubeDL(preview_opts) as ydl:
        preview_path = Path(
            ydl.prepare_filename(
                info,
                outtmpl=str(output_path / f"{base_pattern}.%(ext)s"),
            )
        )

    final_ext = ext_map[media_format]
    target_path = preview_path.with_suffix(f".{final_ext}")
    unique_path = _make_unique_path(target_path)

    return str(unique_path.with_suffix(".%(ext)s"))


def _build_postprocessors(
    media_format: str,
    quality_key: str,
    embed_thumbnail: bool = True,
    audio_bitrate: Optional[str] = None
) -> list[dict]:
    """Builds the yt-dlp postprocessor list for the selected format."""
    postprocessors: list[dict] = []

    ext_map = {
        "MP4": "mp4", "MKV": "mkv", "WEBM": "webm", "AVI": "avi",
        "MP3": "mp3", "M4A": "m4a", "WAV": "wav", "FLAC": "flac", "OPUS": "opus",
    }

    if media_format in VIDEO_FORMATS:
        if audio_bitrate:
            pass
        return postprocessors

    can_embed = embed_thumbnail and _thumbnail_embedding_available()
    target_abr = audio_bitrate.split(' ')[0] if audio_bitrate else _parse_audio_bitrate(quality_key) or "192"

    audio_codec_map = {"MP3": "mp3", "WAV": "wav", "M4A": "m4a", "FLAC": "flac", "OPUS": "opus"}
    codec = audio_codec_map.get(media_format, "mp3")

    postprocessors.append({
        "key": "FFmpegExtractAudio",
        "preferredcodec": codec,
        "preferredquality": target_abr.replace("bps", "").replace("k", ""),
    })

    if can_embed:
        postprocessors.append({"key": "EmbedThumbnail"})

    return postprocessors


def _load_thumbnail(url: str) -> Optional[Image.Image]:
    """Loads a PIL Image from a thumbnail URL."""
    if not url:
        return None

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = response.read()
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


def _map_exception(exc: Exception, ydl_opts: Optional[dict] = None) -> DownloadError:
    """Maps raw exceptions to user-friendly error classes."""
    message = _strip_ansi(str(exc))
    message_lower = message.lower()

    # Whether cookies were ACTUALLY sent to yt-dlp, read from the FINAL
    # ydl_opts (the one providers.py actually produced) rather than a raw
    # cookie_source string — this matters because the user might have
    # picked a browser whose cookies then failed to read (locked/missing),
    # in which case 'cookiesfrombrowser' never made it into ydl_opts at
    # all, so the correct message is "pick a cookie source", not "your
    # cookie file is invalid" (there isn't one).
    has_cookies = bool(ydl_opts and (ydl_opts.get("cookiesfrombrowser") or ydl_opts.get("cookiefile")))

    if isinstance(exc, yt_dlp.utils.DownloadError):
        if "ffmpeg" in message_lower or "ffprobe" in message_lower:
            return FFmpegNotFoundError(
                "FFmpeg bulunamadı veya çalıştırılamadı. Lütfen kurulumu kontrol edin."
            )
        if "requested format is not available" in message_lower:
            return DownloadError(
                "Seçilen kalite bu video için mevcut değil. "
                "Daha düşük bir kalite veya 'En Yüksek' seçeneğini deneyin."
            )
        if any(
            keyword in message_lower
            for keyword in ("unable to download", "network", "connection", "timed out")
        ):
            return NetworkError(
                "Ağ bağlantısı hatası. İnternet bağlantınızı kontrol edip tekrar deneyin."
            )
        if any(
            keyword in message_lower
            for keyword in ("unsupported url", "invalid", "no video", "private video")
        ):
            return InvalidURLError(
                "Geçersiz veya desteklenmeyen link. Desteklenen sitelerden bir link deneyin."
            )
        # Instagram's "empty media response" is the one case where the fix
        # is specifically a browser session/cookies — routed to a concrete,
        # actionable message pointing at Settings, rather than the generic
        # fallback below.
        if "empty media response" in message_lower:
            if has_cookies:
                # Cookies WERE actually sent (cookiesfrombrowser or
                # cookiefile was present in ydl_opts) but it still failed —
                # never say "you didn't select cookies" here, since that
                # would be false. The real cause is most likely an expired
                # or invalid cookie.
                return DownloadError(
                    "Yüklenen çerez dosyası ile Instagram oturumu doğrulanamadı. "
                    "Lütfen cookies.txt dosyanızı yenileyin (Instagram'a tekrar "
                    "giriş yapıp tarayıcınızdan yeniden dışa aktarın) ya da "
                    "Ayarlar'dan farklı bir çerez kaynağı seçin."
                )
            return DownloadError(
                "Instagram videolarını indirebilmek için Ayarlar menüsünden "
                "çerez (cookies) kaynağını seçmeniz veya bir cookies.txt "
                "dosyası tanımlamanız gerekmektedir."
            )
        # TikTok's "status code 0" happens when we fail its web bot-
        # protection/JS challenge (see providers.py:TikTokProvider —
        # largely mitigated when curl_cffi impersonate is installed).
        if "status code 0" in message_lower and "video not available" in message_lower:
            return DownloadError(
                "TikTok bu videoyu vermeyi reddetti (bot koruması). "
                "yt-dlp'nin güncel olduğundan emin olun; sorun sürerse "
                "birkaç dakika sonra tekrar deneyin."
            )
        # If we got here, _run_with_cookie_fallback's automatic retries
        # (see _is_tiktok_transient_challenge_error) were already exhausted
        # — TikTok's bot protection is being persistent. Without curl_cffi
        # impersonate active, this page is nearly impossible to pass.
        if any(marker in message_lower for marker in _TIKTOK_TRANSIENT_CHALLENGE_MARKERS):
            return DownloadError(
                "TikTok, bu isteği bir bot-koruması/doğrulama sayfasıyla "
                "engelledi (birkaç otomatik tekrar denemesine rağmen). Bunu "
                "aşmak için: (1) sisteminizde yt-dlp'nin desteklediği bir "
                "curl_cffi sürümü kurulu olduğundan emin olun — TLS "
                "seviyesinde gerçek bir tarayıcı taklidi yapılmadan bu "
                "koruma çoğu zaman geçilemiyor; ya da (2) Ayarlar'dan "
                "TikTok için geçerli bir çerez kaynağı (giriş yapılmış "
                "tarayıcı ya da güncel bir cookies.txt) seçin. Sorun "
                "sürerse birkaç dakika sonra tekrar deneyin."
            )
        if any(
            keyword in message_lower
            for keyword in ("confirm your age", "age-restricted", "age restricted")
        ):
            return DownloadError(
                "Bu video yaş sınırlaması içeriyor. Bu tür videoları indirebilmek "
                "için tarayıcı oturumu (cookie) entegrasyonu gerekiyor."
            )
        if any(
            keyword in message_lower
            for keyword in (
                "login required",
                "rate-limit reached",
                "requested content is not available",
                "restricted video",
                "not available in your country",
            )
        ):
            return DownloadError(
                "Bu içerik oturum açmayı veya ek doğrulama gerektiriyor "
                "(bot koruması / bölge kısıtlaması). Tarayıcı oturumu (cookie) "
                "entegrasyonu ile tekrar denenebilir."
            )

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return NetworkError(
            "Ağ bağlantısı hatası. İnternet bağlantınızı kontrol edip tekrar deneyin."
        )

    # Defensive length cap: even for an error matching none of the patterns
    # above (never seen before), the user should never be hit with a huge
    # technical wall of text.
    if len(message) > 220:
        message = message[:220] + "..."
    return DownloadError(f"Beklenmeyen bir hata oluştu: {message}")


def _sanitize_text(value: Optional[str], fallback: str = "") -> str:
    """Sanitizes text fields coming from yt-dlp (title, channel name, etc).

    Titles from some platforms (especially emoji/non-Latin-heavy sites like
    TikTok/Instagram) can contain broken unicode surrogates or control
    characters. These can cause a silent exception during Tkinter/Tcl
    rendering, leaving the UI half-updated — so we sanitize the data here,
    at the source, before it ever reaches the UI.
    """
    if not value:
        return fallback

    try:
        # errors="replace": swaps unencodable (e.g. surrogate) characters
        # for a safe placeholder instead of raising.
        cleaned = value.encode("utf-8", errors="replace").decode("utf-8")
    except Exception as exc:
        print(f"[downloader] Text sanitization error, raw value: {value!r} -> {exc}")
        return fallback

    # Strip control characters (except newline/tab) — these can also cause
    # issues in Tk.
    cleaned = "".join(ch for ch in cleaned if ch >= " " or ch in "\n\t").strip()
    return cleaned or fallback


def _build_video_info(info: dict) -> VideoInfo:
    """Builds a VideoInfo object from yt-dlp's raw info dict.

    Shared logic between analyze_url() and analyze() — split into its own
    function so that when the same 'info' dict is already on hand, no
    redundant second network request is needed.
    """
    try:
        title = _sanitize_text(info.get("title"), fallback="Bilinmeyen Başlık")
        duration = int(info.get("duration") or 0)
        thumbnail_url = info.get("thumbnail") or ""
        thumbnail_image = _load_thumbnail(thumbnail_url)
        platform = _resolve_platform(info)
        upload_date_raw = info.get("upload_date") or ""
        channel = _sanitize_text(info.get("channel") or info.get("uploader"), fallback="—")
        view_count = info.get("view_count")
        like_count = info.get("like_count")
        dislike_count = info.get("dislike_count")
    except Exception as exc:
        # On unexpected malformed data, log the raw payload and return a
        # safe fallback VideoInfo instead of crashing silently.
        print(f"[downloader] _build_video_info error: {exc}. Raw data keys: {list(info.keys())}")
        return VideoInfo(
            title="Bilinmeyen Başlık",
            duration=0,
            thumbnail_url="",
            platform=_resolve_platform(info) if isinstance(info, dict) else "Bilinmeyen",
            upload_date_raw="",
            channel="—",
            view_count=None,
            like_count=None,
            dislike_count=None,
            thumbnail_image=None,
        )

    return VideoInfo(
        title=title,
        duration=duration,
        thumbnail_url=thumbnail_url,
        platform=platform,
        upload_date_raw=upload_date_raw,
        channel=channel,
        view_count=view_count,
        like_count=like_count,
        dislike_count=dislike_count,
        thumbnail_image=thumbnail_image,
    )


def _merge_ydl_opts(base: dict, extra: Optional[dict]) -> dict:
    """Merges two yt-dlp ydl_opts dicts.

    For simple keys, 'extra' wins (override). But nested-dict fields like
    'extractor_args' and 'http_headers' are MERGED rather than overwritten
    — so one provider's own extractor settings don't wipe out another field
    that might already be in the same dict.

    Each Provider in providers.py returns a small dict from
    get_extra_ydl_opts() to be passed here as 'extra'; downloader.py never
    needs to know what's inside it, only how to merge it.
    """
    if not extra:
        return dict(base)

    merged = dict(base)
    for key, value in extra.items():
        if key in ("extractor_args", "http_headers") and isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


# Patterns seen in yt-dlp error messages caused by cookie extraction/lock
# issues. providers.py already prevents most cases with a preflight check;
# this list is only a last-resort safety net for what it misses (e.g. a
# race between the check and the real call, or decryption failing after a
# successful copy).
_COOKIE_FAILURE_KEYWORDS = (
    "could not copy",
    "could not find",
    "database is locked",
    "failed to decrypt",
    "could not decrypt",
    "permission denied",
)


def _is_cookie_related_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(keyword in message for keyword in _COOKIE_FAILURE_KEYWORDS)


# These messages are DEFINED in yt_dlp's own TikTok extractor source
# (tiktok.py, _solve_challenge_and_set_cookies) — raised when TikTok's web
# page returns a response that doesn't match the JS-challenge format the
# extractor knows how to solve (typically a bot-protection/block page). The
# root cause is server-side bot detection; not a code bug to "fix" in
# providers.py/downloader.py. In practice this detection is PARTIALLY
# random/transient — the same request can succeed a few seconds later
# (e.g. the "Analyze" step just succeeded, and the second request in the
# "Download" step then failed). So the only thing fixed here is a short-
# wait automatic retry for this specific class of error, following the
# same defense-in-depth logic as the existing cookie-fallback mechanism.
_TIKTOK_TRANSIENT_CHALLENGE_MARKERS = (
    "unexpected response from webpage request",
    "unable to extract challenge data",
    "unable to extract universal data for rehydration",
)


def _is_tiktok_transient_challenge_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TIKTOK_TRANSIENT_CHALLENGE_MARKERS)


def _run_with_cookie_fallback(ydl_opts: dict, action: Callable[["yt_dlp.YoutubeDL"], object]) -> object:
    """Opens `yt_dlp.YoutubeDL(ydl_opts)` and runs `action(ydl)`.

    If the error is cookie-extraction/file-lock related (see
    _is_cookie_related_error) AND ydl_opts has 'cookiesfrombrowser', it's
    stripped from the dict and the call is retried ONCE AUTOMATICALLY —
    so the user is never hit with a hard failure just because of a browser
    cookie issue; worst case they get a cookie-less result (usually fine
    for public content) or the short, cleaned-up message _map_exception
    now produces.

    Note (defense layers): providers.py's preflight lock check + staging a
    temp copy for Brave/Chrome/Edge already prevents the vast majority of
    issues before a download even starts. This function is its BACKUP —
    a last safety net for what staging can't prevent, such as (a) a brief
    race between the preflight check and this real call, or (b) decryption
    failing even after a successful copy (e.g. due to app-bound encryption).
    """
    # Only the SPECIFIC transient-challenge error gets a short-wait retry
    # (up to 2 extra attempts, 3 total) — no other error type (cookie
    # error, network error, invalid URL, etc.) enters this loop; those
    # still fall through to the existing logic below (or raise directly).
    max_challenge_attempts = 3
    for attempt in range(1, max_challenge_attempts + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return action(ydl)
        except yt_dlp.utils.DownloadError as exc:
            if _is_tiktok_transient_challenge_error(exc) and attempt < max_challenge_attempts:
                wait_seconds = 2 * attempt
                print(
                    f"[downloader] TikTok returned a transient block/bot-protection "
                    f"page, retrying in {wait_seconds}s "
                    f"(attempt {attempt + 1}/{max_challenge_attempts})..."
                )
                time.sleep(wait_seconds)
                continue
            if "cookiesfrombrowser" not in ydl_opts or not _is_cookie_related_error(exc):
                raise
            print(
                f"[downloader] Cookie extraction/lock error, retrying without "
                f"cookies: {exc}"
            )
            fallback_opts = dict(ydl_opts)
            fallback_opts.pop("cookiesfrombrowser", None)
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                return action(ydl)


def analyze_url(url: str, extra_ydl_opts: Optional[dict] = None) -> VideoInfo:
    """Analyzes the given URL; extracts info from any site yt-dlp supports.

    extra_ydl_opts: platform-specific extra yt-dlp settings from
    providers.py's BaseProvider.get_extra_ydl_opts() (optional, backward
    compatible — behavior is identical to before if omitted).
    """
    url = url.strip()
    if not url:
        raise InvalidURLError("Lütfen geçerli bir link girin.")

    ydl_opts = _merge_ydl_opts(
        {
            "quiet": False,
            "no_warnings": False,
            "skip_download": True,
            "socket_timeout": 15,
        },
        extra_ydl_opts,
    )

    try:
        info = _run_with_cookie_fallback(ydl_opts, lambda ydl: ydl.extract_info(url, download=False))

        if info is None:
            raise InvalidURLError("Medya bilgileri alınamadı.")

        return _build_video_info(info)
    except DownloadError:
        raise
    except Exception as exc:
        raise _map_exception(exc, ydl_opts) from exc


def _extract_thumbnail_url(data: dict) -> str:
    """Extracts the best thumbnail URL from a yt-dlp info/entry dict.

    Checks the direct 'thumbnail' field first; otherwise picks the largest
    one from the 'thumbnails' list (by resolution, if available).
    """
    direct = data.get("thumbnail")
    if direct:
        return direct

    thumbnails = data.get("thumbnails") or []
    if not thumbnails:
        return ""

    if any(t.get("width") for t in thumbnails):
        best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    else:
        # The list is usually sorted low-to-high; take the last one if no
        # resolution info is available.
        best = thumbnails[-1]

    return best.get("url") or ""


def _normalize_entry_url(entry: dict, parent_extractor: str) -> str:
    """Builds a downloadable full URL from a playlist entry in extract_flat output.

    In extract_flat mode, yt-dlp's 'url' field varies by platform: most
    extractors return a full URL, while some (like YouTube) may only
    return the video ID.
    """
    raw_url = entry.get("url") or ""

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if "youtube" in parent_extractor:
        video_id = raw_url or entry.get("id") or ""
        return f"https://www.youtube.com/watch?v={video_id}"

    # On other platforms, webpage_url usually carries the full link;
    # falling back to id as the best reference we have if that's also missing.
    return entry.get("webpage_url") or raw_url or entry.get("id") or ""


def _build_playlist_info(flat_info: dict) -> PlaylistInfo:
    """Builds a PlaylistInfo from raw playlist info retrieved via extract_flat."""
    parent_extractor = (flat_info.get("extractor_key") or flat_info.get("extractor") or "").lower()
    raw_entries = flat_info.get("entries") or []

    entries: list[PlaylistEntryInfo] = []
    # Standard placeholder titles yt-dlp returns for private/deleted videos.
    PRIVATE_TITLE_MARKERS = {"[private video]", "[deleted video]", "[unavailable video]"}

    for idx, entry in enumerate(raw_entries, start=1):
        if not entry:
            continue

        try:
            duration = entry.get("duration")

            raw_title = _sanitize_text(entry.get("title"))
            if not raw_title:
                title = f"Bilinmeyen Başlık #{idx}"
            elif raw_title.lower() in PRIVATE_TITLE_MARKERS:
                title = "Gizli/Silinmiş Video"
            else:
                title = raw_title

            entries.append(
                PlaylistEntryInfo(
                    index=idx,
                    video_id=entry.get("id") or "",
                    title=title,
                    duration=int(duration) if duration else None,
                    url=_normalize_entry_url(entry, parent_extractor),
                    thumbnail_url=_extract_thumbnail_url(entry),
                )
            )
        except Exception as exc:
            # Don't let a single malformed entry crash the whole playlist
            # analysis — log the raw data and add this video with a safe
            # fallback instead.
            print(f"[downloader] Error processing playlist entry #{idx}: {exc}. Raw data: {entry!r}")
            entries.append(
                PlaylistEntryInfo(
                    index=idx,
                    video_id=entry.get("id") or "" if isinstance(entry, dict) else "",
                    title=f"Bilinmeyen Başlık #{idx}",
                    duration=None,
                    url=_normalize_entry_url(entry, parent_extractor) if isinstance(entry, dict) else "",
                    thumbnail_url="",
                )
            )

    # Cover image fallback chain: the playlist's own thumbnail first, then
    # the first video's thumbnail.
    playlist_thumbnail_url = _extract_thumbnail_url(flat_info)
    if not playlist_thumbnail_url and entries:
        playlist_thumbnail_url = entries[0].thumbnail_url
    playlist_thumbnail_image = _load_thumbnail(playlist_thumbnail_url) if playlist_thumbnail_url else None

    return PlaylistInfo(
        title=_sanitize_text(flat_info.get("title"), fallback="Bilinmeyen Playlist"),
        description=_sanitize_text(flat_info.get("description")),
        uploader=_sanitize_text(flat_info.get("uploader") or flat_info.get("channel"), fallback="—"),
        entry_count=len(entries),
        entries=entries,
        thumbnail_url=playlist_thumbnail_url,
        thumbnail_image=playlist_thumbnail_image,
    )


def load_thumbnail(url: str) -> Optional[Image.Image]:
    """Loads a PIL Image from a thumbnail URL (general-purpose public wrapper).

    ui.py uses this to lazily load small icons for playlist rows on a
    background thread — _load_thumbnail is kept private to preserve the
    module boundary (our modularity rule).
    """
    return _load_thumbnail(url)


def analyze(url: str, extra_ydl_opts: Optional[dict] = None):
    """Analyzes a URL; returns PlaylistInfo for a playlist, VideoInfo for a single video.

    Return type: VideoInfo | PlaylistInfo. ui.py distinguishes with isinstance().
    extra_ydl_opts: see analyze_url()'s docstring (providers.py integration).
    """
    url = url.strip()
    if not url:
        raise InvalidURLError("Lütfen geçerli bir link girin.")

    flat_opts = _merge_ydl_opts(
        {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "socket_timeout": 15,
        },
        extra_ydl_opts,
    )

    try:
        info = _run_with_cookie_fallback(flat_opts, lambda ydl: ydl.extract_info(url, download=False))

        if info is None:
            raise InvalidURLError("Medya bilgileri alınamadı.")

        if info.get("_type") == "playlist" or info.get("entries") is not None:
            return _build_playlist_info(info)

        # Single video: extract_flat already returns full metadata for
        # non-playlist URLs, so we can build a VideoInfo from the same
        # 'info' without an extra network request.
        return _build_video_info(info)
    except DownloadError:
        raise
    except Exception as exc:
        raise _map_exception(exc, flat_opts) from exc


def _resolve_downloaded_filepath(
    output_path: Path,
    prepared_path: Path,
    media_format: str,
    ext_map: dict[str, str],
) -> Path:
    """Returns the actual file path after download/postprocessing."""
    expected_ext = ext_map[media_format]
    final_path = prepared_path.with_suffix(f".{expected_ext}")

    if final_path.exists():
        return final_path

    if prepared_path.exists() and prepared_path.suffix.lstrip(".").lower() == expected_ext:
        return prepared_path

    stem = prepared_path.stem
    matches = sorted(
        output_path.glob(f"{stem}*.{expected_ext}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if matches:
        return matches[0]

    return final_path


def _cleanup_partial_files(outtmpl: Optional[str]) -> None:
    """Cleans up leftover .part / .ytdl temp files from a failed download.

    yt-dlp writes the target file during download as '<name>.part'
    (including variants like '<name>.f137.part' for fragmented/DASH
    downloads) and keeps resume info in '<name>.ytdl'. If the download
    ends in an error, these files are left on disk; we glob by outtmpl's
    base name and delete them so the user isn't left with half-finished
    junk files.

    If an error occurred before outtmpl was even computed (e.g. during the
    initial pre-analysis step), there's nothing to clean up — exits
    silently in that case.
    """
    if not outtmpl:
        return
    try:
        base = Path(outtmpl)
        # outtmpl is in '.../name.%(ext)s' format; taking the base name
        # before %(ext)s.
        stem = base.name.split(".%(ext)s")[0].split(".%(")[0]
        parent = base.parent
        if not stem or not parent.exists():
            return
        for pattern in (f"{stem}*.part", f"{stem}*.ytdl"):
            for leftover in parent.glob(pattern):
                try:
                    leftover.unlink()
                    print(f"[downloader] Cleaned up leftover temp file: {leftover}")
                except OSError as unlink_err:
                    print(f"[downloader] Could not delete temp file ({leftover}): {unlink_err}")
    except Exception as exc:
        # Even if cleanup fails, it should never mask the real error — just log it.
        print(f"[downloader] _cleanup_partial_files error: {exc!r}")


def download_media(
    url: str,
    output_dir: Path,
    media_format: str,
    quality_key: str,
    audio_bitrate: Optional[str] = None,
    progress_callback: Optional[Callable[[ProgressInfo], None]] = None,
    filename_template: str = "%(title)s",
    embed_thumbnail: bool = True,
    concurrent_fragments: int = 4,
    status_messages: dict = None,
    extra_ydl_opts: Optional[dict] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    download_subtitles: bool = False,
    subtitle_langs: Optional[list[str]] = None,
    speed_limit_kbps: int = 0,
) -> Path:
    """Downloads and converts the media in the given format.

    extra_ydl_opts: see analyze_url()'s docstring (providers.py integration).
    cancel_event: if set, the progress hook raises TaskCancelledError on the
    next tick, aborting the yt-dlp download in progress.
    pause_event: if cleared, the progress hook blocks (wait()) until it's
    set again — the download thread stays alive, the socket stays open.
    download_subtitles/subtitle_langs: adds subtitle download + srt
    conversion to the postprocessor pipeline.
    speed_limit_kbps: 0 = unlimited; otherwise passed to yt-dlp as
    'ratelimit' (bytes/sec).
    """
    check_ffmpeg()

    url = url.strip()
    if not url:
        raise InvalidURLError("Lütfen geçerli bir link girin.")

    messages = status_messages or {
        "downloading": "İndiriliyor...",
        "converting": "Dönüştürülüyor...",
        "completed": "Tamamlandı",
    }

    output_path = Path(output_dir).expanduser().resolve().absolute()
    output_path.mkdir(parents=True, exist_ok=True)

    ext_map = {
        "MP4": "mp4", "MKV": "mkv", "WEBM": "webm", "AVI": "avi",
        "MP3": "mp3", "M4A": "m4a", "WAV": "wav", "FLAC": "flac", "OPUS": "opus",
    }

    format_string = _build_format_string(media_format, quality_key, audio_bitrate=None)

    postprocessors = []
    if media_format not in VIDEO_FORMATS:
        target_abr = audio_bitrate.split(' ')[0] if audio_bitrate else _parse_audio_bitrate(quality_key) or "192"
        target_abr = target_abr.replace("bps", "").replace("k", "")
        audio_codec_map = {"MP3": "mp3", "WAV": "wav", "M4A": "m4a", "FLAC": "flac", "OPUS": "opus"}
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_codec_map.get(media_format, "mp3"),
            "preferredquality": target_abr,
        })
        if embed_thumbnail and _thumbnail_embedding_available():
            postprocessors.append({"key": "EmbedThumbnail"})

    if download_subtitles:
        # yt-dlp writes subtitles in their original format (often .vtt);
        # this converts them to .srt alongside the media file.
        postprocessors.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})

    merge_format = {"MP4": "mp4", "MKV": "mkv", "WEBM": "webm"}.get(media_format)

    last_percent = {"value": 0.0}

    def hook(status_dict: dict) -> None:
        # Checked on every tick regardless of status (downloading/finished/
        # error) so a cancel during any phase is caught promptly.
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelledError("İndirme kullanıcı tarafından iptal edildi.")
        if pause_event is not None and not pause_event.is_set():
            pause_event.wait()
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelledError("İndirme kullanıcı tarafından iptal edildi.")

        if not progress_callback:
            return
        status = status_dict.get("status", "")
        if status == "downloading":
            downloaded = status_dict.get("downloaded_bytes") or 0
            total = status_dict.get("total_bytes") or status_dict.get("total_bytes_estimate")
            percent = (downloaded / total * 100) if total else 0.0
            percent = min(percent, 100.0)
            # total_bytes_estimate can fluctuate on DASH/fragmented
            # downloads; this prevents the percentage from jumping backward.
            if percent < last_percent["value"]:
                percent = last_percent["value"]
            else:
                last_percent["value"] = percent
            progress_callback(
                ProgressInfo(
                    percent=percent,
                    speed=_format_speed(status_dict.get("speed")),
                    eta=_format_eta(status_dict.get("eta")),
                    status=messages["downloading"],
                )
            )
        elif status == "finished":
            last_percent["value"] = 100.0
            progress_callback(
                ProgressInfo(percent=100.0, speed="—", eta="00:00", status=messages["converting"])
            )

    def postprocessor_hook(status_dict: dict) -> None:
        # progress_hooks yalnızca İNDİRME aşamasını (downloading/finished)
        # kapsıyor; ffmpeg dönüştürme/birleştirme/altyazı-gömme gibi asıl
        # postprocessing işleri ayrı bir hook zinciriyle (bu fonksiyon)
        # yürütülüyor. cancel_event kontrolünü buraya da koymazsak, kullanıcı
        # indirme bitip dönüştürme başladıktan SONRA iptal ederse iptal hiç
        # işlemez — dönüştürme tamamlanana kadar beklemek zorunda kalır.
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelledError("İndirme kullanıcı tarafından iptal edildi.")
        if progress_callback and status_dict.get("status") == "started":
            progress_callback(
                ProgressInfo(percent=100.0, speed="—", eta="00:00", status=messages["converting"])
            )

    outtmpl: Optional[str] = None  # defined early for .part cleanup in except blocks
    # ydl_opts is also defined early (assigned to None first): if the
    # precheck step itself (pre-analysis via extract_info), or any line
    # before it, fails, the except block's _map_exception(exc, ydl_opts)
    # call would otherwise hit an UnboundLocalError since ydl_opts was
    # normally only assigned AFTER the precheck step succeeded — which
    # would mask the REAL error behind "UnboundLocalError: cannot access
    # local variable 'ydl_opts'". Solved with the same early-definition
    # pattern already used for outtmpl: the except block can now always
    # show the real error, even if the precheck itself failed.
    ydl_opts: Optional[dict] = None

    try:
        precheck_opts = _merge_ydl_opts({"quiet": True, "noplaylist": True}, extra_ydl_opts)
        info = _run_with_cookie_fallback(precheck_opts, lambda ydl: ydl.extract_info(url, download=False))
        if info is None:
            raise InvalidURLError("Medya bilgileri alınamadı.")

        outtmpl = _build_unique_outtmpl(
            output_path, info, filename_template, media_format, quality_key, ext_map
        )

        ydl_opts = _merge_ydl_opts(
            {
                "format": format_string,
                "outtmpl": outtmpl,
                "progress_hooks": [hook],
                "postprocessor_hooks": [postprocessor_hook],
                "postprocessors": postprocessors,
                "merge_output_format": merge_format,
                "remux_video": ext_map[media_format] if media_format in VIDEO_FORMATS else None,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
                "noplaylist": True,
                "concurrent_fragment_downloads": max(1, min(concurrent_fragments, 16)),
                "overwrites": True,
                "no_cache": True,
                "continuedl": False,
                "abort_on_error": True,  # Abort immediately on error, never produce a fake/partial file
                # NETWORK RESILIENCE: these four options are platform-
                # agnostic — applied to every download, not just one
                # provider (hence living here, not in providers.py).
                #   - retries/fragment_retries: lets yt-dlp automatically
                #     retry on transient network/SSL errors (e.g. TLS
                #     record-layer errors like
                #     DECRYPTION_FAILED_OR_BAD_RECORD_MAC).
                #   - http_chunk_size: downloads large videos in ~1MB
                #     chunks instead of one long-lived connection; some
                #     CDNs (YouTube included) can cut off very long single
                #     connections with a 403, chunking can reduce that.
                #   - nocheckcertificate: disables SSL certificate
                #     VALIDATION — note this does NOT address a TLS
                #     record-layer/decryption error like
                #     DECRYPTION_FAILED_OR_BAD_RECORD_MAC (that's unrelated
                #     to certificate validation), so it's unlikely to fix
                #     that specific class of error. Kept as a low-risk
                #     compatibility flag.
                "retries": 10,
                "fragment_retries": 10,
                "http_chunk_size": 1048576,
                "nocheckcertificate": True,
                # EmbedThumbnail needs the thumbnail downloaded to disk to
                # have something to embed (audio formats only).
                "writethumbnail": bool(embed_thumbnail and media_format not in VIDEO_FORMATS),
                # 0/None = unlimited (yt-dlp's own default). speed_limit_kbps
                # is a UI-facing setting (Settings > download speed limit),
                # platform-agnostic like the network resilience options above.
                "ratelimit": (speed_limit_kbps * 1024) if speed_limit_kbps and speed_limit_kbps > 0 else None,
                "writesubtitles": download_subtitles,
                "writeautomaticsub": download_subtitles,
                "subtitleslangs": subtitle_langs or ["en"],
                # YouTube-specific player_client settings are no longer
                # hardcoded here — providers.py:YouTubeProvider injects them
                # via extra_ydl_opts, keeping this function platform-
                # agnostic (previously even TikTok/Instagram downloads got
                # the YouTube-specific setting applied unnecessarily).
            },
            extra_ydl_opts,
        )

        # Audio bitrate downgrading for video formats now goes through
        # yt-dlp's own postprocessor pipeline (at the after_move stage)
        # instead of a manual subprocess call. Since this runs after the
        # file has been moved to its final location and fully released,
        # it avoids the WinError 32 / race-condition issue a manual call
        # would have.
        video_target_abr = None
        if media_format in VIDEO_FORMATS and audio_bitrate:
            video_target_abr = audio_bitrate.lower().strip().replace("bps", "")
            if video_target_abr.endswith("k"):
                video_target_abr = video_target_abr[:-1]

        def _download_action(ydl: "yt_dlp.YoutubeDL") -> Path:
            if video_target_abr:
                ydl.add_post_processor(
                    _AudioBitrateDowngradePP(ydl, target_abr=video_target_abr),
                    when="after_move",
                )
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
            return _resolve_downloaded_filepath(output_path, prepared, media_format, ext_map)

        filepath = _run_with_cookie_fallback(ydl_opts, _download_action)

        if not filepath.exists():
            raise Exception("İndirme tamamlandı ancak dosya bulunamadı.")

        if progress_callback:
            progress_callback(
                ProgressInfo(percent=100.0, speed="—", eta="00:00", status=messages["completed"])
            )

        return filepath
    except DownloadError:
        _cleanup_partial_files(outtmpl)
        raise
    except Exception as exc:
        _cleanup_partial_files(outtmpl)
        raise _map_exception(exc, ydl_opts) from exc


def format_duration_display(seconds: int) -> str:
    """Formats video duration for display in the UI."""
    return _format_duration(seconds)

