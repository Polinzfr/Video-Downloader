"""Uygulama ayarlarını yöneten yapılandırma modülü."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


CONFIG_DIR = Path.home() / ".medya_indirici"
CONFIG_FILE = CONFIG_DIR / "settings.json"


@dataclass
class AppSettings:
    """Kalıcı uygulama ayarları."""

    theme: str = "system"
    language: str = "tr"
    output_dir: str = ""
    default_format: str = "MP4"
    default_quality_key: str = "quality_best"
    default_audio_quality_key: str = "audio_320"
    open_folder_after_download: bool = False
    embed_thumbnail: bool = True
    filename_template: str = "%(title)s"
    concurrent_fragments: int = 4

    # Aynı anda kaç videonun paralel indirileceği (TaskQueueManager worker sayısı).
    # concurrent_fragments'tan farklı: o tek bir videonun parça paralelliği.
    concurrent_downloads: int = 2

    # 'auto' (Firefox->Brave->Chrome->Edge sırayla), 'disabled',
    # 'firefox'/'brave'/'chrome'/'edge', veya 'file' (cookie_file_path kullanılır).
    cookie_source: str = "auto"
    cookie_file_path: str = ""

    # CustomTkinter renk teması. Sadece uygulama başlarken okunur; çalışırken
    # değiştirmek mevcut widget'ları güncellemez, bu yüzden ayar değişince
    # settings_dialog.py "yeniden başlat" uyarısı gösterir.
    color_theme: str = "blue"

    # Ana pencere arka planında gösterilecek özel resmin yolu.
    background_image_path: str = ""

    # İndirme tamamlanınca uygulama içi popup gösterilsin mi.
    notify_on_complete: bool = True

    # Panoya kopyalanan bilinen platform linklerinin otomatik algılanması.
    clipboard_autodetect: bool = False

    # İndirme COMPLETED/FAILED olduğunda ses çalınsın mı (notify_on_complete'ten bağımsız).
    sound_notifications: bool = True

    success_sound_path: str = ""
    error_sound_path: str = ""

    # 0.0-1.0 arası ses seviyesi çarpanı. Sadece özel ses dosyası seçiliyken geçerli.
    success_sound_volume: float = 1.0
    error_sound_volume: float = 1.0

    # Varsayılan olarak altyazı indirilsin mi (bkz. task_queue.py DownloadTask
    # / downloader.py download_media). Kapalıyken subtitle_langs kullanılmaz.
    download_subtitles: bool = False
    # Boşsa downloader.py kendi varsayılanına (["en"]) düşer.
    subtitle_langs: list[str] = field(default_factory=list)

    # 0 = sınırsız. Aksi halde yt-dlp'ye KB/s cinsinden 'ratelimit' olarak geçer.
    speed_limit_kbps: int = 0

    # Pencere kapatma (X) butonuna basınca uygulamadan tamamen çıkmak yerine
    # sistem tepsisine küçültülsün mü (bkz. ui.py _on_close). pystray kurulu
    # değilse bu ayarın hiçbir etkisi olmaz, normal kapatma davranışına düşülür.
    minimize_to_tray: bool = True

    def __post_init__(self) -> None:
        if not self.output_dir:
            self.output_dir = str(Path.home() / "Downloads")


def load_settings() -> AppSettings:
    """Diskten ayarları yükler; dosya yoksa varsayılanları döndürür."""
    if not CONFIG_FILE.exists():
        return AppSettings()

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        known = {f.name for f in AppSettings.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return AppSettings(**filtered)
    except (json.JSONDecodeError, TypeError, ValueError):
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    """Ayarları diske kaydeder."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(asdict(settings), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
