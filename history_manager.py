"""İndirme geçmişini yöneten veri katmanı modülü.

config.py'deki load_settings()/save_settings() ile aynı kalıp: aynı
CONFIG_DIR altında ayrı bir history.json dosyası kullanılıyor. Bu modül
UI'a hiç bağımlı değil — ui.py bir HistoryManager örneği oluşturup
add_entry()/remove_entry()/list_entries() çağırarak kullanıyor.

NOT: Kapak görselleri yerel diske indirilip cache'lenmiyor, sadece
thumbnail_url (uzak URL) saklanıyor. Bilinçli bir MVP kararı.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import CONFIG_DIR

HISTORY_FILE = CONFIG_DIR / "history.json"


@dataclass
class HistoryEntry:
    """Tamamlanmış tek bir indirmeyi temsil eden geçmiş kaydı."""

    title: str
    platform: str
    file_path: str
    thumbnail_url: Optional[str] = None
    downloaded_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class HistoryManager:
    """history.json dosyasını okuyan/yazan basit CRUD yöneticisi.

    Her çağrı diskten okuyup diske yazıyor, in-memory cache tutmuyor —
    birden fazla HistoryManager örneği (ana pencere + geçmiş penceresi)
    her zaman güncel veriyi görür.
    """

    def __init__(self, history_file: Path = HISTORY_FILE) -> None:
        self._history_file = history_file

    def list_entries(self) -> list[HistoryEntry]:
        """Tüm geçmiş kayıtlarını, en yeniden en eskiye sıralı döner."""
        if not self._history_file.exists():
            return []

        try:
            raw = json.loads(self._history_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

        known = {f.name for f in HistoryEntry.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        entries: list[HistoryEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            filtered = {k: v for k, v in item.items() if k in known}
            try:
                entries.append(HistoryEntry(**filtered))
            except TypeError:
                continue

        entries.sort(key=lambda e: e.downloaded_at, reverse=True)
        return entries

    def add_entry(self, entry: HistoryEntry) -> None:
        """Yeni bir geçmiş kaydı ekler ve dosyayı yeniden yazar."""
        entries = self.list_entries()
        entries.insert(0, entry)
        self._save(entries)

    def remove_entry(self, entry_id: str) -> None:
        """Belirtilen entry_id'ye sahip kaydı geçmişten siler (dosyayı silmez)."""
        entries = [e for e in self.list_entries() if e.entry_id != entry_id]
        self._save(entries)

    def _save(self, entries: list[HistoryEntry]) -> None:
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        self._history_file.write_text(
            json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
