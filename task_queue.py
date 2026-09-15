"""İndirme işlerini bir Producer-Consumer kuyruğu üzerinden yöneten modül.

Producer: ui.py, enqueue() ile bir DownloadTask ekler.
Queue: stdlib queue.Queue (thread-safe, FIFO).
Consumer: worker thread havuzu, kuyruktan task çekip uygun Provider'ı
(bkz. providers.py) çözer ve provider.download()'ı çağırır.
Her durum değişikliğinde on_task_update callback'i dispatch_to_main_thread
üzerinden UI thread'inde çalıştırılır.

İptal / Duraklatma: her DownloadTask kendi cancel_event ve pause_event'ini
taşır. Bu event'ler provider.download()'a geçilir; asıl bekleme/kesme
mantığı downloader.py'deki progress hook içinde uygulanır (bkz.
downloader.py). task_queue burada sadece event'leri set/clear eder ve
sonucu duruma yansıtır.
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from downloader import DownloadError, ProgressInfo
from providers import ProviderRegistry


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _new_resume_event() -> threading.Event:
    """pause_event varsayılanı: 'set' = duraklatılmamış (çalışıyor)."""
    event = threading.Event()
    event.set()
    return event


@dataclass
class DownloadTask:
    """Kuyruğa eklenen tek bir indirme işi."""

    url: str
    media_format: str
    quality_key: str
    output_dir: Path
    audio_bitrate: Optional[str] = None
    filename_template: str = "%(title)s"
    embed_thumbnail: bool = True
    concurrent_fragments: int = 4
    status_messages: Optional[dict] = None

    display_title: str = ""

    platform: str = ""
    thumbnail_url: Optional[str] = None

    cookie_source: str = "auto"
    cookie_file_path: Optional[str] = None

    download_subtitles: bool = False
    subtitle_langs: Optional[list[str]] = None

    speed_limit_kbps: int = 0  # 0 = sınırsız

    # --- Runtime state ---
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: TaskStatus = field(default=TaskStatus.PENDING)
    progress: Optional[ProgressInfo] = None
    result_path: Optional[Path] = None
    error: Optional[str] = None

    # set() = iptal edildi. worker, task'ı kuyruktan çekince ve indirme
    # sırasında periyodik olarak bunu kontrol eder.
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # clear() = duraklatıldı (wait() bloklar); set() = çalışıyor/devam ediyor.
    pause_event: threading.Event = field(default_factory=_new_resume_event)


TaskUpdateCallback = Callable[[DownloadTask], None]
DispatchFn = Callable[[Callable[[], None]], None]


class TaskQueueManager:
    """queue.Queue tabanlı Producer-Consumer indirme kuyruğu yöneticisi."""

    def __init__(
        self,
        on_task_update: TaskUpdateCallback,
        dispatch_to_main_thread: DispatchFn,
        max_workers: int = 2,
    ) -> None:
        self._queue: "queue.Queue[Optional[DownloadTask]]" = queue.Queue()
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._on_task_update = on_task_update
        self._dispatch = dispatch_to_main_thread
        self._max_workers = max(1, max_workers)
        self._shutdown_event = threading.Event()
        self._started = False

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------
    def enqueue(self, task: DownloadTask) -> str:
        with self._lock:
            self._tasks[task.task_id] = task
        self._queue.put(task)
        self._notify(task)
        return task.task_id

    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def pending_count(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # İptal / Duraklatma — UI'dan çağrılır
    # ------------------------------------------------------------------
    def cancel(self, task_id: str) -> None:
        """Bir task'ı iptal eder. PENDING (henüz işlenmemiş) veya RUNNING/
        PAUSED durumundaki bir task için çalışır. COMPLETED/FAILED/
        CANCELLED durumundaki bir task için no-op'tur.
        """
        task = self.get_task(task_id)
        if task is None or task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            return
        task.cancel_event.set()
        # Duraklatılmışsa uyandır ki cancel_event kontrolüne düşüp çıkabilsin.
        task.pause_event.set()

    def pause(self, task_id: str) -> None:
        """Sadece RUNNING durumundaki bir task'ı duraklatır."""
        task = self.get_task(task_id)
        if task is None or task.status != TaskStatus.RUNNING:
            return
        task.pause_event.clear()
        task.status = TaskStatus.PAUSED
        self._notify(task)

    def resume(self, task_id: str) -> None:
        """Sadece PAUSED durumundaki bir task'ı devam ettirir."""
        task = self.get_task(task_id)
        if task is None or task.status != TaskStatus.PAUSED:
            return
        task.status = TaskStatus.RUNNING
        task.pause_event.set()
        self._notify(task)

    # ------------------------------------------------------------------
    # Worker havuzu
    # ------------------------------------------------------------------
    def start(self, num_workers: Optional[int] = None) -> None:
        if self._started:
            return
        self._started = True
        count = num_workers or self._max_workers
        for i in range(count):
            self._spawn_worker(i + 1)

    def resize_workers(self, new_count: int) -> None:
        new_count = max(1, new_count)
        current = len(self._workers)
        self._max_workers = new_count
        if not self._started or new_count <= current:
            return
        for i in range(current, new_count):
            self._spawn_worker(i + 1)

    def _spawn_worker(self, index: int) -> None:
        t = threading.Thread(target=self._worker_loop, name=f"DownloadWorker-{index}", daemon=True)
        t.start()
        self._workers.append(t)

    def shutdown(self) -> None:
        self._shutdown_event.set()
        for _ in self._workers:
            self._queue.put(None)

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                if self._shutdown_event.is_set():
                    task.status = TaskStatus.CANCELLED
                    self._notify(task)
                    continue

                # PENDING durumdayken iptal edilmiş olabilir — kuyruktan
                # çekilir çekilmez kontrol edilir, hiç indirme başlatılmaz.
                if task.cancel_event.is_set():
                    task.status = TaskStatus.CANCELLED
                    self._notify(task)
                    continue

                self._process_task(task)
            finally:
                self._queue.task_done()

    def _process_task(self, task: DownloadTask) -> None:
        task.status = TaskStatus.RUNNING
        self._notify(task)

        def progress_callback(p: ProgressInfo) -> None:
            task.progress = p
            self._notify(task)

        try:
            provider = ProviderRegistry.resolve(
                task.url,
                cookie_source=task.cookie_source,
                cookie_file_path=task.cookie_file_path,
            )
            result_path = provider.download(
                url=task.url,
                output_dir=task.output_dir,
                media_format=task.media_format,
                quality_key=task.quality_key,
                progress_callback=progress_callback,
                filename_template=task.filename_template,
                embed_thumbnail=task.embed_thumbnail,
                audio_bitrate=task.audio_bitrate,
                concurrent_fragments=task.concurrent_fragments,
                status_messages=task.status_messages or {},
                cancel_event=task.cancel_event,
                pause_event=task.pause_event,
                download_subtitles=task.download_subtitles,
                subtitle_langs=task.subtitle_langs,
                speed_limit_kbps=task.speed_limit_kbps,
            )
            task.result_path = result_path
            task.status = TaskStatus.COMPLETED
        except DownloadError as exc:
            if task.cancel_event.is_set():
                task.status = TaskStatus.CANCELLED
            else:
                task.error = str(exc)
                task.status = TaskStatus.FAILED
        except Exception as exc:
            if task.cancel_event.is_set():
                task.status = TaskStatus.CANCELLED
            else:
                task.error = str(exc)
                task.status = TaskStatus.FAILED
                print(f"[task_queue] Beklenmeyen hata (task={task.task_id}, url={task.url!r}): {exc!r}")

        self._notify(task)

    # ------------------------------------------------------------------
    # UI bildirimi
    # ------------------------------------------------------------------
    def _notify(self, task: DownloadTask) -> None:
        self._dispatch(lambda: self._safe_dispatch(task))

    def _safe_dispatch(self, task: DownloadTask) -> None:
        try:
            self._on_task_update(task)
        except Exception as exc:
            print(f"[task_queue] on_task_update callback hatası: {exc!r}")
