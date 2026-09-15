"""Download History window.

Same pattern as SettingsDialog in settings_dialog.py: a separate
ctk.CTkToplevel that opens/closes without touching the main window.
Thumbnails are lazily loaded on a background thread via
downloader.load_thumbnail().
"""

from __future__ import annotations

import threading
from pathlib import Path
from datetime import datetime
from tkinter import messagebox
from typing import Optional

import customtkinter as ctk
from PIL import Image

from downloader import load_thumbnail
from history_manager import HistoryEntry, HistoryManager
from i18n import I18n
from settings_dialog import open_file_with_default_app, open_folder_in_explorer

_CARD_THUMB_SIZE = (96, 54)


class HistoryDialog(ctk.CTkToplevel):
    """Modal window showing past downloads as a card list."""

    def __init__(self, parent: ctk.CTk, history_manager: HistoryManager, i18n: I18n) -> None:
        super().__init__(parent)

        self._history_manager = history_manager
        self._i18n = i18n
        self._load_token = 0
        self._thumb_image_refs: dict[str, ctk.CTkImage] = {}
        self._all_entries: list[HistoryEntry] = []

        self.title(i18n.t("history_title"))
        self.geometry("580x720")
        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self._refresh_list()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _build_ui(self) -> None:
        t = self._i18n.t
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        filter_bar = ctk.CTkFrame(self, fg_color="transparent")
        filter_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        filter_bar.grid_columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(filter_bar, placeholder_text=t("history_search_placeholder"))
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", lambda _e: self._apply_filters())

        # values is populated dynamically from the real data in _refresh_list().
        self.platform_filter = ctk.CTkOptionMenu(
            filter_bar, values=[t("history_filter_all")], width=140, command=lambda _v: self._apply_filters()
        )
        self.platform_filter.grid(row=0, column=1)

        self.scroll = ctk.CTkScrollableFrame(self)
        self.scroll.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        close_btn = ctk.CTkButton(self, text=t("close"), width=100, command=self.destroy)
        close_btn.grid(row=2, column=0, padx=16, pady=(0, 16), sticky="e")

    def _refresh_list(self) -> None:
        """Reload history from disk, rebuild the platform filter options,
        then redraw according to the current search/filter state.

        Call this after add/delete. When only the search box or filter
        changes, _apply_filters() alone is enough (no disk re-read).
        """
        t = self._i18n.t
        self._all_entries = self._history_manager.list_entries()

        # Filter options are derived from the actual data rather than a
        # fixed list, so no platform is ever missing or shown with no entries.
        platforms = sorted({e.platform or t("history_unknown_platform") for e in self._all_entries})
        all_label = t("history_filter_all")
        current_selection = self.platform_filter.get()
        self.platform_filter.configure(values=[all_label, *platforms])
        if current_selection not in (all_label, *platforms):
            self.platform_filter.set(all_label)

        self._apply_filters()

    def _apply_filters(self) -> None:
        """Applies the search text + platform filter over self._all_entries
        and redraws matching cards. Does not touch disk.
        """
        t = self._i18n.t
        self._load_token += 1
        token = self._load_token

        for child in self.scroll.winfo_children():
            child.destroy()
        self._thumb_image_refs.clear()

        query = self.search_entry.get().strip().lower()
        selected_platform = self.platform_filter.get()
        all_label = t("history_filter_all")
        unknown_label = t("history_unknown_platform")

        filtered = [
            e
            for e in self._all_entries
            if (query in (e.title or "").lower())
            and (selected_platform == all_label or (e.platform or unknown_label) == selected_platform)
        ]

        if not filtered:
            empty_text = t("history_empty") if not self._all_entries else t("history_no_match")
            ctk.CTkLabel(self.scroll, text=empty_text, text_color=("gray40", "gray60")).grid(
                row=0, column=0, pady=40
            )
            return

        for row, entry in enumerate(filtered):
            self._create_card(row, entry, token)

    def _create_card(self, row: int, entry: HistoryEntry, token: int) -> None:
        t = self._i18n.t
        card = ctk.CTkFrame(self.scroll)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8), padx=2)
        card.grid_columnconfigure(1, weight=1)

        thumb_label = ctk.CTkLabel(
            card, text="🎬", width=_CARD_THUMB_SIZE[0], height=_CARD_THUMB_SIZE[1]
        )
        thumb_label.grid(row=0, column=0, rowspan=2, padx=10, pady=10)

        title_label = ctk.CTkLabel(
            card,
            text=entry.title or "-",
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
            wraplength=260,
            justify="left",
        )
        title_label.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(10, 0))

        meta_label = ctk.CTkLabel(
            card,
            text=f"{entry.platform or t('history_unknown_platform')}  •  {self._format_date(entry.downloaded_at)}",
            anchor="w",
            text_color=("gray40", "gray60"),
        )
        meta_label.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 10))

        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.grid(row=0, column=2, rowspan=2, padx=(0, 10), pady=10)

        ctk.CTkButton(
            btn_frame,
            text=t("history_preview"),
            width=130,
            command=lambda e=entry: self._preview(e),
        ).grid(row=0, column=0, pady=(0, 6))

        ctk.CTkButton(
            btn_frame,
            text=t("history_show_in_folder"),
            width=130,
            command=lambda e=entry: self._show_in_folder(e),
        ).grid(row=1, column=0, pady=(0, 6))

        ctk.CTkButton(
            btn_frame,
            text=t("history_delete"),
            width=130,
            fg_color="transparent",
            border_width=1,
            command=lambda e=entry: self._delete_entry(e),
        ).grid(row=2, column=0)

        if entry.thumbnail_url:
            self._load_thumbnail_async(entry, thumb_label, token)

    def _load_thumbnail_async(self, entry: HistoryEntry, label: ctk.CTkLabel, token: int) -> None:
        def worker() -> None:
            image = load_thumbnail(entry.thumbnail_url)
            if image is None:
                return
            image = image.copy()
            image.thumbnail(_CARD_THUMB_SIZE, Image.Resampling.LANCZOS)
            self.after(0, lambda: self._apply_thumbnail(entry.entry_id, label, image, token))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_thumbnail(
        self, entry_id: str, label: ctk.CTkLabel, pil_image: Image.Image, token: int
    ) -> None:
        # The window may have closed or the list may have been refreshed
        # since the thumbnail started loading — ignore stale results.
        if token != self._load_token or not label.winfo_exists():
            return
        ctk_img = ctk.CTkImage(light_image=pil_image, dark_image=pil_image, size=_CARD_THUMB_SIZE)
        self._thumb_image_refs[entry_id] = ctk_img
        label.configure(image=ctk_img, text="")

    def _preview(self, entry: HistoryEntry) -> None:
        file_path = Path(entry.file_path)
        if not file_path.exists():
            messagebox.showwarning(
                self._i18n.t("history_file_not_found_title"),
                self._i18n.t("history_file_not_found_message"),
            )
            return
        open_file_with_default_app(file_path)

    def _show_in_folder(self, entry: HistoryEntry) -> None:
        # Checks whether the FILE itself exists, not just the folder — the
        # folder (e.g. the general Downloads folder) may still exist even
        # after the file was deleted.
        file_path = Path(entry.file_path)
        if not file_path.exists():
            messagebox.showwarning(
                self._i18n.t("history_file_not_found_title"),
                self._i18n.t("history_file_not_found_message"),
            )
            return
        open_folder_in_explorer(file_path.parent)

    def _delete_entry(self, entry: HistoryEntry) -> None:
        self._history_manager.remove_entry(entry.entry_id)
        self._refresh_list()

    @staticmethod
    def _format_date(iso_str: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            return iso_str
