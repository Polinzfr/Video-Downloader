"""Medya İndirme ve Dönüştürme uygulamasının giriş noktası."""

import datetime
import sys
from pathlib import Path

from ui import run_app

LOG_DIR = Path.home() / ".medya_indirici" / "logs"


class _TeeStream:
    """Bir stream'e (stdout/stderr) yazılan her şeyi AYNI ZAMANDA bir log
    dosyasına da yazar. Uygulama genelinde print() ile atılan hata/teşhis
    mesajları (bkz. task_queue.py/downloader.py/sound_notifier.py vb.)
    normalde sadece konsola gider — --noconsole ile paketlenmiş (konsolsuz)
    bir .exe'de bu mesajlar hiçbir yere gitmez, tamamen kaybolur. Bu sınıf
    print()'lerin tek satırını bile değiştirmeden, tüm çıktıyı dosyaya da
    kopyalayarak bu sorunu çözer.

    original_stream None olabilir: --noconsole ile paketlenmiş bir .exe'de
    sys.stdout/stderr zaten None'dır (konsol hiç yok) — bu durumda sadece
    log dosyasına yazılır, orijinal stream'e yazma denenmez.
    """

    def __init__(self, original_stream, log_file) -> None:
        self._original = original_stream
        self._log_file = log_file

    def write(self, data: str) -> None:
        if self._original is not None:
            self._original.write(data)
        try:
            self._log_file.write(data)
            self._log_file.flush()
        except Exception:
            pass  # log dosyasına yazılamaması uygulamayı ASLA durdurmamalı

    def flush(self) -> None:
        if self._original is not None:
            self._original.flush()
        try:
            self._log_file.flush()
        except Exception:
            pass


def _setup_file_logging() -> None:
    """Günlük bazlı bir log dosyası açar ve stdout/stderr'i oraya da
    yönlendirir. Log dosyası açılamazsa (izin sorunu vb.) sessizce normal
    konsol davranışına düşülür — loglama asla uygulamanın açılışını
    engellememeli."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{datetime.date.today().isoformat()}.log"
        log_file = open(log_path, "a", encoding="utf-8")
        log_file.write(f"\n--- Uygulama başlatıldı: {datetime.datetime.now().isoformat()} ---\n")
        sys.stdout = _TeeStream(sys.stdout, log_file)
        sys.stderr = _TeeStream(sys.stderr, log_file)
    except OSError as exc:
        if sys.stdout is not None:
            print(f"[main] Log dosyası oluşturulamadı (önemsiz, sadece konsola yazılacak): {exc!r}")


def main() -> None:
    """Uygulamayı başlatır."""
    _setup_file_logging()
    run_app()


if __name__ == "__main__":
    main()
