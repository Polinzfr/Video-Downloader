"""Application settings window."""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import customtkinter as ctk

from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional

from config import AppSettings, save_settings
from i18n import AUDIO_FORMATS, I18n, VIDEO_FORMATS
import sound_notifier

# Display <-> internal value mapping for the 'Cookie / Session Source'
# dropdown. Internal values must match providers.py/config.py exactly.
_COOKIE_SOURCE_LABELS = {
    "auto": "Otomatik (Önerilen)",
    "disabled": "Devre Dışı",
    "firefox": "Firefox",
    "brave": "Brave",
    "chrome": "Chrome",
    "edge": "Edge",
    "file": "Manuel cookies.txt Dosyası",
}
_COOKIE_SOURCE_LABELS_REVERSE = {v: k for k, v in _COOKIE_SOURCE_LABELS.items()}

# Display <-> internal value mapping for the color theme dropdown. Internal
# values must match CustomTkinter's own set_default_color_theme() values.
_COLOR_THEME_LABELS = {
    "blue": "Mavi",
    "green": "Yeşil",
    "dark-blue": "Koyu Mavi",
}
_COLOR_THEME_LABELS_REVERSE = {v: k for k, v in _COLOR_THEME_LABELS.items()}


class CTkHoverTooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None

        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text:
            return

        x = self.widget.winfo_rootx() + 15
        y = self.widget.winfo_rooty() + 25

        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            self.tip_window,
            text=self.text,
            justify="left",
            background="#2b2b2b",
            foreground="#ffffff",
            relief="flat",
            padx=8,
            pady=5,
            font=("Segoe UI", 10),
        )
        label.pack()

    def hide_tip(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


class SettingsDialog(ctk.CTkToplevel):
    """Modal window for editing persistent settings."""

    def destroy(self) -> None:
        # The PowerShell/MediaPlayer process started by the 🔊 preview
        # button runs fully detached — closing the window alone wouldn't
        # stop it (it would keep playing on its own for up to 30s).
        # Overriding destroy() covers every closing path (X button,
        # Cancel, Save) from a single place.
        sound_notifier.stop()
        super().destroy()

    def __init__(
        self,
        parent: ctk.CTk,
        settings: AppSettings,
        i18n: I18n,
        on_save: Callable[[AppSettings], None],
    ) -> None:
        super().__init__(parent)

        self._settings = settings
        self._i18n = i18n
        self._on_save = on_save
        self._saved = False
        self._background_image_path = ""  # filled in by _load_values()

        self.title(i18n.t("settings_title"))
        self.geometry("520x700")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self._load_values()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _build_ui(self) -> None:
        """Builds the settings window widgets."""
        self.grid_columnconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(self)
        scroll.grid(row=0, column=0, padx=20, pady=(20, 12), sticky="nsew")

        scroll.grid_columnconfigure(0, weight=0)  # left column: labels
        scroll.grid_columnconfigure(1, weight=0)  # middle column: (?) tooltips
        scroll.grid_columnconfigure(2, weight=1)  # right column: inputs/menus
        self.grid_rowconfigure(0, weight=1)

        t = self._i18n.t
        row = 0

        self._section(scroll, row, t("appearance"))
        row += 1

        self._label(scroll, row, t("theme"))
        self.theme_menu = ctk.CTkOptionMenu(
            scroll,
            values=[t("theme_system"), t("theme_dark"), t("theme_light")],
        )
        self.theme_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._label(scroll, row, t("language"))
        self.lang_menu = ctk.CTkOptionMenu(
            scroll,
            values=[t("lang_tr"), t("lang_en"), t("lang_es"), t("lang_de")],
        )
        self.lang_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._label(scroll, row, t("color_theme"))
        self.color_theme_menu = ctk.CTkOptionMenu(
            scroll,
            values=list(_COLOR_THEME_LABELS.values()),
        )
        self.color_theme_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._section(scroll, row, t("downloads"))
        row += 1

        self._label(scroll, row, t("default_format"))
        self.format_menu = ctk.CTkOptionMenu(
            scroll,
            values=VIDEO_FORMATS + AUDIO_FORMATS,
            command=self._on_format_change,
        )
        self.format_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._label(scroll, row, t("default_quality"))
        self.quality_menu = ctk.CTkOptionMenu(
            scroll,
            values=self._i18n.video_quality_options(),
        )
        self.quality_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._label(scroll, row, t("default_audio_quality"))
        self.audio_quality_menu = ctk.CTkOptionMenu(
            scroll,
            values=["En İyi Ses", "320 kbps", "256 kbps", "192 kbps", "128 kbps", "64 kbps", "32 kbps"],
        )
        self.audio_quality_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._label(scroll, row, t("default_folder"))
        folder_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        folder_frame.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        folder_frame.grid_columnconfigure(0, weight=1)

        self.folder_entry = ctk.CTkEntry(folder_frame)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        ctk.CTkButton(
            folder_frame,
            text=t("select"),
            width=70,
            command=self._browse_folder,
        ).grid(row=0, column=1)
        row += 1

        # CTkSwitch used for visual consistency with notify_check's style
        # family — functionally identical to the previous CTkCheckBox.
        self.open_folder_var = ctk.BooleanVar(value=False)
        self.open_folder_check = ctk.CTkSwitch(
            scroll,
            text=t("open_folder_after"),
            variable=self.open_folder_var,
        )
        self.open_folder_check.grid(row=row, column=0, columnspan=3, pady=8, sticky="w")
        row += 1

        self.embed_thumb_var = ctk.BooleanVar(value=True)
        self.embed_thumb_check = ctk.CTkCheckBox(
            scroll,
            text=t("embed_thumbnail"),
            variable=self.embed_thumb_var,
        )
        self.embed_thumb_check.grid(row=row, column=0, pady=4, sticky="w")

        self.thumb_info = ctk.CTkLabel(scroll, text="?", font=ctk.CTkFont(size=13, weight="bold"), text_color="#1f538d", cursor="hand2")
        self.thumb_info.grid(row=row, column=1, padx=(5, 0), pady=4, sticky="w")
        CTkHoverTooltip(self.thumb_info, t("embed_thumbnail_desc"))
        row += 1

        self._label(scroll, row, t("filename_template"))

        self.template_info = ctk.CTkLabel(scroll, text="?", font=ctk.CTkFont(size=13, weight="bold"), text_color="#1f538d", cursor="hand2")
        self.template_info.grid(row=row, column=1, padx=(5, 0), pady=6, sticky="w")
        CTkHoverTooltip(self.template_info, t("filename_template_desc"))

        self.template_entry = ctk.CTkEntry(scroll, placeholder_text="%(title)s")
        self.template_entry.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self._label(scroll, row, t("concurrent_fragments"))

        self.fragments_info = ctk.CTkLabel(scroll, text="?", font=ctk.CTkFont(size=13, weight="bold"), text_color="#1f538d", cursor="hand2")
        self.fragments_info.grid(row=row, column=1, padx=(5, 0), pady=6, sticky="w")
        CTkHoverTooltip(self.fragments_info, t("concurrent_fragments_desc"))

        self.fragments_slider = ctk.CTkSlider(scroll, from_=1, to=8, number_of_steps=7)
        self.fragments_slider.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        self.fragments_label = ctk.CTkLabel(scroll, text="4")
        self.fragments_label.grid(row=row, column=3, padx=(8, 0), pady=6)
        self.fragments_slider.configure(command=self._on_fragment_change)
        row += 1

        self._label(scroll, row, t("concurrent_downloads"))

        self.downloads_info = ctk.CTkLabel(scroll, text="?", font=ctk.CTkFont(size=13, weight="bold"), text_color="#1f538d", cursor="hand2")
        self.downloads_info.grid(row=row, column=1, padx=(5, 0), pady=6, sticky="w")
        CTkHoverTooltip(self.downloads_info, t("concurrent_downloads_desc"))

        self.downloads_slider = ctk.CTkSlider(scroll, from_=1, to=5, number_of_steps=4)
        self.downloads_slider.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        self.downloads_label = ctk.CTkLabel(scroll, text="2")
        self.downloads_label.grid(row=row, column=3, padx=(8, 0), pady=6)
        self.downloads_slider.configure(command=self._on_downloads_change)
        row += 1

        self._label(scroll, row, t("speed_limit_label"))

        self.speed_limit_info = ctk.CTkLabel(scroll, text="?", font=ctk.CTkFont(size=13, weight="bold"), text_color="#1f538d", cursor="hand2")
        self.speed_limit_info.grid(row=row, column=1, padx=(5, 0), pady=6, sticky="w")
        CTkHoverTooltip(self.speed_limit_info, t("speed_limit_desc"))

        self.speed_limit_entry = ctk.CTkEntry(scroll, placeholder_text=t("speed_limit_placeholder"))
        self.speed_limit_entry.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        self.download_subtitles_var = ctk.BooleanVar(value=False)
        self.download_subtitles_check = ctk.CTkCheckBox(
            scroll, text=t("download_subtitles_label"), variable=self.download_subtitles_var,
            command=self._on_download_subtitles_toggle,
        )
        self.download_subtitles_check.grid(row=row, column=0, columnspan=2, pady=4, sticky="w")
        row += 1

        self._label(scroll, row, t("subtitle_langs_label"))
        self.subtitle_langs_entry = ctk.CTkEntry(scroll, placeholder_text=t("subtitle_langs_placeholder"))
        self.subtitle_langs_entry.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        self.subtitle_langs_entry.configure(state="disabled")
        row += 1

        self._section(scroll, row, t("appearance_extra"))
        row += 1

        self._label(scroll, row, t("background_image"))
        bg_image_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        bg_image_frame.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        bg_image_frame.grid_columnconfigure(0, weight=1)

        self.background_image_label = ctk.CTkLabel(
            bg_image_frame, text=t("none_value"), anchor="w", text_color=("gray40", "gray60")
        )
        self.background_image_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            bg_image_frame, text=t("select"), width=70, command=self._browse_background_image
        ).grid(row=0, column=1, padx=(0, 4))
        ctk.CTkButton(
            bg_image_frame,
            text=t("reset_to_default"),
            width=70,
            fg_color="transparent",
            border_width=1,
            command=self._reset_background_image,
        ).grid(row=0, column=2)
        row += 1

        notify_lbl = t("notify_on_complete")
        self.notify_var = ctk.BooleanVar(value=True)
        self.notify_check = ctk.CTkCheckBox(scroll, text=notify_lbl, variable=self.notify_var)
        self.notify_check.grid(row=row, column=0, columnspan=3, pady=8, sticky="w")
        row += 1

        self.minimize_to_tray_var = ctk.BooleanVar(value=True)
        self.minimize_to_tray_check = ctk.CTkCheckBox(
            scroll, text=t("minimize_to_tray_label"), variable=self.minimize_to_tray_var
        )
        self.minimize_to_tray_check.grid(row=row, column=0, columnspan=3, pady=8, sticky="w")
        row += 1

        # Independent of notify_on_complete (popup) — the user may want the
        # sound without the popup, or vice versa.
        self.sound_var = ctk.BooleanVar(value=True)
        self.sound_check = ctk.CTkCheckBox(scroll, text=t("sound_notifications"), variable=self.sound_var)
        self.sound_check.grid(row=row, column=0, columnspan=3, pady=8, sticky="w")
        row += 1

        # Lists Windows' own system sounds in a dropdown, plus a custom file
        # picker and an instant 🔊 preview button.
        self._system_default_label = t("sound_system_default")
        self._windows_sounds: dict[str, str] = dict(sound_notifier.list_windows_system_sounds())
        self._custom_sound_labels: dict[str, str] = {}
        self._success_sound_path = ""
        self._error_sound_path = ""

        row = self._build_sound_picker(
            scroll, row, label=t("success_sound_label"), attr_prefix="success", play_fn=sound_notifier.play_success
        )
        row = self._build_sound_picker(
            scroll, row, label=t("error_sound_label"), attr_prefix="error", play_fn=sound_notifier.play_error
        )

        self._section(scroll, row, t("cookie_section"))
        row += 1

        self._label(scroll, row, t("cookie_source"))
        self.cookie_source_menu = ctk.CTkOptionMenu(
            scroll,
            values=list(_COOKIE_SOURCE_LABELS.values()),
            command=self._on_cookie_source_change,
        )
        self.cookie_source_menu.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        row += 1

        # File picker row, active only when "Manual cookies.txt File" is
        # selected — hidden by default, toggled by _on_cookie_source_change.
        self.cookie_file_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        self.cookie_file_frame.grid(row=row, column=0, columnspan=3, padx=0, pady=(0, 6), sticky="ew")
        self.cookie_file_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.cookie_file_frame, text="cookies.txt:", anchor="w", width=90).grid(
            row=0, column=0, sticky="w"
        )
        self.cookie_file_entry = ctk.CTkEntry(self.cookie_file_frame)
        self.cookie_file_entry.grid(row=0, column=1, sticky="ew", padx=(4, 8))
        ctk.CTkButton(
            self.cookie_file_frame,
            text=t("select"),
            width=70,
            command=self._browse_cookie_file,
        ).grid(row=0, column=2)
        self.cookie_file_frame.grid_remove()  # hidden by default
        row += 1

        ctk.CTkLabel(
            scroll,
            text=t("cookie_explanation"),
            justify="left",
            anchor="w",
            text_color=("gray40", "gray60"),
            wraplength=420,
        ).grid(row=row, column=0, columnspan=3, pady=(0, 8), sticky="w")
        row += 1

        self._section(scroll, row, t("supported_sites"))
        row += 1

        ctk.CTkLabel(
            scroll,
            text=t("supported_sites_hint"),
            justify="left",
            anchor="w",
            text_color=("gray40", "gray60"),
            wraplength=420,
        ).grid(row=row, column=0, columnspan=3, pady=(4, 8), sticky="w")
        row += 1

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="e")

        ctk.CTkButton(
            btn_frame,
            text=t("cancel"),
            width=100,
            fg_color="transparent",
            border_width=1,
            command=self.destroy,
        ).grid(row=0, column=0, padx=(0, 8))

        ctk.CTkButton(
            btn_frame,
            text=t("save"),
            width=100,
            command=self._save,
        ).grid(row=0, column=1)

    @staticmethod
    def _section(parent: ctk.CTkScrollableFrame, row: int, text: str) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).grid(row=row, column=0, columnspan=3, pady=(12, 4), sticky="w")

    @staticmethod
    def _label(parent: ctk.CTkScrollableFrame, row: int, text: str) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w").grid(
            row=row, column=0, pady=6, sticky="w"
        )

    def _build_sound_picker(self, parent, row: int, label: str, attr_prefix: str, play_fn) -> int:
        """Builds a sound picker row plus a volume row below it: dropdown
        (system default + Windows' own sounds) + custom file browse +
        instant 🔊 preview (plays at the selected volume) + slider.

        attr_prefix is 'success' or 'error' — creates/uses
        self._{prefix}_sound_path, self._{prefix}_sound_volume and
        self.{prefix}_sound_menu under that name (see
        _on_sound_menu_selected/_set_sound_selection/_on_volume_changed/
        _set_volume, which use the same prefix).
        """
        t = self._i18n.t
        self._label(parent, row, label)

        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        values = [self._system_default_label, *self._windows_sounds.keys()]
        menu = ctk.CTkOptionMenu(
            frame,
            values=values,
            width=160,
            command=lambda choice, p=attr_prefix: self._on_sound_menu_selected(p, choice),
        )
        menu.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        setattr(self, f"{attr_prefix}_sound_menu", menu)

        ctk.CTkButton(
            frame, text=t("sound_browse"), width=60, command=lambda p=attr_prefix: self._browse_sound_file(p)
        ).grid(row=0, column=1, padx=(0, 6))

        ctk.CTkButton(
            frame,
            text="🔊",
            width=32,
            command=lambda p=attr_prefix, fn=play_fn: fn(
                getattr(self, f"_{p}_sound_path"), getattr(self, f"_{p}_sound_volume")
            ),
        ).grid(row=0, column=2)

        row += 1

        # The slider has no effect while "System Default" is selected —
        # MessageBeep has no volume parameter (see
        # sound_notifier.py:_play_system_default). It's still always shown
        # (not disabled) since the user might pick a custom file, then
        # switch back to System Default — the slider position isn't lost,
        # it's just inactive at that point.
        self._label(parent, row, f"    {t('sound_volume_label')}")

        vol_frame = ctk.CTkFrame(parent, fg_color="transparent")
        vol_frame.grid(row=row, column=2, padx=(12, 0), pady=6, sticky="ew")
        vol_frame.grid_columnconfigure(0, weight=1)

        setattr(self, f"_{attr_prefix}_sound_volume", 1.0)

        vol_label = ctk.CTkLabel(vol_frame, text="100%", width=42, anchor="e")
        vol_label.grid(row=0, column=1, padx=(6, 0))
        setattr(self, f"{attr_prefix}_volume_label", vol_label)

        slider = ctk.CTkSlider(
            vol_frame,
            from_=0,
            to=100,
            number_of_steps=100,
            command=lambda value, p=attr_prefix: self._on_volume_changed(p, value),
        )
        slider.set(100)
        slider.grid(row=0, column=0, sticky="ew")
        setattr(self, f"{attr_prefix}_volume_slider", slider)

        return row + 1

    def _on_volume_changed(self, prefix: str, value: float) -> None:
        volume = round(value) / 100.0
        setattr(self, f"_{prefix}_sound_volume", volume)
        getattr(self, f"{prefix}_volume_label").configure(text=f"{round(value)}%")

    def _set_volume(self, prefix: str, volume: float) -> None:
        """Called by _load_values() — shows a saved volume (0.0-1.0) on the
        matching slider and label.
        """
        volume = max(0.0, min(1.0, volume))
        setattr(self, f"_{prefix}_sound_volume", volume)
        getattr(self, f"{prefix}_volume_slider").set(volume * 100)
        getattr(self, f"{prefix}_volume_label").configure(text=f"{round(volume * 100)}%")

    def _on_sound_menu_selected(self, prefix: str, choice: str) -> None:
        if choice == self._system_default_label:
            path = ""
        elif choice in self._windows_sounds:
            path = self._windows_sounds[choice]
        elif choice in self._custom_sound_labels:
            path = self._custom_sound_labels[choice]
        else:
            path = ""
        setattr(self, f"_{prefix}_sound_path", path)

    def _browse_sound_file(self, prefix: str) -> None:
        t = self._i18n.t
        path = filedialog.askopenfilename(
            title=t("sound_file_dialog_title"),
            filetypes=[
                ("Audio (WAV/MP3)", "*.wav *.mp3"),
                ("WAV", "*.wav"),
                ("MP3", "*.mp3"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        display_name = f"{t('sound_custom_prefix')} {Path(path).name}"
        menu = getattr(self, f"{prefix}_sound_menu")
        current_values = list(menu.cget("values"))
        if display_name not in current_values:
            current_values.append(display_name)
            menu.configure(values=current_values)
        menu.set(display_name)

        self._custom_sound_labels[display_name] = path
        setattr(self, f"_{prefix}_sound_path", path)

    def _set_sound_selection(self, prefix: str, saved_path: str) -> None:
        """Called by _load_values() — tries to show the saved path
        (from config.json) as selected in the matching dropdown: the
        matching name if it's one of the Windows system sounds, or
        '<custom prefix> <filename>' if it's a previously-picked custom
        file, or the system-default label if no path is saved.
        """
        menu = getattr(self, f"{prefix}_sound_menu")

        if not saved_path:
            menu.set(self._system_default_label)
            setattr(self, f"_{prefix}_sound_path", "")
            return

        for display_name, path in self._windows_sounds.items():
            if path == saved_path:
                menu.set(display_name)
                setattr(self, f"_{prefix}_sound_path", saved_path)
                return

        display_name = f"{self._i18n.t('sound_custom_prefix')} {Path(saved_path).name}"
        current_values = list(menu.cget("values"))
        if display_name not in current_values:
            current_values.append(display_name)
            menu.configure(values=current_values)
        menu.set(display_name)
        self._custom_sound_labels[display_name] = saved_path
        setattr(self, f"_{prefix}_sound_path", saved_path)

    def _on_fragment_change(self, value: float) -> None:
        self.fragments_label.configure(text=str(int(value)))

    def _on_downloads_change(self, value: float) -> None:
        self.downloads_label.configure(text=str(int(value)))

    def _on_download_subtitles_toggle(self) -> None:
        state = "normal" if self.download_subtitles_var.get() else "disabled"
        self.subtitle_langs_entry.configure(state=state)

    def _on_cookie_source_change(self, choice: str) -> None:
        """Shows/hides the file picker row when the cookie source dropdown changes."""
        if choice == _COOKIE_SOURCE_LABELS["file"]:
            self.cookie_file_frame.grid()
        else:
            self.cookie_file_frame.grid_remove()

    def _on_format_change(self, choice: str) -> None:
        """Locks/unlocks quality menus based on the selected format."""
        is_audio = choice.lower() in [fmt.lower() for fmt in AUDIO_FORMATS]

        if is_audio:
            self.quality_menu.configure(state="disabled")
            self.audio_quality_menu.configure(state="normal")
        else:
            self.quality_menu.configure(state="normal")
            self.audio_quality_menu.configure(state="disabled")

    def _browse_folder(self) -> None:
        folder = filedialog.askdirectory(
            title=self._i18n.t("select_folder_title"),
            initialdir=self.folder_entry.get() or str(Path.home() / "Downloads"),
        )
        if folder:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, folder)

    def _browse_cookie_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select cookies.txt",
            filetypes=[("Cookies file", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.cookie_file_entry.delete(0, "end")
            self.cookie_file_entry.insert(0, path)

    def _browse_background_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select background image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg"), ("All files", "*.*")],
        )
        if path:
            self._background_image_path = path
            self.background_image_label.configure(text=Path(path).name)

    def _reset_background_image(self) -> None:
        self._background_image_path = ""
        self.background_image_label.configure(text=self._i18n.t("none_value"))

    def _load_values(self) -> None:
        """Loads current settings into the form."""
        t = self._i18n.t
        theme_map = {
            "system": t("theme_system"),
            "dark": t("theme_dark"),
            "light": t("theme_light"),
        }
        self.theme_menu.set(theme_map.get(self._settings.theme, t("theme_system")))

        lang_map = {
            "tr": t("lang_tr"),
            "en": t("lang_en"),
            "de": t("lang_de"),
            "es": t("lang_es"),
        }
        self.lang_menu.set(lang_map.get(self._settings.language, t("lang_en")))

        self.format_menu.set(self._settings.default_format)

        self.quality_menu.set(
            self._i18n.quality_label_from_key(self._settings.default_quality_key)
        )

        # Falls back to "audio_320" if default_audio_quality_key is missing.
        saved_audio = getattr(self._settings, "default_audio_quality_key", "audio_320")
        self.audio_quality_menu.set(self._i18n.t(saved_audio))

        self._on_format_change(self._settings.default_format)

        self.folder_entry.insert(0, self._settings.output_dir)
        self.open_folder_var.set(self._settings.open_folder_after_download)
        self.embed_thumb_var.set(self._settings.embed_thumbnail)
        self.template_entry.insert(0, self._settings.filename_template)
        self.fragments_slider.set(self._settings.concurrent_fragments)
        self.fragments_label.configure(text=str(self._settings.concurrent_fragments))

        self.downloads_slider.set(self._settings.concurrent_downloads)
        self.downloads_label.configure(text=str(self._settings.concurrent_downloads))

        speed_limit = getattr(self._settings, "speed_limit_kbps", 0)
        self.speed_limit_entry.insert(0, str(speed_limit) if speed_limit else "")

        download_subtitles = getattr(self._settings, "download_subtitles", False)
        self.download_subtitles_var.set(download_subtitles)
        subtitle_langs = getattr(self._settings, "subtitle_langs", []) or []
        self.subtitle_langs_entry.insert(0, ",".join(subtitle_langs))
        self._on_download_subtitles_toggle()  # set correct initial enabled/disabled state

        cookie_label = _COOKIE_SOURCE_LABELS.get(
            self._settings.cookie_source, _COOKIE_SOURCE_LABELS["auto"]
        )
        self.cookie_source_menu.set(cookie_label)
        self.cookie_file_entry.insert(0, self._settings.cookie_file_path)
        self._on_cookie_source_change(cookie_label)  # set correct initial visibility

        self.color_theme_menu.set(
            _COLOR_THEME_LABELS.get(self._settings.color_theme, _COLOR_THEME_LABELS["blue"])
        )

        self._background_image_path = self._settings.background_image_path
        self.background_image_label.configure(
            text=Path(self._background_image_path).name if self._background_image_path else t("none_value")
        )

        self.notify_var.set(self._settings.notify_on_complete)
        self.minimize_to_tray_var.set(getattr(self._settings, "minimize_to_tray", True))
        self.sound_var.set(self._settings.sound_notifications)
        self._set_sound_selection("success", self._settings.success_sound_path)
        self._set_sound_selection("error", self._settings.error_sound_path)
        self._set_volume("success", self._settings.success_sound_volume)
        self._set_volume("error", self._settings.error_sound_volume)

    def _save(self) -> None:
        """Saves the form values and updates the main window."""
        t = self._i18n.t
        theme_reverse = {
            t("theme_system"): "system",
            t("theme_dark"): "dark",
            t("theme_light"): "light",
        }

        lang_reverse = {
            t("lang_tr"): "tr",
            t("lang_en"): "en",
            t("lang_de"): "de",
            t("lang_es"): "es",
        }
        lang = lang_reverse.get(self.lang_menu.get(), "en")

        audio_val = self.audio_quality_menu.get()
        if "320" in audio_val:
            audio_key = "audio_320"
        elif "256" in audio_val:
            audio_key = "audio_256"
        elif "192" in audio_val:
            audio_key = "audio_192"
        elif "128" in audio_val:
            audio_key = "audio_128"
        elif "64" in audio_val:
            audio_key = "audio_64"
        elif "32" in audio_val:
            audio_key = "audio_32"
        else:
            audio_key = "audio_320"  # "Best Audio" selected, or fallback

        try:
            speed_limit_kbps = int(self.speed_limit_entry.get().strip() or "0")
        except ValueError:
            speed_limit_kbps = 0
        speed_limit_kbps = max(0, speed_limit_kbps)

        subtitle_langs = [
            lang.strip() for lang in self.subtitle_langs_entry.get().split(",") if lang.strip()
        ]

        updated = AppSettings(
            theme=theme_reverse.get(self.theme_menu.get(), "system"),
            language=lang,
            output_dir=self.folder_entry.get().strip() or str(Path.home() / "Downloads"),
            default_format=self.format_menu.get(),
            default_quality_key=self._i18n.quality_key_from_label(self.quality_menu.get()),
            default_audio_quality_key=audio_key,
            open_folder_after_download=self.open_folder_var.get(),
            embed_thumbnail=self.embed_thumb_var.get(),
            filename_template=self.template_entry.get().strip() or "%(title)s",
            concurrent_fragments=int(self.fragments_slider.get()),
            concurrent_downloads=int(self.downloads_slider.get()),
            speed_limit_kbps=speed_limit_kbps,
            download_subtitles=self.download_subtitles_var.get(),
            subtitle_langs=subtitle_langs,
            cookie_source=_COOKIE_SOURCE_LABELS_REVERSE.get(
                self.cookie_source_menu.get(), "auto"
            ),
            cookie_file_path=self.cookie_file_entry.get().strip(),
            color_theme=_COLOR_THEME_LABELS_REVERSE.get(self.color_theme_menu.get(), "blue"),
            background_image_path=self._background_image_path,
            notify_on_complete=self.notify_var.get(),
            minimize_to_tray=self.minimize_to_tray_var.get(),
            sound_notifications=self.sound_var.get(),
            success_sound_path=self._success_sound_path,
            error_sound_path=self._error_sound_path,
            success_sound_volume=self._success_sound_volume,
            error_sound_volume=self._error_sound_volume,
        )

        color_theme_changed = updated.color_theme != self._settings.color_theme

        save_settings(updated)
        self._on_save(updated)
        if color_theme_changed:
            # CustomTkinter's set_default_color_theme() is only read at
            # startup; changing it at runtime doesn't update existing
            # widgets (a known CTk limitation) — hence the restart notice
            # instead of applying it live.
            messagebox.showinfo(
                self._i18n.t("settings_title"),
                self._i18n.t("settings_saved") + "\n\n" + self._i18n.t("color_theme_restart_notice"),
            )
        else:
            messagebox.showinfo(self._i18n.t("settings_title"), self._i18n.t("settings_saved"))
        self.destroy()


def open_folder_in_explorer(path: Path) -> None:
    """Opens the download folder in the OS's file manager."""
    folder = str(path.resolve())
    if sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", folder], check=False)
    else:
        subprocess.run(["xdg-open", folder], check=False)


def open_file_with_default_app(path: Path) -> None:
    """Opens a file with the OS's default application.

    Same platform-specific mechanism as open_folder_in_explorer — the only
    difference is the target is a FILE, not a folder. For video files this
    typically opens Films & TV/Media Player on Windows, QuickTime on macOS,
    or whatever xdg-open resolves to on Linux. Used for the History window's
    preview button instead of an embedded player, to avoid a new dependency.
    """
    file_path = str(path.resolve())
    if sys.platform == "win32":
        os.startfile(file_path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", file_path], check=False)
    else:
        subprocess.run(["xdg-open", file_path], check=False)
