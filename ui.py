"""CustomTkinter-based media downloader UI."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image

from config import AppSettings, load_settings, save_settings
from providers import ProviderRegistry
from task_queue import DownloadTask, TaskQueueManager, TaskStatus
from downloader import (
    DownloadError,
    FFmpegNotFoundError,
    InvalidURLError,
    NetworkError,
    PlaylistInfo,
    ProgressInfo,
    VideoInfo,
    format_count,
    format_duration_display,
    format_upload_date,
    load_thumbnail,
)
from i18n import AUDIO_FORMATS, I18n, VIDEO_FORMATS
from settings_dialog import SettingsDialog, open_folder_in_explorer
from history_manager import HistoryEntry, HistoryManager
from history_dialog import HistoryDialog
from batch_dialog import BatchDialog
import update_checker
import sound_notifier

try:
    from tkinterdnd2 import DND_TEXT, TkinterDnD

    _DND_AVAILABLE = True
except ImportError as exc:
    _DND_AVAILABLE = False
    print(f"[ui] Sürükle-bırak desteği kullanılamıyor (tkinterdnd2 kurulu değil, önemsiz): {exc!r}")

try:
    import pystray
    from PIL import ImageDraw

    _TRAY_AVAILABLE = True
except ImportError as exc:
    _TRAY_AVAILABLE = False
    print(f"[ui] Sistem tepsisi desteği kullanılamıyor (pystray kurulu değil, önemsiz): {exc!r}")

# customtkinter'ın CTk'si zaten bir tkinter.Tk alt sınıfı; tkinterdnd2'nin
# kendi TkinterDnD.Tk'siyle DOĞRUDAN çoklu kalıtım MRO çakışmasına yol açar.
# Bilinen çözüm: sadece sürükle-bırak metodlarını (drop_target_register,
# dnd_bind vb.) sağlayan DnDWrapper mixin'ini kullanmak — gerçek bir Tk alt
# sınıfı değil, __init__ içinde TkinterDnD._require(self) çağrılınca CTk'nin
# ÜZERİNE sürükle-bırak desteği ekleniyor. tkinterdnd2 kurulu değilse
# uygulama normal (DnD'siz) çalışmaya devam eder.
_APP_BASE_CLASSES = (ctk.CTk, TkinterDnD.DnDWrapper) if _DND_AVAILABLE else (ctk.CTk,)


def _build_tray_icon_image() -> "Image.Image":
    """Basit, tek dosyalık bir tepsi ikonu üretir (harici bir .ico/.png
    asset'ine bağımlı olmamak için) — mavi zemin üzerinde bir indirme oku."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(31, 83, 141, 255))
    draw.polygon(
        [(20, 18), (44, 18), (44, 34), (52, 34), (32, 52), (12, 34), (20, 34)],
        fill=(255, 255, 255, 255),
    )
    return img


class MediaDownloaderApp(*_APP_BASE_CLASSES):
    """Main window of the media download/conversion application."""

    def __init__(self) -> None:
        super().__init__()

        if _DND_AVAILABLE:
            self.TkdndVersion = TkinterDnD._require(self)

        self.settings = load_settings()
        self.i18n = I18n(self.settings.language)

        ctk.set_appearance_mode(self.settings.theme)
        # IMPORTANT LIMIT: set_default_color_theme() only takes effect HERE,
        # before the first widgets are created — changing it later (from
        # Settings) does NOT update existing widgets (a known CTk
        # limitation). That's why settings_dialog.py shows a "restart"
        # notice when the color theme changes; there's no live-apply
        # mechanism here.
        ctk.set_default_color_theme(self.settings.color_theme)

        self._output_dir = Path(self.settings.output_dir)
        self._current_url = ""
        self._video_info: Optional[VideoInfo] = None
        self._thumbnail_ref: Optional[ctk.CTkImage] = None
        self._empty_thumbnail: Optional[ctk.CTkImage] = None
        self._is_busy = False
        self._settings_window: Optional[SettingsDialog] = None
        self._history_manager = HistoryManager()
        self._history_window: Optional[HistoryDialog] = None

        # --- Playlist support state ---
        self._playlist_info: Optional[PlaylistInfo] = None
        self._selected_indices: set[int] = set()
        self._entry_vars: dict[int, ctk.BooleanVar] = {}
        self._base_geometry = "820x780"
        self._playlist_geometry = "1180x780"

        # --- Playlist row thumbnails (lazy-load) and click-to-preview state ---
        self._playlist_load_token = 0
        self._entry_thumb_labels: dict[int, ctk.CTkLabel] = {}
        self._entry_thumb_images: dict[int, Image.Image] = {}
        self._entry_thumb_refs: dict[int, ctk.CTkImage] = {}
        self._selected_preview_index: Optional[int] = None
        self._entry_detail_token = 0

        # --- Download Queue (Producer-Consumer) ---
        # _on_download / _on_download_playlist no longer call
        # download_media() directly; they build a DownloadTask per video and
        # enqueue() it. Progress/completion notifications flow through
        # _on_task_update (and from there to _handle_single_task_update /
        # _handle_playlist_task_update).
        self._active_tasks: dict[str, DownloadTask] = {}
        self._single_task_id: Optional[str] = None
        self._playlist_batch: Optional[dict] = None
        # STRICT DOUBLE-NOTIFICATION LOCK: once a task_id's COMPLETED/FAILED
        # notification (popup + folder-open) has been processed, its id
        # goes here. _handle_single_task_update unconditionally ignores any
        # further call for the same task_id (regardless of cause — a
        # delayed after(0) callback, a possible double-notify, etc).
        self._notified_task_ids: set[str] = set()
        self._last_status_text: Optional[str] = None
        self._last_status_update_time: float = 0.0

        # --- Download panel (Dashboard) — per-row live tracking ---
        # The data/widget split is deliberate: _playlist_row_data is a cheap
        # dict (can be updated at high frequency), _playlist_row_widgets are
        # the actual CTk widgets (expensive, created LAZILY, synced at a
        # fixed cadence). Detailed rationale in _sync_dashboard_rows.
        self._playlist_row_data: dict[str, dict] = {}
        self._playlist_row_widgets: dict[str, dict] = {}
        self._dirty_row_ids: set[str] = set()
        self._dashboard_sync_job: Optional[str] = None
        # NOTE (fitting the screen): 1040 used to overflow below the
        # taskbar on standard 1080p screens; reduced to 850. Since
        # CTkScrollableFrame already scrolls internally, a growing row
        # count doesn't overflow the panel — it just becomes scrollable
        # within itself.
        self._dashboard_geometry = "820x850"
        # The currently-running after() job for the animation that resizes
        # window width/height gradually rather than instantly — see
        # _smooth_resize. Both the playlist analysis panel's open/close and
        # the download Dashboard's open/close use this.
        self._resize_animation_job: Optional[str] = None

        # --- Custom background image (only visible in margins/edges; all
        # existing panels stay opaque, see _apply_background_image) ---
        self._background_image_source: Optional[Image.Image] = None  # PIL, raw/original
        self._background_ctk_image: Optional[ctk.CTkImage] = None  # last rendered
        self._background_resize_job: Optional[str] = None

        # --- Clipboard Auto-Detect ---
        # See _check_clipboard_for_autofill. While the toggle is on, links
        # copied to the clipboard that ProviderRegistry.is_known_platform()
        # confirms belong to a KNOWN platform (YouTube/TikTok/Instagram/
        # Twitter) are auto-filled and Analyze is triggered (regardless of
        # whether the box is empty/full — see the note in
        # _check_clipboard_for_autofill). _last_seen_clipboard prevents
        # re-filling/re-analyzing the same link every 800ms while it stays
        # on the clipboard (won't re-trigger until the user copies
        # something different).
        self._clipboard_check_job: Optional[str] = None
        self._last_seen_clipboard: str = ""

        # --- Sistem tepsisi (tray) ---
        self._tray_icon: Optional["pystray.Icon"] = None
        self._tray_thread: Optional[threading.Thread] = None

        self._task_queue_manager = TaskQueueManager(
            on_task_update=self._on_task_update,
            dispatch_to_main_thread=lambda fn: self.after(0, fn),
            max_workers=self.settings.concurrent_downloads,
        )
        self._task_queue_manager.start()

        self._build_ui()
        self._apply_settings_to_ui()
        self._refresh_texts()

        # Start the Clipboard Auto-Detect loop ONCE; each turn it checks the
        # toggle's current state and reschedules itself (see
        # _check_clipboard_for_autofill) — it keeps quietly running even
        # while the toggle is off, it just does nothing. Deliberate choice
        # to avoid building a separate start/stop state machine.
        self._clipboard_check_job = self.after(800, self._check_clipboard_for_autofill)

        # Ensures worker threads (even daemon ones) get a clean shutdown
        # signal when the window closes.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Sürükle-bırak: pencerenin HERHANGİ bir yerine link bırakılabilir.
        # DÜZELTME: Önce sadece url_entry'ye (bir CTkEntry — composite bir
        # widget) bağlamıştık; müşteri tarafında hiç tetiklenmedi. customtkinter
        # widget'ları çoğunlukla bir Canvas/Frame üzerine inşa edildiği için
        # tkinterdnd2'nin OLE drag&drop kaydı bazı Windows kurulumlarında bu
        # composite widget'larda güvenilir çalışmıyor. Bunun yerine kök
        # pencereye (self) kaydediyoruz — Tk'nin alt widget'ları (Toplevel
        # olmayanlar) aynı native pencereyi (HWND) paylaşır, yani nereye
        # bırakılırsa bırakılsın yakalanır. tkinterdnd2 kurulu değilse bu
        # adım atlanır — DnD, uygulamanın çalışması için zorunlu değil.
        if _DND_AVAILABLE:
            self.drop_target_register(DND_TEXT)
            self.dnd_bind("<<Drop>>", self._on_url_drop)

        # yt-dlp update check: deliberately delayed a bit after startup
        # (after(1500,...), so it doesn't block the UI's first paint), runs
        # on a background thread — the network request (PyPI) must NEVER
        # block the UI.
        self.after(1500, self._check_for_yt_dlp_update)

    def _check_for_yt_dlp_update(self) -> None:
        def worker() -> None:
            result = update_checker.is_update_available()
            self.after(0, lambda: self._on_update_check_result(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_check_result(self, result: update_checker.UpdateCheckResult) -> None:
        if result.update_available:
            self.update_btn.configure(text=f"{self.i18n.t('update_available')} ({result.latest_version})")
            self.update_btn.grid()
        # If result.error is set (network issue etc.), we show nothing —
        # no need to greet the user with a warning on startup.

    def _on_update_yt_dlp_clicked(self) -> None:
        self.update_btn.configure(state="disabled", text=self.i18n.t("update_updating"))

        def worker() -> None:
            success, log = update_checker.run_update()
            self.after(0, lambda: self._on_update_finished(success, log))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_finished(self, success: bool, log: str) -> None:
        t = self.i18n.t
        if success:
            self.update_btn.grid_remove()
            self._show_notification_dialog(
                t("update_complete_title"),
                t("update_complete_message"),
                is_error=False,
            )
        else:
            self.update_btn.configure(state="normal", text=t("update_available"))
            self._show_notification_dialog(
                t("update_failed_title"),
                t("update_failed_message", log=log[-500:] if log else "(no details)"),
                is_error=True,
            )

    def _open_batch_dialog(self) -> None:
        BatchDialog(parent=self, i18n=self.i18n, on_urls_submitted=self._on_batch_urls_submitted)

    def _on_url_drop(self, event) -> None:
        """URL kutusuna sürükle-bırak ile bırakılan link(ler)i işler.

        DÜZELTME: Tarayıcılar bir linki sürüklerken genelde SADECE URL'yi
        değil, URL'nin ardından sayfa başlığını da ayrı bir satırda
        gönderir (ör. "https://...\\r\\nVideo Başlığı\\r\\n"). Eski kod
        .split() ile TÜM boşluklara göre ayırıyordu — bu yüzden başlıktaki
        her kelime ayrı bir "URL" sanılıp bozuk veri Analiz Et'e gidiyordu.
        Artık sadece satır satır ayrılıyor ve GERÇEKTEN http(s) ile
        başlayan satırlar URL olarak kabul ediliyor; başlık gibi diğer
        satırlar sessizce atılıyor.
        """
        if self._is_busy:
            return
        raw = event.data or ""
        lines = [line.strip().strip("{}").strip() for line in raw.replace("\r", "\n").split("\n")]
        urls = [line for line in lines if line.lower().startswith(("http://", "https://"))]
        if not urls:
            return
        if len(urls) == 1:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, urls[0])
            self._on_analyze()
        else:
            self._on_batch_urls_submitted(urls)

    def _on_batch_urls_submitted(self, urls: list[str]) -> None:
        """Enqueues the URL list from BatchDialog, reusing the existing
        playlist-batch mechanism (see _handle_playlist_task_update/
        _finish_playlist_batch). Each URL becomes an independent
        DownloadTask using the main window's CURRENT format/quality/output-
        folder selection — the SAME kwargs logic as _on_download, not
        duplicated in a second place (see batch_dialog.py's docstring).
        """
        if self._is_busy:
            return  # busy-lock: don't enter if a single/playlist/other batch is already running

        media_format = self.format_menu.get()
        selected_quality = self.quality_menu.get()
        quality_key = self.i18n.quality_key_from_label(selected_quality)
        if media_format in AUDIO_FORMATS:
            audio_bitrate = self._resolve_audio_bitrate(media_format, selected_quality)
        else:
            audio_bitrate = self._resolve_audio_bitrate(media_format, self.audio_quality_menu.get())

        self._single_task_id = None
        self._playlist_batch = {
            "total": len(urls),
            "task_ids": set(),
            "completed": 0,
            "failed": [],
            "cancelled": 0,
            "label": "Toplu indirme",
            "finished_notified": False,
            "processed_ids": set(),
        }

        self._set_busy(True)
        for url in urls:
            task = DownloadTask(
                url=url,
                media_format=media_format,
                quality_key=quality_key,
                output_dir=self._output_dir,
                audio_bitrate=audio_bitrate,
                filename_template=self.settings.filename_template,
                embed_thumbnail=self.settings.embed_thumbnail,
                concurrent_fragments=self.settings.concurrent_fragments,
                status_messages=self._status_messages(),
                display_title=url,  # no analysis step — video info isn't known yet
                cookie_source=self.settings.cookie_source,
                cookie_file_path=self.settings.cookie_file_path or None,
                download_subtitles=getattr(self.settings, "download_subtitles", False),
                subtitle_langs=getattr(self.settings, "subtitle_langs", None),
                speed_limit_kbps=getattr(self.settings, "speed_limit_kbps", 0),
            )
            self._playlist_batch["task_ids"].add(task.task_id)
            self._task_queue_manager.enqueue(task)

    def _on_close(self) -> None:
        if _TRAY_AVAILABLE and getattr(self.settings, "minimize_to_tray", True):
            try:
                self._minimize_to_tray()
                return
            except Exception as exc:
                print(f"[ui] Sistem tepsisine küçültme başarısız, normal kapatmaya düşülüyor: {exc!r}")
        self._shutdown_and_destroy()

    def _shutdown_and_destroy(self) -> None:
        if self._tray_icon is not None:
            self._tray_icon.stop()
            self._tray_icon = None
        if self._dashboard_sync_job is not None:
            self.after_cancel(self._dashboard_sync_job)
            self._dashboard_sync_job = None
        if self._resize_animation_job is not None:
            self.after_cancel(self._resize_animation_job)
            self._resize_animation_job = None
        if self._background_resize_job is not None:
            self.after_cancel(self._background_resize_job)
            self._background_resize_job = None
        if self._clipboard_check_job is not None:
            self.after_cancel(self._clipboard_check_job)
            self._clipboard_check_job = None
        self._task_queue_manager.shutdown()
        sound_notifier.stop()  # a sound tested from the Settings window might still be playing
        self.destroy()

    def _minimize_to_tray(self) -> None:
        """Pencereyi gizleyip sistem tepsisinde bir ikon başlatır.

        pystray.Icon.run() bloklayan bir çağrı olduğundan ayrı bir daemon
        thread'de çalıştırılıyor — Tkinter mainloop'unu asla bloklamaz. Tepsi
        menüsündeki callback'ler (pystray'in kendi thread'inde çalışır) ana
        thread'e self.after(0, ...) ile dispatch ediliyor; task_queue.py'nin
        UI güncellemelerinde kullandığı aynı desen.
        """
        self.withdraw()
        if self._tray_icon is not None:
            return  # zaten çalışıyor
        image = _build_tray_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem(self.i18n.t("tray_show"), self._on_tray_show, default=True),
            pystray.MenuItem(self.i18n.t("tray_exit"), self._on_tray_quit),
        )
        self._tray_icon = pystray.Icon("medya_indirici", image, self.i18n.t("app_title"), menu)
        self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _on_tray_show(self, icon, item) -> None:
        self.after(0, self._restore_from_tray)

    def _restore_from_tray(self) -> None:
        if self._tray_icon is not None:
            self._tray_icon.stop()
            self._tray_icon = None
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_tray_quit(self, icon, item) -> None:
        self.after(0, self._handle_tray_quit_request)

    def _handle_tray_quit_request(self) -> None:
        """Tepsi menüsünden 'Çıkış' seçilince çağrılır — aktif bir indirme
        varsa (tekil ya da bitmemiş bir playlist batch'i) kullanıcıyı
        onaylatmadan uygulamayı KAPATMAZ, yarım kalan indirme sessizce iptal
        olmasın diye."""
        batch_active = bool(self._playlist_batch) and not self._playlist_batch.get("finished_notified", False)
        has_active_download = self._single_task_id is not None or batch_active

        if has_active_download:
            self._restore_from_tray()
            if not self._confirm_dialog(self.i18n.t("app_title"), self.i18n.t("tray_quit_confirm_message")):
                return

        self._shutdown_and_destroy()

    def _confirm_dialog(self, title: str, message: str) -> bool:
        """Basit bir Evet/Hayır onay penceresi — _show_notification_dialog
        ile aynı görsel dil, ama tek OK butonu yerine iki seçenekli."""
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("420x220")
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_columnconfigure(1, weight=1)
        dialog.grid_rowconfigure(0, weight=1)

        ctk.CTkLabel(dialog, text=message, wraplength=380, justify="left").grid(
            row=0, column=0, columnspan=2, padx=20, pady=(20, 12), sticky="nsew"
        )

        result = {"value": False}

        def on_yes() -> None:
            result["value"] = True
            dialog.destroy()

        def on_no() -> None:
            result["value"] = False
            dialog.destroy()

        ctk.CTkButton(dialog, text=self.i18n.t("ok"), command=on_yes).grid(
            row=1, column=0, padx=(20, 8), pady=(0, 20), sticky="ew"
        )
        ctk.CTkButton(
            dialog, text=self.i18n.t("cancel"), fg_color="transparent", border_width=1, command=on_no
        ).grid(row=1, column=1, padx=(8, 20), pady=(0, 20), sticky="ew")

        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

        self.wait_window(dialog)
        return result["value"]

    def _on_task_update(self, task: DownloadTask) -> None:
        """Handles status updates from TaskQueueManager (runs on the main thread).

        Routes the task to the right handler depending on whether it
        belongs to the currently-tracked playlist batch or a single
        download. If it belongs to neither (e.g. a delayed update arriving
        after the user switched screens), it's silently ignored — the UI
        state no longer cares about it.
        """
        self._active_tasks[task.task_id] = task

        if self._playlist_batch and task.task_id in self._playlist_batch["task_ids"]:
            self._handle_playlist_task_update(task)
            return

        if self._single_task_id is not None and task.task_id == self._single_task_id:
            self._handle_single_task_update(task)
            return

    def _on_cancel_single(self) -> None:
        if self._single_task_id:
            self._task_queue_manager.cancel(self._single_task_id)

    def _on_toggle_pause_single(self) -> None:
        if not self._single_task_id:
            return
        task = self._task_queue_manager.get_task(self._single_task_id)
        if task is None:
            return
        if task.status == TaskStatus.PAUSED:
            self._task_queue_manager.resume(self._single_task_id)
        elif task.status == TaskStatus.RUNNING:
            self._task_queue_manager.pause(self._single_task_id)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # IMPORTANT (ordering): this label is created FIRST (i.e. lowest in
        # the stacking order) using `place()` to cover the ENTIRE window.
        # Every other widget is added with `grid()` on the same `self` —
        # place and grid can coexist on the same parent. Since all panels
        # are OPAQUE, the image only shows in margins/edges, never behind
        # widgets — a deliberate choice for readability.
        self.background_label = ctk.CTkLabel(self, text="", image=None)
        self.background_label.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._apply_background_image(self.settings.background_image_path)
        self.bind("<Configure>", self._on_window_configure)

        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(15, 5))
        top_bar.grid_columnconfigure(0, weight=1)

        self.header_label = ctk.CTkLabel(
            top_bar,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            anchor="w",
        )
        self.header_label.grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.update_btn = ctk.CTkButton(
            top_bar,
            text=self.i18n.t("update_available"),
            height=36,
            fg_color="#B8860B",
            hover_color="#8B6508",
            command=self._on_update_yt_dlp_clicked,
        )
        self.update_btn.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.update_btn.grid_remove()  # hidden until an update is detected

        self.batch_btn = ctk.CTkButton(
            top_bar,
            text="📋",
            width=40,
            height=36,
            font=ctk.CTkFont(size=18),
            command=self._open_batch_dialog,
        )
        self.batch_btn.grid(row=0, column=2, sticky="e", padx=(10, 0))

        self.history_btn = ctk.CTkButton(
            top_bar,
            text="📜",
            width=40,
            height=36,
            font=ctk.CTkFont(size=18),
            command=self._open_history,
        )
        self.history_btn.grid(row=0, column=3, sticky="e", padx=(10, 0))

        self.settings_btn = ctk.CTkButton(
            top_bar,
            text="⚙",
            width=40,
            height=36,
            font=ctk.CTkFont(size=18),
            command=self._open_settings,
        )
        self.settings_btn.grid(row=0, column=4, sticky="e", padx=(10, 0))

        self.subtitle_label = ctk.CTkLabel(
            top_bar,
            text="",
            font=ctk.CTkFont(size=13),
            text_color=("gray40", "gray60"),
            anchor="w",
        )
        self.subtitle_label.grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 10))

        url_frame = ctk.CTkFrame(self)
        url_frame.grid(row=1, column=0, padx=24, pady=16, sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            url_frame,
            placeholder_text="",
            height=42,
            font=ctk.CTkFont(size=14),
        )
        self.url_entry.grid(row=0, column=0, padx=(16, 8), pady=16, sticky="ew")

        self.analyze_btn = ctk.CTkButton(
            url_frame,
            text="",
            width=120,
            height=42,
            command=self._on_analyze,
        )
        self.analyze_btn.grid(row=0, column=1, padx=(0, 16), pady=16)

        # Clipboard Auto-Detect toggle — see _clipboard_check_job and
        # _check_clipboard_for_autofill in __init__.
        self.clipboard_autodetect_var = ctk.BooleanVar(value=False)
        self.clipboard_autodetect_switch = ctk.CTkSwitch(
            url_frame,
            text=self.i18n.t("clipboard_autodetect_label"),
            variable=self.clipboard_autodetect_var,
            onvalue=True,
            offvalue=False,
            command=self._on_clipboard_autodetect_toggle,
        )
        self.clipboard_autodetect_switch.grid(row=0, column=2, padx=(0, 16), pady=16)

        info_frame = ctk.CTkFrame(self)
        info_frame.grid(row=2, column=0, padx=24, pady=(0, 12), sticky="nsew")
        info_frame.grid_columnconfigure(1, weight=1)
        info_frame.grid_columnconfigure(2, weight=1)

        self.thumbnail_label = ctk.CTkLabel(
            info_frame,
            text="",
            width=240,
            height=135,
            fg_color=("gray85", "gray25"),
            corner_radius=8,
        )
        self.thumbnail_label.grid(row=0, column=0, rowspan=2, padx=16, pady=16, sticky="nw")

        meta_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
        meta_frame.grid(row=0, column=1, padx=(0, 16), pady=(16, 8), sticky="nsew")
        meta_frame.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            meta_frame,
            text="",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
            wraplength=400,
            justify="left",
        )
        self.title_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.meta_labels: dict[str, ctk.CTkLabel] = {}
        meta_keys = ("platform", "channel", "duration", "upload_date", "views", "likes", "dislikes")
        for idx, key in enumerate(meta_keys, start=1):
            label = ctk.CTkLabel(
                meta_frame,
                text="",
                font=ctk.CTkFont(size=13),
                anchor="w",
                text_color=("gray30", "gray70"),
            )
            label.grid(row=idx, column=0, sticky="w", pady=2)
            self.meta_labels[key] = label

        # --- Playlist panel (checkbox list) ---
        # Only shown with grid() once a playlist has been analyzed; stays
        # hidden with grid_remove() for the single-video flow, doesn't
        # disturb the page layout.
        self.playlist_panel = ctk.CTkFrame(info_frame, fg_color="transparent")
        self.playlist_panel.grid_columnconfigure(0, weight=1)
        self.playlist_panel.grid_rowconfigure(2, weight=1)

        self.playlist_header_label = ctk.CTkLabel(
            self.playlist_panel,
            text="",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            wraplength=260,
            justify="left",
        )
        self.playlist_header_label.grid(row=0, column=0, sticky="w", pady=(16, 4))

        self.playlist_select_btn = ctk.CTkButton(
            self.playlist_panel,
            text="",
            width=150,
            command=self._on_toggle_select_all,
        )
        self.playlist_select_btn.grid(row=1, column=0, sticky="w", pady=(0, 8))

        self.playlist_scroll = ctk.CTkScrollableFrame(
            self.playlist_panel,
            width=260,
            height=220,
        )
        self.playlist_scroll.grid(row=2, column=0, sticky="nsew", pady=(0, 16))
        self.playlist_scroll.grid_columnconfigure(0, weight=1)

        options_frame = ctk.CTkFrame(self)
        options_frame.grid(row=3, column=0, padx=24, pady=8, sticky="ew")
        options_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.format_header = ctk.CTkLabel(options_frame, text="")
        self.format_header.grid(row=0, column=0, padx=8, pady=(12, 4))
        
        self.audio_quality_header = ctk.CTkLabel(options_frame, text=self.i18n.t("audio_quality_header"))
        self.audio_quality_header.grid(row=0, column=1, padx=8, pady=(12, 4))
        
        self.quality_header = ctk.CTkLabel(options_frame, text="")
        self.quality_header.grid(row=0, column=2, padx=8, pady=(12, 4))
        
        self.folder_header = ctk.CTkLabel(options_frame, text="")
        self.folder_header.grid(row=0, column=3, padx=8, pady=(12, 4))

        self.format_menu = ctk.CTkOptionMenu(
            options_frame,
            values=VIDEO_FORMATS + AUDIO_FORMATS,
            command=self._on_format_change,
        )
        self.format_menu.grid(row=1, column=0, padx=8, pady=(0, 16), sticky="ew")

        self.audio_quality_menu = ctk.CTkOptionMenu(
            options_frame,
            values=["En Yüksek", "320kbps", "256kbps", "192kbps", "160kbps", "128kbps", "64kbps", "32kbps"],
        )
        self.audio_quality_menu.grid(row=1, column=1, padx=8, pady=(0, 16), sticky="ew")
        self.audio_quality_menu.set("En Yüksek")

        self.quality_menu = ctk.CTkOptionMenu(
            options_frame,
            values=self.i18n.video_quality_options(),
        )
        self.quality_menu.grid(row=1, column=2, padx=8, pady=(0, 16), sticky="ew")

        folder_frame = ctk.CTkFrame(options_frame, fg_color="transparent")
        folder_frame.grid(row=1, column=3, padx=8, pady=(0, 16), sticky="ew")
        folder_frame.grid_columnconfigure(0, weight=1)

        self.folder_label = ctk.CTkLabel(
            folder_frame,
            text=self._truncate_path(str(self._output_dir)),
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self.folder_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.folder_btn = ctk.CTkButton(
            folder_frame,
            text="",
            width=70,
            command=self._on_select_folder,
        )
        self.folder_btn.grid(row=0, column=1)

        self.download_btn = ctk.CTkButton(
            self,
            text="",
            height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._on_download,
        )
        self.download_btn.grid(row=4, column=0, padx=24, pady=8, sticky="ew")

        self.progress_frame = ctk.CTkFrame(self)
        self.progress_frame.grid(row=5, column=0, padx=24, pady=(8, 20), sticky="ew")
        self.progress_frame.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            self.progress_frame,
            text="",
            font=ctk.CTkFont(size=13),
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, padx=16, pady=(12, 4), sticky="ew")

        # İptal / Duraklat-Devam butonları — sadece bir indirme aktifken
        # anlamlı, bu yüzden varsayılan gizli (_set_busy açıp kapatıyor).
        control_frame = ctk.CTkFrame(self.progress_frame, fg_color="transparent")
        control_frame.grid(row=0, column=1, padx=(0, 16), pady=(12, 4), sticky="e")

        self.pause_btn = ctk.CTkButton(
            control_frame,
            text="⏸️",
            width=32,
            height=24,
            font=ctk.CTkFont(size=12),
            command=self._on_toggle_pause_single,
        )
        self.pause_btn.grid(row=0, column=0, padx=(0, 4))

        self.cancel_btn = ctk.CTkButton(
            control_frame,
            text="❌",
            width=32,
            height=24,
            font=ctk.CTkFont(size=12),
            fg_color="transparent",
            border_width=1,
            command=self._on_cancel_single,
        )
        self.cancel_btn.grid(row=0, column=1)
        control_frame.grid_remove()
        self._single_control_frame = control_frame

        self.progress_bar = ctk.CTkProgressBar(self.progress_frame)
        self.progress_bar.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
        self.progress_bar.set(0)

        stats_frame = ctk.CTkFrame(self.progress_frame, fg_color="transparent")
        stats_frame.grid(row=2, column=0, padx=16, pady=(4, 12), sticky="ew")
        stats_frame.grid_columnconfigure((0, 1), weight=1)

        self.speed_label = ctk.CTkLabel(
            stats_frame,
            text="",
            font=ctk.CTkFont(size=12),
            anchor="w",
        )
        self.speed_label.grid(row=0, column=0, sticky="w")

        self.eta_label = ctk.CTkLabel(
            stats_frame,
            text="",
            font=ctk.CTkFont(size=12),
            anchor="e",
        )
        self.eta_label.grid(row=0, column=1, sticky="e")

        # --- Download panel (Dashboard) ---
        # The macro summary (status_label/progress_bar above) is shown
        # alongside the micro per-row detail (this panel). Fully hidden
        # with grid_remove() before a playlist download starts; the window
        # doesn't reserve space for it.
        self.dashboard_frame = ctk.CTkScrollableFrame(self, height=220)
        self.dashboard_frame.grid(row=6, column=0, padx=24, pady=(0, 16), sticky="nsew")
        self.dashboard_frame.grid_columnconfigure(0, weight=1)
        self.dashboard_frame.grid_remove()

        self.geometry(self._base_geometry)
        self.minsize(720, 700)

    def _apply_settings_to_ui(self) -> None:
        self._output_dir = Path(self.settings.output_dir)
        self.folder_label.configure(text=self._truncate_path(str(self._output_dir)))
        self.format_menu.set(self.settings.default_format)
        self._on_format_change(self.settings.default_format)
        self.quality_menu.set(
            self.i18n.quality_label_from_key(self.settings.default_quality_key)
        )
        self.clipboard_autodetect_var.set(self.settings.clipboard_autodetect)

    def _on_clipboard_autodetect_toggle(self) -> None:
        """Called when the Clipboard Auto-Detect switch changes.

        NOTE: this is the first place in ui.py that calls save_settings()
        DIRECTLY — until now, persistent settings were only written from
        settings_dialog.py's "Save" flow. Deliberate choice: this is a
        toggle switch, not a form field waiting for "Save" — the user
        expects it to persist the moment they flip it (settings_dialog.py
        wasn't touched at all, that flow is unchanged).
        """
        self.settings.clipboard_autodetect = self.clipboard_autodetect_var.get()
        save_settings(self.settings)

    def _check_clipboard_for_autofill(self) -> None:
        """Clipboard Auto-Detect — see the intro note in __init__.

        Reschedules itself with self.after() every ~800ms (first call in
        __init__, cancelled in _on_close). Keeps running even while the
        toggle is OFF, but does nothing in that case — a deliberate, cheap
        choice to avoid building a separate start/stop mechanism (a boolean
        check every 800ms has no measurable cost).

        Rule: if the clipboard link is DIFFERENT from _last_seen_clipboard
        and belongs to a KNOWN platform, the box is cleared and filled with
        the new link and analysis is triggered again, regardless of whether
        the box is currently full/empty. Dedup is still handled by the same
        (_last_seen_clipboard) mechanism — the same link sitting on the
        clipboard doesn't re-trigger repeatedly; it only fires when
        something GENUINELY NEW is copied to the clipboard.
        """
        if self.clipboard_autodetect_var.get():
            try:
                clipboard_text = self.clipboard_get().strip()
            except Exception:
                # Clipboard is empty, has no text (e.g. an image was
                # copied), or OS access failed right now — silently skip
                # this turn.
                clipboard_text = ""

            if clipboard_text and clipboard_text != self._last_seen_clipboard:
                self._last_seen_clipboard = clipboard_text
                # is_known_platform: only YouTube/TikTok/Instagram/Twitter
                # — unrelated text copied to the clipboard, or e.g. an
                # Amazon link, doesn't leak into the box (see providers.py).
                if ProviderRegistry.is_known_platform(clipboard_text):
                    self.url_entry.delete(0, "end")
                    self.url_entry.insert(0, clipboard_text)
                    self._on_analyze()

        self._clipboard_check_job = self.after(800, self._check_clipboard_for_autofill)

    def _refresh_texts(self) -> None:
        t = self.i18n.t
        self.title(t("app_title"))
        self.header_label.configure(text=t("header"))
        self.subtitle_label.configure(text=t("subtitle"))
        self.url_entry.configure(placeholder_text=t("url_placeholder"))
        self.analyze_btn.configure(text=t("analyze"))
        self.format_header.configure(text=t("format"))
        self.audio_quality_header.configure(text=t("audio_quality_header"))
        self.quality_header.configure(text=t("quality"))
        self.folder_header.configure(text=t("output_folder"))
        self.folder_btn.configure(text=t("select"))
        self.download_btn.configure(text=t("start_download"))
        self.status_label.configure(text=t("ready"))
        self.speed_label.configure(text=f"{t('speed')}: —")
        self.eta_label.configure(text=f"{t('remaining')}: —")
        self.settings_btn.configure(text=t("settings"))

        if not self._video_info:
            self.thumbnail_label.configure(text=t("preview"))
            self.title_label.configure(text=f"{t('title')}: —")
            for key in self.meta_labels:
                self.meta_labels[key].configure(text=f"{t(key)}: —")

    @staticmethod
    def _truncate_path(path: str, max_len: int = 36) -> str:
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3) :]

    @staticmethod
    def _truncate_title(title: str, max_len: int = 52) -> str:
        """Truncates video titles keeping the START (unlike paths, the end is cut off)."""
        if len(title) <= max_len:
            return title
        return title[: max_len - 3] + "..."

    def _resolve_audio_bitrate(self, media_format: str, audio_selection_label: str) -> Optional[str]:
        """Converts the selected quality label into the audio_bitrate value
        downloader.py understands.
        """
        label = audio_selection_label.strip()
        if label in ("En İyi Ses", "En Yüksek"):
            return "best"
        if media_format in AUDIO_FORMATS:
            return label.lower().replace("kbps", "").strip() + "k"
        return label.lower().strip()

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        state = "disabled" if busy else "normal"
        self.analyze_btn.configure(state=state)
        self.download_btn.configure(state=state)
        self.folder_btn.configure(state=state)

    def _on_format_change(self, choice: str) -> None:
        if choice in AUDIO_FORMATS:
            self.audio_quality_menu.grid_remove()
            self.audio_quality_header.grid_remove()
        else:
            self.audio_quality_menu.grid()
            self.audio_quality_header.grid()
            self.audio_quality_menu.configure(values=["En Yüksek", "320kbps", "256kbps", "192kbps", "160kbps", "128kbps", "64kbps", "32kbps"])

        current_key = self.i18n.quality_key_from_label(self.quality_menu.get())

        if choice in AUDIO_FORMATS:
            options = ["En İyi Ses", "320 kbps", "256 kbps", "192 kbps", "128 kbps", "64 kbps", "32 kbps"]
            self.quality_menu.configure(values=options)
            self.quality_menu.set("320 kbps")
        else:
            options = self.i18n.video_quality_options()
            self.quality_menu.configure(values=options)
            default = (
                self.i18n.quality_label_from_key(current_key)
                if current_key.startswith("quality_")
                else self.i18n.t("quality_best")
            )
            self.quality_menu.set(default if default in options else options[0])

    def _open_settings(self) -> None:
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.focus()
            return

        self._settings_window = SettingsDialog(
            parent=self,
            settings=self.settings,
            i18n=self.i18n,
            on_save=self._on_settings_saved,
        )

    def _open_history(self) -> None:
        if self._history_window and self._history_window.winfo_exists():
            self._history_window.focus()
            return

        self._history_window = HistoryDialog(
            parent=self,
            history_manager=self._history_manager,
            i18n=self.i18n,
        )

    def _on_settings_saved(self, settings: AppSettings) -> None:
        lang_changed = settings.language != self.settings.language
        background_changed = settings.background_image_path != self.settings.background_image_path
        self.settings = settings
        self.i18n = I18n(settings.language)

        ctk.set_appearance_mode(settings.theme)
        self._apply_settings_to_ui()
        self._refresh_texts()

        if background_changed:
            self._apply_background_image(settings.background_image_path)

        self._task_queue_manager.resize_workers(settings.concurrent_downloads)

        if lang_changed and self._video_info:
            self._update_video_info(self._video_info)

    def _on_select_folder(self) -> None:
        folder = filedialog.askdirectory(
            title=self.i18n.t("select_folder_title"),
            initialdir=str(self._output_dir),
        )
        if folder:
            self._output_dir = Path(folder)
            self.folder_label.configure(text=self._truncate_path(folder))

    def _show_error(self, title: str, message: str) -> None:
        self._show_notification_dialog(title, message, is_error=True)

    def _show_success(self, title: str, message: str) -> None:
        self._show_notification_dialog(title, message, is_error=False)

    def _show_notification_dialog(self, title: str, message: str, is_error: bool) -> None:
        """Custom Toplevel used instead of messagebox.showinfo/showerror.

        Windows' native MessageBox API (which tkinter.messagebox wraps)
        automatically plays its own system sound (Asterisk/Hand) based on
        the info/error icon — even if our code never calls it explicitly.
        tkinter.messagebox offers no way to suppress that. The only fix is
        to avoid the native dialog entirely and draw our own (silent) popup,
        so sound is now controlled solely through sound_notifications.

        Blocks like messagebox.showinfo() does (via self.wait_window) —
        the existing "show popup first, open folder on OK" ordering (see
        _handle_single_task_update/_finish_playlist_batch) depends on this
        blocking behavior being preserved.

        İstisna: pencere sistem tepsisine küçültülmüşken (self._tray_icon
        dolu) bir Toplevel açmak anlamsız — kullanıcı görmüyor. Bu durumda
        pystray'in native bildirimine (icon.notify) düşülüyor; başarısız
        olursa (bazı Linux DE'lerinde bildirim servisi olmayabilir) pencere
        geri getirilip normal diyalog gösteriliyor.
        """
        if self._tray_icon is not None:
            try:
                self._tray_icon.notify(message, title)
                return
            except Exception as exc:
                print(f"[ui] Tepsi bildirimi gösterilemedi, pencere geri getiriliyor: {exc!r}")
                self._restore_from_tray()

        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("420x260")
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        icon = "❌" if is_error else "✅"
        ctk.CTkLabel(dialog, text=icon, font=ctk.CTkFont(size=32)).grid(
            row=0, column=0, padx=20, pady=(16, 4)
        )

        text_box = ctk.CTkTextbox(dialog, wrap="word")
        text_box.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 12))
        text_box.insert("1.0", message)
        text_box.configure(state="disabled")

        ctk.CTkButton(dialog, text=self.i18n.t("ok"), width=100, command=dialog.destroy).grid(
            row=2, column=0, pady=(0, 16)
        )

        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

        self.wait_window(dialog)

    def _status_messages(self) -> dict[str, str]:
        t = self.i18n.t
        return {
            "downloading": t("downloading"),
            "converting": t("converting"),
            "completed": t("completed"),
        }

    def _run_in_thread(self, target, on_success=None) -> None:
        def worker() -> None:
            try:
                result = target()
                if on_success:
                    self.after(0, lambda r=result: on_success(r))
            except FFmpegNotFoundError as exc:
                err_msg = str(exc)
                self.after(0, lambda: self._show_error(self.i18n.t("ffmpeg_error"), err_msg))
            except InvalidURLError as exc:
                err_msg = str(exc)
                self.after(0, lambda: self._show_error(self.i18n.t("invalid_link"), err_msg))
            except NetworkError as exc:
                err_msg = str(exc)
                self.after(0, lambda: self._show_error(self.i18n.t("network_error"), err_msg))
            except DownloadError as exc:
                err_msg = str(exc)
                self.after(0, lambda: self._show_error(self.i18n.t("download_error"), err_msg))
            except Exception as exc:
                err_msg = str(exc)
                self.after(0, lambda: self._show_error(self.i18n.t("download_error"), err_msg))
            finally:
                self.after(0, lambda: self._set_busy(False))

        if self._is_busy:
            return

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()


    def _get_empty_thumbnail(self) -> ctk.CTkImage:
        """'Resim yok' demek yerine her zaman gerçek (1x1 şeffaf) bir görsel döndürür.

        Kök neden: Tkinter, configure(image=None) çağrıldığında önceki
        PhotoImage referansını serbest bırakmaya çalışıyor; o referans zaten
        garbage collector tarafından geçersiz kılınmışsa "pyimage... doesn't
        exist" TclError'ı fırlatıyor. Bunun yerine hep GEÇERLİ (boş ama gerçek)
        bir görsele geçiş yaparak bu kod yolunu tamamen bypass ediyoruz. Görsel
        self üzerinde kalıcı olarak tutulur (tek sefer oluşturulur), böylece
        kendisi de asla GC'lenmez.
        """
        if self._empty_thumbnail is None:
            blank = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            self._empty_thumbnail = ctk.CTkImage(light_image=blank, dark_image=blank, size=(1, 1))
        return self._empty_thumbnail

    def _set_main_thumbnail(self, image: Optional[Image.Image], placeholder_text: Optional[str] = None) -> None:
        """Sol taraftaki büyük önizleme kutusunu günceller.

        _update_video_info, _update_playlist_info (ana kapak) ve playlist
        satırına tıklama (_on_entry_click) arasında paylaşılan ortak mantık.
        """
        t = self.i18n.t
        if image:
            img = image.copy()
            img.thumbnail((240, 135), Image.Resampling.LANCZOS)
            self._thumbnail_ref = ctk.CTkImage(
                light_image=img,
                dark_image=img,
                size=(240, 135),
            )
            self.thumbnail_label.configure(image=self._thumbnail_ref, text="")
            # Reference pinning: görseli widget'ın kendi attribute'una da bağlıyoruz.
            self.thumbnail_label.image = self._thumbnail_ref
        else:
            # DİKKAT: configure(image=None) YERİNE her zaman geçerli boş bir
            # görsel kullanıyoruz (bkz. _get_empty_thumbnail docstring'i).
            empty = self._get_empty_thumbnail()
            self._thumbnail_ref = empty
            self.thumbnail_label.image = empty
            self.thumbnail_label.configure(image=empty, text=placeholder_text or t("no_preview"))

    def _update_video_info(self, info: VideoInfo) -> None:
        self._video_info = info
        t = self.i18n.t

        self.title_label.configure(text=f"{t('title')}: {info.title}")
        self.meta_labels["platform"].configure(text=f"{t('platform')}: {info.platform}")
        self.meta_labels["channel"].configure(text=f"{t('channel')}: {info.channel}")
        self.meta_labels["duration"].configure(
            text=f"{t('duration')}: {format_duration_display(info.duration)}"
        )
        self.meta_labels["upload_date"].configure(
            text=f"{t('upload_date')}: {format_upload_date(info.upload_date_raw, self.settings.language)}"
        )
        self.meta_labels["views"].configure(
            text=f"{t('views')}: {format_count(info.view_count)}"
        )
        self.meta_labels["likes"].configure(
            text=f"{t('likes')}: {format_count(info.like_count)}"
        )

        dislike_text = format_count(info.dislike_count)
        if info.dislike_count is None and info.platform == "YouTube":
            dislike_text = t("hidden")
        self.meta_labels["dislikes"].configure(
            text=f"{t('dislikes')}: {dislike_text}"
        )

        self._set_main_thumbnail(info.thumbnail_image)

        if info.error:
            # Bu VideoInfo bir hata/erişilemezlik durumunu temsil ediyor
            # (örn. gizli/silinmiş video). "analysis_done" yerine net bir
            # durum mesajı gösteriyoruz; analyze_url'i tekrar çağırmıyoruz.
            self.status_label.configure(text=f"Video kullanılamıyor: {info.error}")
        else:
            self.status_label.configure(text=t("analysis_done"))
        self.progress_bar.set(0)
        self.speed_label.configure(text=f"{t('speed')}: —")
        self.eta_label.configure(text=f"{t('remaining')}: —")

    def _show_playlist_panel(self) -> None:
        """Playlist panelini gösterir.

        DÜZELTME (bkz. konuşma — büyük playlist'lerde pencere animasyonu
        "glitchleniyordu"): Panel artık animasyon SIRASINDA değil, animasyon
        TAMAMEN BİTİNCE (on_complete) gridleniyor. Eskiden panel animasyon
        başlamadan ÖNCE grid()'leniyordu — bu, yüzlerce satırlı bir
        playlist'te her animasyon adımının (10 kez, ~15ms arayla) TÜM o
        satırları yeniden yerleştirmeye çalışmasına, dolayısıyla pencerenin
        "titremesine" ve son adımda hedef genişliğe tam ulaşamamasına
        (etiketlerin kırpılması) yol açıyordu. Artık pencere ÖNCE (boş/dar
        haliyle) hedef genişliğe büyüyor, panel İÇERİĞİ sadece bir kez, en
        sonda yerleşiyor.
        """
        width, height = (int(v) for v in self._playlist_geometry.split("x"))

        def reveal_panel() -> None:
            self.playlist_panel.grid(row=0, column=2, rowspan=2, padx=(0, 16), pady=0, sticky="nsew")

        self._smooth_resize(width, height, on_complete=reveal_panel)

    def _hide_playlist_panel(self) -> None:
        self.playlist_panel.grid_remove()
        width, height = (int(v) for v in self._base_geometry.split("x"))
        self._smooth_resize(width, height)

    def _sync_select_all_button_text(self) -> None:
        if not self._playlist_info:
            return
        # NOT: Bu metinler ileride i18n.py'a taşınabilir; şimdilik projedeki
        # mevcut kısmen-hardcode paterniyle (örn. "Ses Kalitesi") tutarlı.
        if len(self._selected_indices) >= len(self._playlist_info.entries):
            self.playlist_select_btn.configure(text=self.i18n.t("deselect_all"))
        else:
            self.playlist_select_btn.configure(text=self.i18n.t("select_all"))

    def _on_entry_toggle(self, idx: int) -> None:
        var = self._entry_vars.get(idx)
        if var is None:
            return
        if var.get():
            self._selected_indices.add(idx)
        else:
            self._selected_indices.discard(idx)
        self._sync_select_all_button_text()

    def _on_toggle_select_all(self) -> None:
        if not self._playlist_info:
            return

        select_all = len(self._selected_indices) < len(self._playlist_info.entries)
        for idx, var in self._entry_vars.items():
            var.set(select_all)

        if select_all:
            self._selected_indices = {entry.index for entry in self._playlist_info.entries}
        else:
            self._selected_indices.clear()

        self._sync_select_all_button_text()

    def _update_playlist_info(self, playlist: PlaylistInfo) -> None:
        """Analiz sonucu bir PlaylistInfo geldiğinde checkbox panelini kurar."""
        self._playlist_info = playlist
        self._video_info = None
        self._selected_indices = {entry.index for entry in playlist.entries}
        self._entry_vars = {}
        self._entry_thumb_labels = {}
        self._entry_thumb_images = {}
        self._entry_thumb_refs = {}
        self._selected_preview_index = None

        # Önceki analizden kalan satırları temizle (yeniden analiz durumunda çakışmasın)
        for widget in self.playlist_scroll.winfo_children():
            widget.destroy()

        t = self.i18n.t
        self.playlist_header_label.configure(
            text=f"{t('title')}: {playlist.title}\n({playlist.entry_count} video)"
        )

        for entry in playlist.entries:
            var = ctk.BooleanVar(value=True)
            self._entry_vars[entry.index] = var

            row_frame = ctk.CTkFrame(self.playlist_scroll, fg_color="transparent")
            row_frame.grid(row=entry.index - 1, column=0, sticky="ew", pady=2)
            row_frame.grid_columnconfigure(2, weight=1)

            checkbox = ctk.CTkCheckBox(
                row_frame,
                text="",
                width=20,
                variable=var,
                command=lambda idx=entry.index: self._on_entry_toggle(idx),
            )
            checkbox.grid(row=0, column=0, padx=(0, 6), sticky="w")

            # 32x32 küçük thumbnail ikonu — başlangıçta gri placeholder,
            # gerçek görsel arka planda (lazy) yüklenince yerine geçecek.
            thumb_label = ctk.CTkLabel(
                row_frame,
                text="",
                width=32,
                height=32,
                fg_color=("gray85", "gray25"),
                corner_radius=4,
                cursor="hand2",
            )
            thumb_label.grid(row=0, column=1, padx=(0, 6), sticky="w")
            self._entry_thumb_labels[entry.index] = thumb_label

            duration_text = format_duration_display(entry.duration) if entry.duration else "—"
            entry_label = ctk.CTkLabel(
                row_frame,
                text=f"{entry.index}. {entry.title}  ({duration_text})",
                anchor="w",
                justify="left",
                wraplength=160,
                font=ctk.CTkFont(size=12),
                cursor="hand2",
            )
            entry_label.grid(row=0, column=2, sticky="w")

            # Satıra (thumbnail veya başlık üzerine) tıklanınca sol panelde önizleme
            for widget in (row_frame, thumb_label, entry_label):
                widget.bind("<Button-1>", lambda _event, e=entry: self._on_entry_click(e))

        self._sync_select_all_button_text()

        # DÜZELTME (pencere animasyonu "glitchleniyor" — bkz. konuşma):
        # Yukarıdaki döngü, playlist'teki HER video için yeni widget'lar
        # (checkbox+thumbnail+label) oluşturuyor. Tk bunların layout'unu
        # HEMEN değil, event loop boşa çıkınca ("idle" anında) hesaplıyor.
        # _show_playlist_panel() hemen ardından pencereyi 10 adımda
        # animasyonla büyütmeye başlayınca, Tk aynı anda hem onlarca yeni
        # widget'ın yerleşimini hesaplamaya hem de her ~15ms'de bir yeni bir
        # geometry()'yi işlemeye çalışıyor — ikisi çakışınca pencere
        # "titreyip" hedefe tam ulaşamadan kalabiliyor (etiketlerin
        # kırpılması bunun belirtisiydi). update_idletasks() burada Tk'yi
        # ZORLA "yakalıyor" — tüm yeni widget'ların layout'u animasyon
        # başlamadan ÖNCE tamamen hesaplanmış oluyor, animasyon sadece dış
        # pencere boyutuyla uğraşıyor.
        self.update_idletasks()
        self._show_playlist_panel()

        # Tekil video meta alanlarını playlist bağlamına uygun şekilde sıfırla
        self.title_label.configure(text=f"{t('title')}: {playlist.title}")
        for key in self.meta_labels:
            self.meta_labels[key].configure(text=f"{t(key)}: —")
        self.meta_labels["channel"].configure(text=f"{t('channel')}: {playlist.uploader}")

        # Ana kapak: playlist analizinde downloader.py tarafından zaten
        # fallback zinciriyle (playlist kapağı yoksa ilk video) çözülmüş halde geliyor.
        self._set_main_thumbnail(playlist.thumbnail_image)

        self.status_label.configure(text=t("analysis_done"))
        self.progress_bar.set(0)
        self.speed_label.configure(text=f"{t('speed')}: —")
        self.eta_label.configure(text=f"{t('remaining')}: —")

        # Satır thumbnail'lerini UI'ı dondurmadan arka planda kademeli yükle
        self._load_playlist_thumbnails_async(playlist)

    def _load_playlist_thumbnails_async(self, playlist: PlaylistInfo) -> None:
        """Playlist satırlarındaki 32x32 ikonları arka plan thread'inde tembel yükler.

        Her entry'nin thumbnail'i ayrı ayrı indirilip hazır oldukça UI'a
        `self.after(0, ...)` ile işlenir; böylece uygulama donmaz, ikonlar
        teker teker 'belirir gibi' güncellenir. Kullanıcı bu bitmeden yeni
        bir analiz başlatırsa token karşılaştırmasıyla eski sonuçlar atlanır.

        Her istek arasına kısa bir bekleme (THUMBNAIL_FETCH_DELAY) konur;
        büyük playlist'lerde platforma art arda çok hızlı istek atıp
        rate-limit'e takılmayı önlemek için.
        """
        THUMBNAIL_FETCH_DELAY = 0.15  # saniye

        self._playlist_load_token += 1
        token = self._playlist_load_token

        def worker() -> None:
            for entry in playlist.entries:
                if token != self._playlist_load_token:
                    return  # Kullanıcı yeni bir analiz başlattı, bu iş artık geçersiz
                if not entry.thumbnail_url:
                    continue

                image = load_thumbnail(entry.thumbnail_url)
                if image is not None:
                    # Belleği verimli kullanmak için önbelleğe TAM boyutlu görseli değil,
                    # önizleme için yeterli küçük bir kopyayı koyuyoruz. Orijinal 'image'
                    # bu fonksiyon dönünce referanssız kalır ve CPython'ın refcounting
                    # GC'si tarafından hemen serbest bırakılır (ekstra gc.collect() çağrısı
                    # gerekmiyor, hatta performansı gereksiz yere düşürür).
                    image.thumbnail((320, 180), Image.Resampling.LANCZOS)
                    self.after(0, lambda e=entry, img=image, tok=token: self._apply_entry_thumbnail(e, img, tok))

                # Platforma art arda çok hızlı istek atmamak için kısa bir bekleme
                time.sleep(THUMBNAIL_FETCH_DELAY)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_entry_thumbnail(self, entry, pil_image: Image.Image, token: int) -> None:
        """Arka planda yüklenen bir entry thumbnail'ini ilgili satıra ve (gerekirse) ana panele işler."""
        if token != self._playlist_load_token:
            return  # Ana thread'e geçene kadar yeni bir analiz başlamış olabilir

        self._entry_thumb_images[entry.index] = pil_image

        label = self._entry_thumb_labels.get(entry.index)
        if label is not None and label.winfo_exists():
            icon = pil_image.copy()
            icon.thumbnail((32, 32), Image.Resampling.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=icon, dark_image=icon, size=(32, 32))
            self._entry_thumb_refs[entry.index] = ctk_img  # referansı canlı tut (GC'ye karşı)
            label.image = ctk_img  # ekstra pinning: widget kendi referansını da tutsun
            label.configure(image=ctk_img, text="")

        # Bu entry şu an sol panelde önizleniyorsa, büyük görüntüyü de güncelle
        if self._selected_preview_index == entry.index:
            self._set_main_thumbnail(pil_image)

    def _on_entry_click(self, entry) -> None:
        """Playlist satırına tıklanınca sol panelde SADECE o videoya ait bilgileri gösterir.

        extract_flat'ten gelen entry verisi (başlık, süre, thumbnail) anında
        gösterilir; ardından arka planda o videonun tam metadatası (kanal,
        izlenme, beğeni, yükleme tarihi vb.) çekilip hazır olunca panel
        _update_video_info üzerinden gerçek verilerle güncellenir.

        KRİTİK: Bu fonksiyonun tamamı try/except ile sarılı. Örneğin gizli/
        silinmiş bir video için entry.url boş/geçersiz gelirse veya beklenmedik
        bir veri sorunu çıkarsa, hata burada durdurulup kullanıcıya net bir
        mesaj gösteriliyor — aksi halde exception yarıda kesilen fonksiyonun
        state'i tutarsız bırakması yüzünden bir sonraki tıklamanın da
        çalışmaması gibi bir "kilitlenme" hissi yaratabiliyordu.
        """
        try:
            self._selected_preview_index = entry.index
            t = self.i18n.t

            # 1) Anında gösterebileceğimiz asgari bilgi (ağ beklemeden)
            self.title_label.configure(text=f"{t('title')}: {entry.title}")
            for key in self.meta_labels:
                self.meta_labels[key].configure(text=f"{t(key)}: —")
            self.meta_labels["duration"].configure(
                text=f"{t('duration')}: {format_duration_display(entry.duration) if entry.duration else '—'}"
            )
            self.status_label.configure(text="Video detayları yükleniyor...")

            cached_image = self._entry_thumb_images.get(entry.index)
            if cached_image is not None:
                self._set_main_thumbnail(cached_image)
            else:
                # Henüz arka planda yüklenmedi; hazır olunca _apply_entry_thumbnail otomatik günceller.
                self._set_main_thumbnail(None, placeholder_text="Yükleniyor...")

            # entry.url boşsa (örn. gizli/silinmiş video normalize edilemediyse)
            # arka plana hiç göndermiyoruz; analyze_url zaten InvalidURLError
            # fırlatacaktı, burada erkenden ve net bir mesajla kesiyoruz.
            if not entry.url:
                self.status_label.configure(text="Bu video için indirilebilir bir bağlantı bulunamadı.")
                self._set_main_thumbnail(cached_image, placeholder_text="Önizleme yok")
                return

            # 2) Videoya özel tam metadata (kanal, izlenme, beğeni vb.) arka planda çekiliyor
            self._fetch_entry_details_async(entry)
        except Exception as exc:
            # Ne olursa olsun fonksiyon burada güvenle sonlanıyor; state tutarsız
            # kalmıyor, bir sonraki tıklama normal şekilde çalışmaya devam ediyor.
            print(f"[ui] _on_entry_click hatası (entry={getattr(entry, 'title', '?')!r}): {exc!r}")

            # Önce metin/duruma dair geri bildirimi veriyoruz — bu adım thumbnail
            # işlemi her nasılsa başarısız olsa bile kullanıcıya mutlaka ulaşsın.
            self.status_label.configure(text=f"Video önizlenemedi: {exc}")

            # Thumbnail temizliğini ayrı, kendi içinde sarılı bir adımda yapıyoruz:
            # önce widget'ın kendi 'image' referansını (Python tarafında) doğrudan
            # None'a çekiyoruz, SONRA gerçek (boş) bir CTkImage ile configure
            # ediyoruz. Bu iki adımı ayırmak, olası bir TclError'ın status_label
            # güncellemesini engellemesinin önüne geçiyor.
            try:
                self.thumbnail_label.image = None
                empty = self._get_empty_thumbnail()
                self.thumbnail_label.configure(image=empty, text="Önizleme yok")
                self.thumbnail_label.image = empty
                self._thumbnail_ref = empty
            except Exception as thumb_exc:
                # Thumbnail sıfırlama bile başarısız olursa yutuyoruz — en azından
                # status_label zaten güncellendi, kullanıcı boşlukta kalmıyor.
                print(f"[ui] Thumbnail reset hatası (yutuldu): {thumb_exc!r}")

    def _fetch_entry_details_async(self, entry) -> None:
        """Tıklanan playlist entry'sinin tam VideoInfo detayını arka planda çeker."""
        self._entry_detail_token += 1
        request_token = self._entry_detail_token

        def worker() -> None:
            try:
                # KRİTİK DÜZELTME: bkz. _on_analyze'daki aynı düzeltme — bu
                # çağrı da cookie_source/cookie_file_path'i hiç geçirmiyordu.
                provider = ProviderRegistry.resolve(
                    entry.url,
                    cookie_source=self.settings.cookie_source,
                    cookie_file_path=self.settings.cookie_file_path or None,
                )
                video_info = provider.get_info(entry.url)
            except Exception as exc:
                # Sessizce yutmak yerine hatayı logluyoruz — böylece uygulamanın
                # nerede tıkandığını (hangi URL, hangi hata) konsoldan görebiliriz.
                print(f"[ui] Video detayı çekilemedi (url={entry.url!r}): {exc!r}")
                # None döndürüp ayrı bir "hata" kod yolu işletmek yerine, hatayı
                # VideoInfo.error alanında işaretliyoruz — böylece _update_video_info
                # tek, tutarlı bir render yolundan geçiyor ve provider.get_info'yu
                # tekrar tekrar çağırma/farklı state'ler icat etme ihtiyacı kalmıyor.
                video_info = VideoInfo(
                    title=entry.title,
                    duration=entry.duration or 0,
                    thumbnail_url=entry.thumbnail_url,
                    platform="Bilinmeyen",
                    upload_date_raw="",
                    channel="—",
                    view_count=None,
                    like_count=None,
                    dislike_count=None,
                    thumbnail_image=None,
                    error=str(exc),
                )

            self.after(0, lambda: self._apply_entry_details(entry, video_info, request_token))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_entry_details(
        self,
        entry,
        video_info: Optional[VideoInfo],
        request_token: int,
    ) -> None:
        """Arka planda çekilen tam video detayını sol panele işler (stale-guard'lı)."""
        if request_token != self._entry_detail_token:
            return  # Kullanıcı bu sırada başka bir videoya tıkladı, bu sonuç artık geçersiz
        if self._selected_preview_index != entry.index:
            return  # Ekstra güvenlik: önizleme başka bir entry'ye kaymış olabilir

        if video_info is None:
            # Normalde artık buraya düşülmemeli (worker her zaman bir VideoInfo
            # döndürüyor, hata durumunda bile) — yine de ekstra bir güvenlik ağı.
            self.status_label.configure(text="Detaylar yüklenemedi.")
            return

        # Hata durumunda bile satırın zaten yüklenmiş küçük thumbnail'i varsa
        # (kullanıcı bunu playlist listesinde görmüştü), onu büyük panelde de
        # göstermeye devam edelim — boş bir kutu yerine tanıdık bir görsel.
        if video_info.error and video_info.thumbnail_image is None:
            cached_image = self._entry_thumb_images.get(entry.index)
            if cached_image is not None:
                video_info.thumbnail_image = cached_image

        try:
            # _update_video_info tüm meta alanlarını (kanal, izlenme, beğeni, tarih,
            # thumbnail) bu videoya özel gerçek verilerle dolduruyor — playlist
            # paneli/seçimleri etkilenmiyor. info.error doluysa status_label'ı
            # otomatik olarak "Video kullanılamıyor: ..." şeklinde gösteriyor.
            self._update_video_info(video_info)
        except Exception as exc:
            # KRİTİK: Bu try/except olmadan burada patlayan bir hata (örn. bozuk
            # unicode başlığın Tkinter'da render edilememesi) status_label'ı
            # sonsuza dek "Yükleniyor..." durumunda bırakıyordu. Artık her
            # durumda kullanıcıya bir sonuç (başarı ya da net bir hata) gösteriliyor.
            print(f"[ui] _update_video_info render hatası (entry={entry.title!r}): {exc!r}")
            self.status_label.configure(text=f"Detaylar yüklenemedi (render hatası: {exc})")

    def _handle_analysis_result(self, result) -> None:
        """provider.get_info() sonucunun tipine göre (VideoInfo/PlaylistInfo) akışı ayırır."""
        if isinstance(result, PlaylistInfo):
            self._update_playlist_info(result)
        else:
            self._playlist_info = None
            self._selected_indices = set()
            self._entry_vars = {}
            self._selected_preview_index = None
            # Bekleyen playlist thumbnail/detay işlerini geçersiz kıl (stale-guard)
            self._playlist_load_token += 1
            self._entry_detail_token += 1
            self._hide_playlist_panel()
            self._update_video_info(result)

    def _on_analyze(self) -> None:
        url = self.url_entry.get().strip()
        if not url:
            self._show_error(self.i18n.t("missing_url"), self.i18n.t("enter_url"))
            return

        self._current_url = url
        self.status_label.configure(text=self.i18n.t("analyzing"))

        def task():
            # URL'nin platformuna uygun Provider'ı çözüp (bkz. providers.py)
            # analizi onun üzerinden yapıyoruz — böylece extractor_args gibi
            # platforma özel ayarlar artık indirmede olduğu gibi ANALİZ
            # aşamasında da devreye giriyor (TikTok/Twitter için tutarlılık).
            #
            # KRİTİK DÜZELTME (bkz. konuşma geçmişi — 'auto'ya sessizce düşme
            # bug'ı): Bu çağrı cookie_source/cookie_file_path'i HİÇ
            # GEÇİRMİYORDU, bu yüzden ProviderRegistry.resolve()'un varsayılanı
            # olan "auto"ya düşüyordu — kullanıcı Ayarlar'da "Manuel
            # cookies.txt" seçmiş olsa BİLE, ANALİZ aşaması hep "auto" ile
            # (tarayıcı çerezi deneyerek, DPAPI'ye çarparak) çalışıyordu. Bu,
            # tam olarak "İndir" butonuna hiç basılmadan Instagram'ın "empty
            # media response" ile patlamasının kök sebebiydi.
            provider = ProviderRegistry.resolve(
                url,
                cookie_source=self.settings.cookie_source,
                cookie_file_path=self.settings.cookie_file_path or None,
            )
            return provider.get_info(url)

        self._run_in_thread(task, on_success=self._handle_analysis_result)

    def _update_progress(self, progress: ProgressInfo) -> None:
        t = self.i18n.t
        self.progress_bar.set(progress.percent / 100)
        self.status_label.configure(text=progress.status)
        self.speed_label.configure(text=f"{t('speed')}: {progress.speed}")
        self.eta_label.configure(text=f"{t('remaining')}: {progress.eta}")

    def _on_download(self) -> None:
        # BUSY-LOCK (çift-tıklama koruması): _set_busy zaten analyze_btn/
        # download_btn/folder_btn'i birlikte disable eden mevcut mekanizma —
        # burada da aynısını kullanıyoruz ki hızlı art arda tıklamada aynı
        # görev iki kez kuyruğa girmesin (bkz. konuşma: "yetim indirme görevi"
        # riski — ikinci tıklama _single_task_id'yi üzerine yazıp ilkini UI'dan
        # sessizce düşürüyordu). _set_busy(False) çağrısı, görev tamamlanınca/
        # hata alınca _handle_single_task_update ve _finish_playlist_batch
        # içinde yapılıyor.
        if self._is_busy:
            return

        if self._playlist_info is not None:
            self._set_busy(True)
            self._on_download_playlist()
            return

        url = self.url_entry.get().strip() or self._current_url
        if not url:
            self._show_error(self.i18n.t("missing_url"), self.i18n.t("analyze_first"))
            return

        media_format = self.format_menu.get()
        selected_quality = self.quality_menu.get()
        quality_key = self.i18n.quality_key_from_label(selected_quality)

        if media_format in AUDIO_FORMATS:
            audio_bitrate = self._resolve_audio_bitrate(media_format, selected_quality)
        else:
            audio_bitrate = self._resolve_audio_bitrate(media_format, self.audio_quality_menu.get())

        t = self.i18n.t
        self.progress_bar.set(0)
        self.status_label.configure(text=t("download_starting"))
        self.speed_label.configure(text=f"{t('speed')}: -")
        self.eta_label.configure(text=f"{t('remaining')}: -")

        display_title = self._video_info.title if self._video_info else url
        video_platform = self._video_info.platform if self._video_info else ""
        video_thumbnail_url = self._video_info.thumbnail_url if self._video_info else None

        # KRİTİK TEŞHİS (bkz. konuşma geçmişi): DownloadTask'a giden
        # cookie_source/cookie_file_path'in TAM OLARAK bu anda self.settings'te
        # ne olduğunu gösteriyoruz — zincirin ui.py ucundaki son kontrol noktası.

        task = DownloadTask(
            url=url,
            media_format=media_format,
            quality_key=quality_key,
            output_dir=self._output_dir,
            audio_bitrate=audio_bitrate,
            filename_template=self.settings.filename_template,
            embed_thumbnail=self.settings.embed_thumbnail,
            concurrent_fragments=self.settings.concurrent_fragments,
            status_messages=self._status_messages(),
            display_title=display_title,
            platform=video_platform,
            thumbnail_url=video_thumbnail_url,
            cookie_source=self.settings.cookie_source,
            cookie_file_path=self.settings.cookie_file_path or None,
            download_subtitles=getattr(self.settings, "download_subtitles", False),
            subtitle_langs=getattr(self.settings, "subtitle_langs", None),
            speed_limit_kbps=getattr(self.settings, "speed_limit_kbps", 0),
        )

        # Bu tek görevi playlist batch state'inden ayrı, tekil bir indirme olarak takip ediyoruz.
        self._playlist_batch = None
        self._single_task_id = task.task_id

        self.pause_btn.configure(text="⏸️")
        self._single_control_frame.grid()

        self._set_busy(True)
        self._task_queue_manager.enqueue(task)

    def _on_download_playlist(self) -> None:
        """Seçili playlist videolarının tümünü kuyruğa ekler.

        İndirmelerin kendisi artık burada senkron/thread bazlı değil;
        TaskQueueManager'ın worker havuzu bu görevleri (ayarlardaki
        concurrent_downloads sayısı kadar paralel) arka planda işliyor.
        İlerleme/tamamlanma bildirimleri _on_task_update -> _handle_playlist_task_update
        üzerinden geliyor.
        """
        if not self._selected_indices:
            self._show_error(self.i18n.t("missing_url"), "En az bir video seçmelisiniz.")
            return

        playlist = self._playlist_info
        selected_entries = sorted(
            (entry for entry in playlist.entries if entry.index in self._selected_indices),
            key=lambda entry: entry.index,
        )

        media_format = self.format_menu.get()
        selected_quality = self.quality_menu.get()
        quality_key = self.i18n.quality_key_from_label(selected_quality)

        if media_format in AUDIO_FORMATS:
            audio_bitrate = self._resolve_audio_bitrate(media_format, selected_quality)
        else:
            audio_bitrate = self._resolve_audio_bitrate(media_format, self.audio_quality_menu.get())

        t = self.i18n.t
        self.progress_bar.set(0)
        self.status_label.configure(text=t("download_starting"))
        self.speed_label.configure(text=f"{t('speed')}: -")
        self.eta_label.configure(text=f"{t('remaining')}: -")

        # Tekil indirme state'iyle çakışmasın diye ayrı tutuyoruz.
        self._single_task_id = None
        self._playlist_batch = {
            "total": len(selected_entries),
            "task_ids": set(),
            "completed": 0,
            "failed": [],  # (display_title, error_message) çiftleri
            "cancelled": 0,
            "label": "Playlist",  # bkz. _on_batch_urls_submitted — aynı mekanizma "Toplu indirme" etiketiyle de kullanılıyor
            "finished_notified": False,  # _finish_playlist_batch'in yalnızca 1 kez tetiklenmesini garanti eder
            # KRİTİK DÜZELTME (overcounting): task_queue.py'deki _notify(), aynı
            # DownloadTask objesini (kopyalamadan) closure'a geçiriyor. Worker
            # thread RUNNING -> COMPLETED geçişini hızlı yaptığında, ana thread'e
            # sırayla düşen iki callback (biri stale RUNNING, biri gerçek
            # COMPLETED) task.status'u AYNI ANDA "COMPLETED" olarak okuyabiliyor.
            # Bu da tek bir task için sayacın iki kez artmasına (4/3 gibi) yol
            # açıyordu. processed_ids, task_id bazında tekilleştirme yaparak her
            # task'ın completed/failed sayaçlarına EN FAZLA BİR KEZ katkı
            # yapmasını garanti ediyor.
            "processed_ids": set(),
        }

        batch = self._playlist_batch
        # Dashboard state'ini bu batch için sıfırlıyoruz.
        self._playlist_row_data = {}
        self._playlist_row_widgets = {}
        self._dirty_row_ids = set()

        # KRİTİK TEŞHİS (bkz. konuşma geçmişi): bkz. _on_download'daki aynı log.

        for entry in selected_entries:
            # NOT (dürüst varsayım): downloader.py'yi görmediğim için
            # PlaylistInfo'nun bir .platform alanı olup olmadığını bilmiyorum
            # (playlist entry'lerinde .platform kullanan bir örnek bulamadım,
            # sadece .thumbnail_url için var). getattr(..., "") ile güvenli
            # okuyoruz — alan yoksa AttributeError patlamak yerine sessizce
            # boş kalır, HistoryDialog bunu "Bilinmeyen" olarak gösterir.
            # downloader.py'yi paylaşırsan gerçek alan adına göre düzeltirim.
            playlist_platform = getattr(self._playlist_info, "platform", "")

            task = DownloadTask(
                url=entry.url,
                media_format=media_format,
                quality_key=quality_key,
                output_dir=self._output_dir,
                audio_bitrate=audio_bitrate,
                filename_template=self.settings.filename_template,
                embed_thumbnail=self.settings.embed_thumbnail,
                concurrent_fragments=self.settings.concurrent_fragments,
                status_messages=self._status_messages(),
                display_title=entry.title,
                platform=playlist_platform,
                thumbnail_url=entry.thumbnail_url,
                cookie_source=self.settings.cookie_source,
                cookie_file_path=self.settings.cookie_file_path or None,
                download_subtitles=getattr(self.settings, "download_subtitles", False),
                subtitle_langs=getattr(self.settings, "subtitle_langs", None),
                speed_limit_kbps=getattr(self.settings, "speed_limit_kbps", 0),
            )
            batch["task_ids"].add(task.task_id)
            # NOT: Burada henüz WIDGET oluşturmuyoruz — sadece hafif bir veri
            # kaydı. Gerçek satır widget'ı, o task ilk güncellemesini
            # aldığında (worker onu işlemeye başladığında) _sync_dashboard_rows
            # içinde LAZY olarak yaratılacak. Bkz. o metodun docstring'i.
            self._playlist_row_data[task.task_id] = {
                "title": entry.title,
                "status": self.i18n.t("dashboard_pending"),
                "percent": 0.0,
            }
            self._task_queue_manager.enqueue(task)

        # KRİTİK (görsel kalabalık): Playlist modunda eski tek satırlık
        # status_label + ana progress_bar (speed/eta dahil, hepsi
        # progress_frame içinde) artık gereksiz — her videonun kendi durumu
        # Dashboard'ta zaten görünüyor. grid_remove() ile TAMAMEN gizliyoruz
        # (satır 5 boşalınca Dashboard yukarı kayıyor, boşluk kalmıyor).
        # Batch bitince (_finish_playlist_batch) tekrar gösteriliyor.
        self.progress_frame.grid_remove()

        self.dashboard_frame.grid()
        # Genişliği DEĞİŞTİRMİYORUZ (playlist paneli açıksa 1180, değilse 820
        # olarak kalsın) — sadece yükseklik Dashboard'a yer açacak şekilde büyüsün.
        current_width, _ = self._current_geometry_wh()
        dashboard_height = int(self._dashboard_geometry.split("x")[1])
        self._smooth_resize(current_width, dashboard_height)
        self._start_dashboard_sync()

    def _record_history(self, task: DownloadTask) -> None:
        """Başarıyla tamamlanmış bir görevi İndirme Geçmişi'ne (history.json) kaydeder.

        Hem _handle_single_task_update hem _handle_playlist_task_update'in
        COMPLETED dalından çağrılıyor — task_id tekilleştirmesi zaten o iki
        yerde yapıldığından (bkz. _notified_task_ids / processed_ids), burada
        ekstra bir guard'a gerek yok: bu fonksiyon her task_id için en fazla
        bir kez çağrılacağı garanti edilmiş bir noktadan tetikleniyor.
        """
        if not task.result_path:
            return  # COMPLETED ama result_path yoksa (beklenmedik durum) kayıt atlanır
        try:
            self._history_manager.add_entry(
                HistoryEntry(
                    title=task.display_title or task.url,
                    platform=task.platform or "Bilinmeyen",
                    file_path=str(task.result_path),
                    thumbnail_url=task.thumbnail_url,
                )
            )
        except OSError as exc:
            # history.json'a yazılamaması indirmenin kendisini geçersiz kılmamalı —
            # sadece logluyoruz, kullanıcıya ayrı bir hata popup'ı göstermiyoruz.
            print(f"[ui] İndirme geçmişi kaydedilemedi (task={task.task_id}): {exc!r}")

    def _handle_single_task_update(self, task: DownloadTask) -> None:
        """Tekil (playlist dışı) bir DownloadTask'ın durum güncellemesini işler."""
        t = self.i18n.t

        if task.status == TaskStatus.RUNNING:
            self.pause_btn.configure(text="⏸️", state="normal")
            if task.progress:
                self._update_progress(task.progress)
            else:
                self.status_label.configure(text=t("download_starting"))
            return

        if task.status == TaskStatus.PAUSED:
            self.pause_btn.configure(text="▶️", state="normal")
            self.status_label.configure(text=t("paused"))
            return

        if task.status == TaskStatus.CANCELLED:
            if task.task_id in self._notified_task_ids:
                return
            self._notified_task_ids.add(task.task_id)
            self.status_label.configure(text=t("download_cancelled"))
            self._single_control_frame.grid_remove()
            self._single_task_id = None
            self._set_busy(False)
            return

        if task.status == TaskStatus.COMPLETED:
            if task.task_id in self._notified_task_ids:
                return  # KESİN KİLİT: bu görev için bildirim zaten gösterildi
            self._notified_task_ids.add(task.task_id)
            self._record_history(task)
            if self.settings.sound_notifications:
                sound_notifier.play_success(self.settings.success_sound_path, self.settings.success_sound_volume)

            self._update_progress(
                ProgressInfo(percent=100.0, speed="—", eta="00:00", status=t("completed"))
            )
            # SIRALAMA DÜZELTMESİ (bkz. konuşma — "klasör popup'tan önce öne
            # fırlıyor"): messagebox.showinfo() ZATEN bloklayan bir çağrı —
            # kullanıcı 'Tamam'a basana kadar bir sonraki satır çalışmaz. Bu
            # yüzden ayrı bir buton-callback'ine gerek yok: popup'ı ÖNCE
            # gösterip klasör açmayı ondan SONRAYA almak, "önce popup, Tamam'a
            # basınca klasör" davranışını doğrudan garanti ediyor.
            # DÜZELTME (bkz. konuşma — "özel ses ile Windows'un varsayılan
            # success sesi aynı anda çalıyor"): self.bell() burada AYRI bir
            # ses mekanizmasıydı (sound_notifications sistemi eklenmeden
            # önce konulmuştu) — notify_on_complete VE sound_notifications
            # ikisi de varsayılan açık olduğu için, sound_notifier'ın çaldığı
            # (özel/seçilen) sesle ÜST ÜSTE biniyordu. Artık ses TAMAMEN
            # sound_notifications'ın sorumluluğunda; notify_on_complete
            # SADECE popup'ı kontrol ediyor.
            if self.settings.notify_on_complete:
                self._show_success(t("download_complete"), t("file_saved", path=task.result_path))
            if self.settings.open_folder_after_download and task.result_path:
                open_folder_in_explorer(task.result_path.parent)
            self._single_control_frame.grid_remove()
            self._single_task_id = None
            self._set_busy(False)
        elif task.status == TaskStatus.FAILED:
            if task.task_id in self._notified_task_ids:
                return  # KESİN KİLİT: aynı mantık, hata bildirimi için de geçerli
            self._notified_task_ids.add(task.task_id)
            if self.settings.sound_notifications:
                sound_notifier.play_error(self.settings.error_sound_path, self.settings.error_sound_volume)

            self.status_label.configure(text=f"İndirme başarısız: {task.error}")
            self._show_error(t("download_error"), f"{task.display_title or task.url}\n\n{task.error}")
            self._single_control_frame.grid_remove()
            self._single_task_id = None
            self._set_busy(False)

    def _handle_playlist_task_update(self, task: DownloadTask) -> None:
        """Playlist batch'ine ait bir DownloadTask'ın durum güncellemesini işler.

        NOT: Birden fazla video paralel indiği için tek bir progress_bar'ın
        tam hassasiyetle her videoyu ayrı ayrı yansıtması mümkün değil; bu
        yüzden progress_bar'ı "tamamlanan görev oranı + en son güncellenen
        görevin kendi yüzdesi" şeklinde yaklaşık gösteriyoruz. Hız/kalan süre
        etiketleri de en son ilerleme bildirimini yapan göreve ait olur.
        """
        if not self._playlist_batch:
            return
        batch = self._playlist_batch

        # Dashboard satırı için veri güncellemesi — widget'a dokunmuyor,
        # bkz. _update_dashboard_row / _sync_dashboard_rows docstring'leri.
        self._update_dashboard_row(task)

        t = self.i18n.t
        total = batch["total"]
        finished = batch["completed"] + len(batch["failed"]) + batch.get("cancelled", 0)

        if task.status == TaskStatus.RUNNING:
            if task.progress:
                fraction = (finished + task.progress.percent / 100) / total
                self.progress_bar.set(min(max(fraction, 0.0), 1.0))
                self.speed_label.configure(text=f"{t('speed')}: {task.progress.speed}")
                self.eta_label.configure(text=f"{t('remaining')}: {task.progress.eta}")
            # KRİTİK DÜZELTME (flickering): status_label'a ASLA task.display_title
            # (video adı) yazmıyoruz — sadece sabit, genel bir metin. Ayrıca aynı
            # metni tekrar tekrar configure() etmek bile (içerik değişmese dahi)
            # Tk'de görsel bir titremeye yol açabiliyor; bu yüzden metni SADECE
            # gerçekten değiştiyse güncelliyoruz (_set_status_label_if_changed).
            self._set_status_label_if_changed(
                f"{batch.get('label', 'Playlist')} indiriliyor... ({finished}/{total} tamamlandı)"
            )
            return

        if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return  # PENDING/PAUSED — sayaç için henüz bir işlem yok

        # KRİTİK DÜZELTME (overcounting): Aynı task_id için COMPLETED/FAILED
        # zaten işlendiyse (bkz. _playlist_batch başlatma kısmındaki NOT —
        # stale RUNNING callback'inin task.status'u sonradan mutasyona uğramış
        # olarak okuması), bu bildirimi tamamen yok sayıyoruz. Böylece her
        # video sayaca EN FAZLA BİR KEZ katkı yapıyor.
        if task.task_id in batch["processed_ids"]:
            return
        batch["processed_ids"].add(task.task_id)

        if task.status == TaskStatus.COMPLETED:
            # min(): processed_ids tekilleştirmesi zaten garanti ediyor ama
            # görünen sayacın total'i ASLA aşmaması için ek bir güvenlik ağı.
            batch["completed"] = min(batch["completed"] + 1, total)
            self._record_history(task)
        elif task.status == TaskStatus.CANCELLED:
            batch["cancelled"] = batch.get("cancelled", 0) + 1
        else:  # TaskStatus.FAILED
            batch["failed"].append((task.display_title, task.error))

        finished = min(batch["completed"] + len(batch["failed"]) + batch.get("cancelled", 0), total)
        self.progress_bar.set(finished / total if total else 1.0)
        # force=True: bu bir "video bitti" milestone'u — RUNNING tiklerinin
        # aksine seyrek ve önemli bir olay, throttle'a takılıp ekranda hiç
        # görünmeden atlanmamalı.
        self._set_status_label_if_changed(
            f"{batch.get('label', 'Playlist')} indiriliyor... ({finished}/{total} tamamlandı)", force=True
        )

        # KRİTİK DÜZELTME (çifte özet mesajı / race condition): finished >= total
        # şartı doğru olsa bile, _finish_playlist_batch'i SADECE bir kez tetiklemek
        # istiyoruz. Örn. messagebox modal iken Tkinter'ın event loop'u başka
        # after() callback'lerini işlemeye devam edebiliyor; bu da nadiren aynı
        # "bitiş" durumunun ikinci bir kez işlenmesine yol açabiliyordu.
        # finished_notified flag'i bunu kesin olarak engelliyor.
        if finished >= total and not batch.get("finished_notified"):
            batch["finished_notified"] = True
            self._finish_playlist_batch(batch)

    def _apply_background_image(self, path: str) -> None:
        """Ayarlardan gelen bir arka plan resmi yolunu yükler (ya da temizler).

        `path` boşsa arka plan kaldırılır (varsayılan pencere rengine döner).
        Yükleme başarısız olursa (dosya silinmiş/bozuk/erişilemez) sessizce
        arka planı temizliyoruz — bir wallpaper dosyası uygulamayı asla
        çökertmemeli, en kötü ihtimalle resimsiz kalır.
        """
        if not path:
            self._background_image_source = None
            self._background_ctk_image = None
            self.background_label.configure(image=None)
            return

        try:
            self._background_image_source = Image.open(path).convert("RGBA")
        except Exception:
            self._background_image_source = None
            self._background_ctk_image = None
            self.background_label.configure(image=None)
            return

        self._render_background_image_for_size(self.winfo_width(), self.winfo_height())

    def _on_window_configure(self, event) -> None:
        """Pencere boyutu her değiştiğinde (sürükleyerek resize dahil) tetiklenir.

        KRİTİK (performans): <Configure>, bir resize sürüklemesi sırasında
        saniyede onlarca kez ateşlenebiliyor; her seferinde PIL ile yeniden
        boyutlandırma yapmak UI'ı kilitleyebilir. Dashboard senkronizasyonunda
        kullandığımız aynı "debounce" desenini uyguluyoruz: her olayda bekleyen
        bir job varsa iptal edip yeniden planlıyoruz — gerçek yeniden
        boyutlandırma, sürükleme durduktan ~150ms sonra SADECE BİR KEZ çalışır.
        """
        if event.widget is not self or not self._background_image_source:
            return
        if self._background_resize_job is not None:
            self.after_cancel(self._background_resize_job)
        self._background_resize_job = self.after(
            150,
            lambda: self._render_background_image_for_size(self.winfo_width(), self.winfo_height()),
        )

    def _render_background_image_for_size(self, width: int, height: int) -> None:
        self._background_resize_job = None
        if not self._background_image_source or width <= 1 or height <= 1:
            return
        self._background_ctk_image = ctk.CTkImage(
            light_image=self._background_image_source,
            dark_image=self._background_image_source,
            size=(width, height),
        )
        self.background_label.configure(image=self._background_ctk_image)

    def _current_geometry_wh(self) -> tuple[int, int]:
        """self.geometry()'nin döndürdüğü 'WxH+X+Y' formatından genişlik/yükseklik okur."""
        size_part = self.geometry().split("+")[0]
        width_str, height_str = size_part.split("x")
        return int(width_str), int(height_str)

    def _smooth_resize(
        self,
        target_width: int,
        target_height: int,
        steps: int = 6,
        delay: int = 20,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        """Pencereyi ANİDEN değil, `steps` adımda kademeli olarak hedef boyuta taşır.

        Genişlik VE yükseklik birlikte, doğrusal interpolasyonla animasyonlanıyor.
        ARTIK TÜM boyut değişimleri (playlist analiz panelinin açılışı/kapanışı
        `_show_playlist_panel`/`_hide_playlist_panel` VE indirme Dashboard'unun
        açılışı/kapanışı `_on_download_playlist`/`_finish_playlist_batch`) bu
        fonksiyon üzerinden çağrılıyor — kodda çıplak `self.geometry("WxH")`
        çağrısı kalmadı (bkz. aşağıdaki _resize_step, tek gerçek geometry()
        çağrı noktası).

        on_complete: animasyon son adımını bitirince (bir kez) çağrılır.
        _show_playlist_panel, ağır playlist panelini (yüzlerce widget
        olabilir) TAM OLARAK BURADA — animasyon bitince — gridliyor; böylece
        panel, her animasyon adımında tekrar tekrar yeniden yerleşmek
        zorunda kalmıyor, sadece bir kez, animasyon durduktan sonra yerleşiyor.
        """
        if self._resize_animation_job is not None:
            self.after_cancel(self._resize_animation_job)
            self._resize_animation_job = None

        start_width, start_height = self._current_geometry_wh()
        if start_width == target_width and start_height == target_height:
            if on_complete is not None:
                on_complete()
            return  # zaten hedefteyiz, animasyona gerek yok

        self._resize_step(start_width, start_height, target_width, target_height, 1, steps, delay, on_complete)

    def _resize_step(
        self,
        start_width: int,
        start_height: int,
        target_width: int,
        target_height: int,
        step: int,
        total_steps: int,
        delay: int,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        progress = step / total_steps
        next_width = round(start_width + (target_width - start_width) * progress)
        next_height = round(start_height + (target_height - start_height) * progress)
        self.geometry(f"{next_width}x{next_height}")
        # Her adımı hemen render'a zorla — aksi halde after(15,...) ile
        # zamanlanan sonraki adım, bu adımın çizimi bitmeden tetiklenip
        # adımların kuyrukta üst üste binmesine (görsel "titreme") yol
        # açabiliyor, özellikle ağır bir panel (playlist listesi gibi) aynı
        # anda yeniden yerleşiyorsa.
        self.update_idletasks()

        if step >= total_steps:
            self._resize_animation_job = None
            if on_complete is not None:
                on_complete()
            return

        self._resize_animation_job = self.after(
            delay,
            lambda: self._resize_step(
                start_width, start_height, target_width, target_height, step + 1, total_steps, delay, on_complete
            ),
        )

    def _update_dashboard_row(self, task: DownloadTask) -> None:
        """Dashboard satırı için hafif veri modelini (_playlist_row_data) günceller.

        Bilerek widget'a DOKUNMUYORUZ — gerçek widget güncellemesi
        _sync_dashboard_rows'ta, sabit tempoda yapılıyor. Bu ayrım
        performans için kritik: bu metod, her task_update callback'inde
        (yani saniyede onlarca kez) çağrılabilir; dict yazmak ucuz, widget
        configure() etmek pahalı. Detaylı gerekçe _sync_dashboard_rows
        docstring'inde.
        """
        data = self._playlist_row_data.get(task.task_id)
        if data is None:
            return  # bu batch'e ait değil ya da henüz kayıt yok

        if task.status == TaskStatus.RUNNING:
            data["status"] = self.i18n.t("downloading")
            data["controls_state"] = "running"
            if task.progress:
                data["percent"] = max(0.0, min(task.progress.percent / 100, 1.0))
        elif task.status == TaskStatus.PAUSED:
            data["status"] = self.i18n.t("paused")
            data["controls_state"] = "paused"
        elif task.status == TaskStatus.COMPLETED:
            data["status"] = self.i18n.t("dashboard_completed")
            data["percent"] = 1.0
            data["controls_state"] = "done"
        elif task.status == TaskStatus.FAILED:
            data["status"] = self.i18n.t("dashboard_failed")
            data["controls_state"] = "done"
        elif task.status == TaskStatus.CANCELLED:
            data["status"] = self.i18n.t("dashboard_cancelled")
            data["controls_state"] = "done"
        else:
            return  # PENDING — görünüm zaten "Bekliyor", dirty'e gerek yok

        self._dirty_row_ids.add(task.task_id)

    def _on_cancel_row(self, task_id: str) -> None:
        self._task_queue_manager.cancel(task_id)

    def _on_toggle_pause_row(self, task_id: str) -> None:
        task = self._task_queue_manager.get_task(task_id)
        if task is None:
            return
        if task.status == TaskStatus.PAUSED:
            self._task_queue_manager.resume(task_id)
        elif task.status == TaskStatus.RUNNING:
            self._task_queue_manager.pause(task_id)

    def _create_dashboard_row(self, title: str, task_id: str) -> dict:
        """Tek bir playlist satırı için widget grubunu (frame+progress+status) inşa eder.

        LAZY çağrılır — bkz. _sync_dashboard_rows. Satır sırası, o ana kadar
        oluşturulmuş satır sayısına göre belirleniyor (indirilme/tamamlanma
        sırasıyla eşleşir, playlist'teki orijinal sırayla değil — bu bilinçli
        bir tercih: kullanıcı en son ne olduğunu üstte/altta net görsün diye
        değil, basitlik için; ileride istenirse index bazlı grid() ile
        orijinal sıraya sabitlenebilir).
        """
        row_frame = ctk.CTkFrame(self.dashboard_frame, fg_color=("gray92", "gray20"))
        row_frame.grid(row=len(self._playlist_row_widgets), column=0, sticky="ew", padx=2, pady=2)
        row_frame.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            row_frame,
            text=self._truncate_title(title),
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        title_label.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=(6, 2))

        status_label = ctk.CTkLabel(
            row_frame,
            text=self.i18n.t("dashboard_pending"),
            anchor="e",
            width=90,
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        )
        status_label.grid(row=0, column=1, sticky="e", padx=(4, 8), pady=(6, 2))

        row_pause_btn = ctk.CTkButton(
            row_frame,
            text="⏸️",
            width=24,
            height=20,
            font=ctk.CTkFont(size=10),
            command=lambda tid=task_id: self._on_toggle_pause_row(tid),
        )
        row_pause_btn.grid(row=0, column=2, padx=(0, 2), pady=(6, 2))

        row_cancel_btn = ctk.CTkButton(
            row_frame,
            text="❌",
            width=24,
            height=20,
            font=ctk.CTkFont(size=10),
            fg_color="transparent",
            border_width=1,
            command=lambda tid=task_id: self._on_cancel_row(tid),
        )
        row_cancel_btn.grid(row=0, column=3, padx=(0, 8), pady=(6, 2))

        progress_bar = ctk.CTkProgressBar(row_frame, height=6)
        progress_bar.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=(0, 6))
        progress_bar.set(0)

        return {
            "frame": row_frame,
            "title_label": title_label,
            "progress": progress_bar,
            "status_label": status_label,
            "pause_btn": row_pause_btn,
            "cancel_btn": row_cancel_btn,
        }

    def _start_dashboard_sync(self) -> None:
        """Dashboard senkronizasyon döngüsünü başlatır (zaten çalışıyorsa no-op)."""
        if self._dashboard_sync_job is not None:
            return
        self._sync_dashboard_rows()

    def _sync_dashboard_rows(self) -> None:
        """Playlist dashboard satır widget'larını periyodik (200ms) olarak günceller.

        KRİTİK (performans, 100+ videoluk playlist'ler için): _update_dashboard_row
        her task_update sinyalinde tetiklenebiliyor — paralel worker sayısı kadar
        eşzamanlı indirme varsa, bu saniyede onlarca kez olabilir. Eğer her
        sinyalde ilgili satırın widget'ını DOĞRUDAN configure() etseydik, widget
        update frekansı gelen sinyal frekansına bağlı kalırdı ve 100+ satırlık
        bir playlist'te UI thread'i tıkanabilirdi.

        Bunun yerine iki katmanlı bir tasarım kullanıyoruz:
          1) Veri (_playlist_row_data) her sinyalde güncellenir (ucuz, sadece dict).
          2) Widget senkronizasyonu SABİT bir tempoda (200ms) ve SADECE bu turda
             "dirty" işaretlenmiş satırlar için yapılır. Böylece widget update
             frekansı sinyal frekansından bağımsız, sabit bir üst sınıra sahip
             olur — playlist 3 video da olsa 300 video da olsa aynı.

        Satır widget'ları da LAZY oluşturulur (ilk güncelleme geldiğinde). Worker
        sayısı sınırlı olduğundan (bkz. task_queue.py max_workers), aynı anda
        RUNNING olan (dolayısıyla aynı anda widget'ı yaratılan) satır sayısı da
        doğal olarak sınırlı kalır — 100+ widget'lık ani bir "başlangıç patlaması"
        yerine, indirme ilerledikçe kademeli bir oluşum olur.

        NOT: Bu, CTkScrollableFrame'in görünmeyen satırları da bellekte tuttuğu
        gerçeğini (native virtualization yok) ortadan kaldırmıyor — çok büyük
        playlist'lerde (birkaç yüz+) widget SAYISI hâlâ bir üst sınır. Bu
        düzeltme sadece "update frekansı" kaynaklı kilitlenmeyi çözüyor. Gerçek
        sanallaştırma (scroll pozisyonuna göre sadece görünen satırları inşa
        etmek) ayrı ve daha büyük bir iş — ihtiyaç olursa (test sırasında hâlâ
        yavaşlık görülürse) ayrı bir adımda ele alınmalı.
        """
        if not self._playlist_batch:
            self._dashboard_sync_job = None
            return

        dirty_ids, self._dirty_row_ids = self._dirty_row_ids, set()

        for task_id in dirty_ids:
            data = self._playlist_row_data.get(task_id)
            if data is None:
                continue
            row = self._playlist_row_widgets.get(task_id)
            if row is None:
                row = self._create_dashboard_row(data["title"], task_id)
                self._playlist_row_widgets[task_id] = row
            row["progress"].set(data["percent"])
            row["status_label"].configure(text=data["status"])

            controls_state = data.get("controls_state", "pending")
            if controls_state == "done":
                row["pause_btn"].configure(state="disabled")
                row["cancel_btn"].configure(state="disabled")
            elif controls_state == "paused":
                row["pause_btn"].configure(text="▶️", state="normal")
                row["cancel_btn"].configure(state="normal")
            else:  # "running" or "pending"
                row["pause_btn"].configure(text="⏸️", state="normal")
                row["cancel_btn"].configure(state="normal")

        self._dashboard_sync_job = self.after(200, self._sync_dashboard_rows)

    def _set_status_label_if_changed(self, text: str, force: bool = False) -> None:
        """status_label'ı hem 'içerik değişti mi' hem 'zaman aşımı' kontrolüyle günceller.

        Paralel playlist indirmesinde çok sayıda RUNNING bildirimi kısa
        aralıklarla gelebiliyor. İki koruma katmanı var:
          1) İçerik aynıysa hiç configure() çağırmıyoruz (gereksiz redraw yok).
          2) İçerik değişmiş olsa bile, son güncellemeden bu yana 100ms
             geçmediyse ERTELİYORUZ — yüksek frekanslı RUNNING tiklerinin
             UI'ı "disko gibi yanıp sönmesini" engelliyor.
        force=True (örn. tamamlanma/hata gibi ayrık, önemli olaylar için)
        throttle'ı bypass eder — bu tür milestone'lar asla ertelenmez/kaybolmaz.
        """
        now = time.monotonic()
        if not force:
            if self._last_status_text == text:
                return
            if now - self._last_status_update_time < 0.1:  # 100ms throttle
                return
        self._last_status_text = text
        self._last_status_update_time = now
        self.status_label.configure(text=text)

    def _finish_playlist_batch(self, batch: Optional[dict] = None) -> None:
        """Playlist'teki tüm görevler bitince (hepsi COMPLETED/FAILED) özet gösterir.

        batch parametresi: çağıran taraf (_handle_playlist_task_update) hangi
        batch'i bitirdiğini AÇIKÇA geçirir. Bu, aşağıdaki senaryoyu önlemek
        için önemli: özet penceresi (modal messagebox) açıkken kullanıcı YENİ
        bir playlist indirmesi başlatırsa, self._playlist_batch artık o yeni
        batch'i gösterir — bu fonksiyon kendi işini bitirirken self._playlist_batch'i
        koşulsuz None yaparsa, yeni başlamış playlist'in state'ini SİLER. Bunun
        yerine sadece HÂLÂ İŞLEDİĞİMİZ batch güncelse (kimlik kontrolüyle) sıfırlıyoruz.
        """
        if batch is None:
            batch = self._playlist_batch
        if not batch:
            return

        t = self.i18n.t
        succeeded = batch["completed"]
        failed = batch["failed"]
        cancelled = batch.get("cancelled", 0)

        self._update_progress(
            ProgressInfo(percent=100.0, speed="—", eta="00:00", status=t("completed"))
        )

        summary_lines = [t("batch_summary", succeeded=succeeded, failed=len(failed), cancelled=cancelled)]
        if failed:
            summary_lines.append("")
            summary_lines.append(t("batch_failed_videos_header"))
            for title, error in failed:
                summary_lines.append(f"- {title}: {error}")

        # Ses: TEK bir özet sesi — her video için ayrı ayrı ÇALMIYORUZ (bkz.
        # konuşma). Herhangi bir hata varsa hata sesi, hepsi başarılıysa
        # başarı sesi. _handle_single_task_update'teki aynı sound_notifications
        # ayarına bağlı.
        if self.settings.sound_notifications:
            if failed:
                sound_notifier.play_error(self.settings.error_sound_path, self.settings.error_sound_volume)
            else:
                sound_notifier.play_success(self.settings.success_sound_path, self.settings.success_sound_volume)

        # SIRALAMA DÜZELTMESİ (bkz. _handle_single_task_update'teki aynı not):
        # popup ÖNCE gösterilir (messagebox.showinfo bloklayan bir çağrı —
        # 'Tamam'a basılmadan sonraki satır çalışmaz), klasör açma SONRA.
        # self.bell() KALDIRILDI (bkz. konuşma — aynı düzeltme, yukarıdaki
        # tekil indirme dalıyla tutarlı: ses artık SADECE sound_notifications
        # üzerinden, notify_on_complete sadece popup'ı kontrol ediyor).
        if self.settings.notify_on_complete:
            self._show_success(t("download_complete"), "\n".join(summary_lines))

        if self.settings.open_folder_after_download:
            # Klasörü bir kez açmak için batch'teki herhangi bir tamamlanmış task'ı kullanıyoruz.
            for task_id in batch["task_ids"]:
                completed_task = self._active_tasks.get(task_id)
                if completed_task and completed_task.status == TaskStatus.COMPLETED and completed_task.result_path:
                    open_folder_in_explorer(completed_task.result_path.parent)
                    break

        # BUSY-LOCK'u serbest bırak: playlist batch'i bu noktada (başarılı/
        # hatalı fark etmeksizin) tamamen bitmiş sayılıyor, bkz. _on_download'daki
        # kilitleme notu. Hangi batch'in hâlâ "aktif" (self._playlist_batch)
        # olduğuna bakılmaksızın serbest bırakılır — busy-lock sayesinde artık
        # zaten aynı anda birden fazla batch'in yarışması mümkün değil.
        self._set_busy(False)

        # KRİTİK: Sadece hâlâ AKTİF olan batch, bizim bitirdiğimiz batch ile
        # aynıysa sıfırlıyoruz — aksi halde (yukarıdaki docstring'deki senaryo)
        # yeni başlamış bir playlist'in state'ini yanlışlıkla silmiş oluruz.
        # Aynı guard, dashboard temizliği için de geçerli: eğer kullanıcı
        # modal açıkken YENİ bir playlist başlattıysa, o yeni batch'in
        # dashboard'unu (widget'larını) silmemeliyiz.
        if self._playlist_batch is batch:
            self._playlist_batch = None

            if self._dashboard_sync_job is not None:
                self.after_cancel(self._dashboard_sync_job)
                self._dashboard_sync_job = None
            for row in self._playlist_row_widgets.values():
                row["frame"].destroy()
            self._playlist_row_widgets.clear()
            self._playlist_row_data.clear()
            self._dirty_row_ids.clear()
            self.dashboard_frame.grid_remove()

            # Görsel kalabalık düzeltmesinin tersi: playlist modunda gizlediğimiz
            # eski status_label/progress_bar alanını (progress_frame) geri
            # gösteriyoruz — tekil indirme moduna dönüldüğünde bu alan yine
            # kullanılacak.
            self.progress_frame.grid()

            current_width, _ = self._current_geometry_wh()
            base_height = int(self._base_geometry.split("x")[1])
            self._smooth_resize(current_width, base_height)


def run_app() -> None:
    """Uygulamayı başlatır."""
    app = MediaDownloaderApp()
    app.mainloop()