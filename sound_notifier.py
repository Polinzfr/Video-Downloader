"""Yapılandırılabilir ses bildirimi modülü.

Tkinter'a bağımlı değildir (downloader.py/history_manager.py ile aynı prensip).

Çalma önceliği:
1) pygame.mixer — kuruluysa.
2) Windows'ta PowerShell + WPF MediaPlayer — sıfır pip kurulumu gerektiren
   modern bir Windows ses API'si (.wav ve .mp3 destekler).
3) winsound / MCI — son çare native fallback.
4) Sistem varsayılanı (MessageBeep vb.) — sound_path boşsa.

Ses çalma hiçbir zaman indirme akışını kesmemeli/çökertmemeli — tüm
fonksiyonlar sessizce başarısız olur (try/except), asla exception fırlatmaz.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

WINDOWS_MEDIA_DIR = Path("C:/Windows/Media")

_pygame_mixer = None  # ilk kullanımda _get_mixer() tarafından lazy-init edilir
_pygame_import_failed = False
_pygame_init_failed = False

_current_process = None  # şu an çalmakta olan PowerShell/MediaPlayer süreci
_current_pygame_sound = None  # şu an çalmakta olan pygame.mixer.Sound nesnesi


def stop() -> None:
    """Şu an çalmakta olan (varsa) bildirim sesini durdurur."""
    global _current_process, _current_pygame_sound

    if _current_process is not None and _current_process.poll() is None:
        try:
            _current_process.terminate()
        except Exception as exc:
            print(f"[sound_notifier] Çalan süreç durdurulamadı (önemsiz): {exc!r}")
    _current_process = None

    if _current_pygame_sound is not None:
        try:
            _current_pygame_sound.stop()
        except Exception as exc:
            print(f"[sound_notifier] pygame sesi durdurulamadı (önemsiz): {exc!r}")
    _current_pygame_sound = None


def _get_mixer():
    """pygame.mixer'ı tembel (lazy) olarak import edip başlatır.

    Başarısızsa None döner ve bir daha denemez; çağıran taraf None görünce
    native fallback'e düşer.
    """
    global _pygame_mixer, _pygame_import_failed, _pygame_init_failed

    if _pygame_import_failed or _pygame_init_failed:
        return None
    if _pygame_mixer is not None:
        return _pygame_mixer

    try:
        import pygame.mixer as mixer
    except ImportError:
        _pygame_import_failed = True
        return None

    try:
        mixer.init()
    except Exception as exc:
        _pygame_init_failed = True
        print(f"[sound_notifier] pygame.mixer başlatılamadı (önemsiz, native yönteme düşülüyor): {exc!r}")
        return None

    _pygame_mixer = mixer
    return _pygame_mixer


def list_windows_system_sounds() -> list[tuple[str, str]]:
    """C:\\Windows\\Media\\ altındaki Windows .wav seslerini listeler.

    Döner: [(gösterim_adı, tam_yol), ...] — sadece Windows'ta ve klasör
    gerçekten mevcutsa dolu bir liste döner; aksi halde boş liste.
    """
    if sys.platform != "win32" or not WINDOWS_MEDIA_DIR.exists():
        return []

    sounds: list[tuple[str, str]] = []
    try:
        for wav_path in sorted(WINDOWS_MEDIA_DIR.glob("*.wav")):
            display_name = wav_path.stem.replace("Windows ", "").replace("-", " ")
            sounds.append((display_name, str(wav_path)))
    except OSError as exc:
        print(f"[sound_notifier] Windows ses klasörü okunamadı: {exc!r}")
    return sounds


def play_success(sound_path: Optional[str] = None, volume: float = 1.0) -> None:
    """İndirme başarıyla tamamlandığında çalınacak ses.

    sound_path boş/None ise sistem varsayılanına düşer; o durumda volume
    uygulanamaz.
    """
    _play(sound_path, success=True, volume=volume)


def play_error(sound_path: Optional[str] = None, volume: float = 1.0) -> None:
    """İndirme başarısız olduğunda çalınacak ses. bkz. play_success."""
    _play(sound_path, success=False, volume=volume)


def _play(sound_path: Optional[str], success: bool, volume: float = 1.0) -> None:
    volume = max(0.0, min(1.0, volume))
    try:
        if sound_path and Path(sound_path).exists():
            _play_file(sound_path, volume)
        else:
            _play_system_default(success)
    except Exception as exc:
        print(f"[sound_notifier] Ses çalınamadı (önemsiz): {exc!r}")


def _play_file(path: str, volume: float = 1.0) -> None:
    """Belirli bir ses dosyasını (.wav veya .mp3), verilen ses seviyesiyle çalar.

    Not: winsound.PlaySound'un ses seviyesi parametresi yok — sadece diğer
    tüm yöntemler başarısız olup bu son çare .wav dalına düşüldüğünde
    volume uygulanamaz. pygame, PowerShell/MediaPlayer ve MCI yollarının
    hepsinde volume destekleniyor.
    """
    global _current_pygame_sound

    mixer = _get_mixer()
    if mixer is not None:
        try:
            stop()
            sound = mixer.Sound(path)
            sound.set_volume(volume)
            sound.play()
            _current_pygame_sound = sound
            return
        except Exception as exc:
            print(f"[sound_notifier] pygame ile çalınamadı ({exc!r}), diğer yöntemlere düşülüyor: {path!r}")

    if sys.platform == "win32" and _play_via_powershell_mediaplayer(path, volume):
        return

    _play_file_native(path, volume)


def _play_via_powershell_mediaplayer(path: str, volume: float = 1.0) -> bool:
    """WPF'nin System.Windows.Media.MediaPlayer sınıfını, arka planda ayrık
    çalışan bir PowerShell süreci üzerinden kullanır — hem .wav hem .mp3
    çalabiliyor, MediaPlayer.Volume (0.0-1.0) ile ses seviyesi destekleniyor.

    subprocess.Popen (ateşle-ve-unut) kullanılıyor; süreç sadece
    başlatılabildi mi diye doğrulanabiliyor, gerçekten ses çaldığı garanti
    edilemiyor. Dosya süresi NaturalDuration'dan okunup en fazla 30 saniye
    üst sınırla beklenir.
    """
    global _current_process
    try:
        import subprocess

        stop()

        escaped_path = path.replace("'", "''")
        volume_clamped = max(0.0, min(1.0, volume))
        script = (
            "Add-Type -AssemblyName PresentationCore; "
            f"$p = New-Object System.Windows.Media.MediaPlayer; "
            f"$p.Volume = {volume_clamped}; "
            f"$p.Open([uri]'{escaped_path}'); "
            "$p.Play(); "
            "$waited = 0; "
            "while (-not $p.NaturalDuration.HasTimeSpan -and $waited -lt 3000) { "
            "Start-Sleep -Milliseconds 100; $waited += 100 }; "
            "if ($p.NaturalDuration.HasTimeSpan) { "
            "$durationMs = [Math]::Min($p.NaturalDuration.TimeSpan.TotalMilliseconds, 30000) "
            "} else { $durationMs = 3000 }; "
            "Start-Sleep -Milliseconds $durationMs; "
            "$p.Close()"
        )
        _current_process = subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        print(f"[sound_notifier] PowerShell MediaPlayer başlatılamadı: {exc!r}")
        return False


def _play_file_native(path: str, volume: float = 1.0) -> None:
    """PowerShell/MediaPlayer kullanılamadığında düşülen, en eski native
    mekanizma (winsound/MCI) — gerçek anlamda son çare.

    winsound.PlaySound'un ses seviyesi parametresi yok — .wav dalında
    volume sessizce göz ardı edilir. MCI dalında (.mp3 ve winsound
    başarısız olursa .wav fallback'i) volume destekleniyor.
    """
    if sys.platform == "win32":
        ext = Path(path).suffix.lower()

        if ext == ".mp3":
            if not _play_via_mci(path, volume):
                print(f"[sound_notifier] MP3 çalınamadı (native MCI de başarısız): {path!r}")
            return

        try:
            import winsound

            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as exc:
            print(f"[sound_notifier] winsound başarısız ({exc!r}), MCI deneniyor: {path!r}")
            if not _play_via_mci(path, volume):
                print(f"[sound_notifier] Native MCI de başarısız oldu: {path!r}")
    elif sys.platform == "darwin":
        import subprocess

        subprocess.run(["afplay", "-v", str(volume), path], check=False)
    else:
        import subprocess

        players_with_volume = [
            ("paplay", ["--volume", str(int(volume * 65536))]),
            ("ffplay", ["-nodisp", "-autoexit", "-volume", str(int(volume * 100))]),
        ]
        for player, volume_args in players_with_volume:
            try:
                subprocess.run([player, *volume_args, path], check=False, capture_output=True)
                return
            except FileNotFoundError:
                continue
        try:
            subprocess.run(["aplay", path], check=False, capture_output=True)
            return
        except FileNotFoundError:
            pass
        print("\a", end="", flush=True)


_MCI_ALIAS = "claude_video_downloader_notify"


def _play_via_mci(path: str, volume: float = 1.0) -> bool:
    """Windows Media Control Interface (winmm.dll) üzerinden ses çalar —
    pygame ve PowerShell/MediaPlayer kullanılamadığında son çare.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        winmm = ctypes.windll.winmm
        winmm.mciSendStringW(f"close {_MCI_ALIAS}", None, 0, None)
        open_cmd = f'open "{path}" alias {_MCI_ALIAS}'
        result = winmm.mciSendStringW(open_cmd, None, 0, None)
        if result != 0:
            print(f"[sound_notifier] MCI open başarısız (kod {result}): {path!r}")
            return False

        volume_int = int(max(0.0, min(1.0, volume)) * 1000)
        winmm.mciSendStringW(f"setaudio {_MCI_ALIAS} volume to {volume_int}", None, 0, None)

        result = winmm.mciSendStringW(f"play {_MCI_ALIAS}", None, 0, None)
        if result != 0:
            print(f"[sound_notifier] MCI play başarısız (kod {result}): {path!r}")
            winmm.mciSendStringW(f"close {_MCI_ALIAS}", None, 0, None)
            return False
        return True
    except Exception as exc:
        print(f"[sound_notifier] MCI ile çalma başarısız: {exc!r}")
        return False


def _play_system_default(success: bool) -> None:
    """sound_path hiç seçilmemişse düşülen sıfır-yapılandırma fallback'i."""
    if sys.platform == "win32":
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK if success else winsound.MB_ICONHAND)
    elif sys.platform == "darwin":
        import subprocess

        sound_file = "Glass.aiff" if success else "Basso.aiff"
        subprocess.run(["afplay", f"/System/Library/Sounds/{sound_file}"], check=False)
    else:
        print("\a", end="", flush=True)
