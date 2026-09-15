"""Batch link download window.

Same pattern as settings_dialog.py/history_dialog.py: a separate
ctk.CTkToplevel that doesn't touch the main window's single-download flow.

This dialog is deliberately "dumb": it has none of the download logic
(DownloadTask/TaskQueueManager/format-quality selection) — it only parses
URLs out of pasted text and hands them to the on_urls_submitted callback
as a list. The actual task-creation/enqueue logic lives in one place in
ui.py (_on_batch_urls_submitted).
"""

from __future__ import annotations

import re
from typing import Callable

import customtkinter as ctk

from i18n import I18n

_URL_PATTERN = re.compile(r"https?://\S+")


def parse_urls(raw_text: str) -> list[str]:
    """Extracts URLs from free-form text (one per line or comma-separated).

    Order is preserved, exact-duplicate strings are removed. Platform
    support is not checked here — that's ProviderRegistry.resolve()'s job;
    an unsupported URL is simply marked FAILED by task_queue and reported
    through the normal error flow.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _URL_PATTERN.findall(raw_text.replace(",", "\n")):
        url = match.strip()
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


class BatchDialog(ctk.CTkToplevel):
    """Window that collects a batch of URLs from a multi-line paste area."""

    def __init__(
        self, parent: ctk.CTk, i18n: I18n, on_urls_submitted: Callable[[list[str]], None]
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._on_urls_submitted = on_urls_submitted

        self.title(i18n.t("batch_title"))
        self.geometry("520x480")
        self.transient(parent)
        self.grab_set()

        self._build_ui()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _build_ui(self) -> None:
        t = self._i18n.t
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text=t("batch_instructions"), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=16, pady=(16, 6)
        )

        self.textbox = ctk.CTkTextbox(self, wrap="none")
        self.textbox.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 6))
        self.textbox.bind("<KeyRelease>", self._update_count)
        # <<Paste>> is also listened for since a mouse-based paste may not
        # trigger <KeyRelease>; after(10, ...) waits for the content to update.
        self.textbox.bind("<<Paste>>", lambda _e: self.after(10, self._update_count))

        self.count_label = ctk.CTkLabel(self, text=t("batch_count", count=0), text_color=("gray40", "gray60"))
        self.count_label.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 10))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, sticky="e", padx=16, pady=(0, 16))

        ctk.CTkButton(
            btn_frame, text=t("cancel"), fg_color="transparent", border_width=1, command=self.destroy
        ).grid(row=0, column=0, padx=(0, 8))
        self.submit_btn = ctk.CTkButton(btn_frame, text=t("batch_add_to_queue"), command=self._submit)
        self.submit_btn.grid(row=0, column=1)

    def _update_count(self, _event=None) -> None:
        urls = parse_urls(self.textbox.get("1.0", "end"))
        self.count_label.configure(
            text=self._i18n.t("batch_count", count=len(urls)), text_color=("gray40", "gray60")
        )

    def _submit(self) -> None:
        urls = parse_urls(self.textbox.get("1.0", "end"))
        if not urls:
            self.count_label.configure(text=self._i18n.t("batch_no_valid_links"), text_color="orange")
            return
        self._on_urls_submitted(urls)
        self.destroy()
