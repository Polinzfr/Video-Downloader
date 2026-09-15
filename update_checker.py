"""yt-dlp güncelleme kontrolü ve güncelleme işlemini yöneten modül.

UI'a bağımlı değil, sadece veri/işlem katmanı — ui.py bunu arka plan
thread'inde çağırıp sonucu `self.after(0, ...)` ile ana thread'e taşıyor.

ÖNEMLİ: yt-dlp burada bir pip PAKETİ olarak import ediliyor (standalone
.exe dağıtımı değil), bu yüzden yt-dlp'nin kendi `-U` CLI özelliği burada
çalışmaz. Doğru güncelleme yolu `pip install --upgrade yt-dlp`.

NOT: Güncelleme başarılı olsa bile çalışan Python sürecinde zaten import
edilmiş olan eski yt_dlp modülü değişmez — uygulamanın yeniden başlatılması
gerekir; burada otomatik bir yeniden başlatma yapılmıyor.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
_REQUEST_TIMEOUT = 8


@dataclass
class UpdateCheckResult:
    """is_update_available()'ın döndürdüğü sonuç."""

    installed_version: Optional[str]
    latest_version: Optional[str]
    update_available: bool
    error: Optional[str] = None


def get_installed_version() -> Optional[str]:
    """Şu an import edilen yt_dlp'nin sürümünü döner; okunamıyorsa None."""
    try:
        import yt_dlp

        return yt_dlp.version.__version__
    except Exception as exc:
        print(f"[update_checker] Kurulu yt-dlp sürümü okunamadı: {exc!r}")
        return None


def _fetch_latest_version() -> Optional[str]:
    """PyPI JSON API'sinden yt-dlp'nin en güncel yayınlanan sürümünü çeker."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=_REQUEST_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("info", {}).get("version")
    except Exception as exc:
        print(f"[update_checker] PyPI'den güncel sürüm sorgulanamadı: {exc!r}")
        return None


def is_update_available() -> UpdateCheckResult:
    """Kurulu ve PyPI'deki en güncel sürümü karşılaştırır.

    yt-dlp sürümleri 'YYYY.MM.DD' formatında olduğundan düz string
    karşılaştırması kronolojik sırayla örtüşüyor — ekstra bağımlılık
    gerekmiyor.
    """
    installed = get_installed_version()
    latest = _fetch_latest_version()

    if installed is None or latest is None:
        return UpdateCheckResult(
            installed_version=installed,
            latest_version=latest,
            update_available=False,
            error="Sürüm bilgisi alınamadı (ağ sorunu veya PyPI erişilemedi olabilir).",
        )

    return UpdateCheckResult(
        installed_version=installed,
        latest_version=latest,
        update_available=(latest != installed),
    )


def run_update() -> tuple[bool, str]:
    """`pip install --upgrade yt-dlp` çalıştırır. (başarılı_mı, log_metni) döner."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        log = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, log
    except Exception as exc:
        return False, f"Güncelleme çalıştırılırken beklenmeyen bir hata oluştu: {exc!r}"
