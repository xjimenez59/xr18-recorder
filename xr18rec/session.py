"""Pilotage d'`arecord` : démarrage/arrêt d'une session, suivi des dépassements (xruns)."""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

from . import audio, storage, system, wavtools, xr18osc
from . import health as health_mod
from .config import FORMAT_BYTES, Config
from .meters import LevelMonitor, msb_offset
from .reaper import LAYOUT_FILE, channel_names, load_layout, write_project

MONITOR_IDLE_S = 10.0  # contrôle des niveaux : arrêt si plus aucune page ne l'interroge
MONITOR_MAX_S = 600.0  # et en tout cas au bout de 10 minutes
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RecorderError(RuntimeError):
    pass


class _Monitor:
    """Lecture de contrôle de la carte son (sans enregistrer) pour voir les niveaux avant la prise."""

    def __init__(self, proc: subprocess.Popen, mon: LevelMonitor):
        self.proc, self.mon = proc, mon
        self.started = self.last_poll = time.monotonic()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.layout = None
        self.stderr: deque[str] = deque(maxlen=10)

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


def sanitize_name(name: str | None) -> str:
    return _SAFE.sub("_", (name or "").strip()).strip("._-")[:60]


class Recorder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._session_dir: str | None = None
        self._started = 0.0
        self._stopped = 0.0
        self._overruns = 0
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._error: str | None = None
        self._stopping = False
        self._layout_thread: threading.Thread | None = None
        self._levels: LevelMonitor | None = None
        self._monitor: _Monitor | None = None
        self._health = health_mod.Health()
        self._take_health: dict = {}  # maxima et événements de la prise (température, sous-tension…)
        self._overrun_at: list[float] = []  # instants (s depuis le début) des dépassements
        self._health_thread: threading.Thread | None = None
        self._started_iso = ""
        self._free_start = 0.0
        self._levels_thread: threading.Thread | None = None
        self._notice: str | None = None
        self._active_dir = self._restore_choice()

    # ---- commande ---------------------------------------------------------
    def build_command(self, basename: str) -> list[str]:
        c = self.cfg
        return [
            c.arecord_bin, "-D", c.device, "-c", str(c.channels), "-r", str(c.rate),
            "-f", c.format, "-t", "wav", "--max-file-time", str(c.segment_seconds),
            basename,  # pas de -q : on veut les messages « overrun » sur stderr
        ]

    # ---- état -------------------------------------------------------------
    def free_gb(self) -> float:
        path = self._active_dir
        while path and not os.path.exists(path):
            path = os.path.dirname(path)
        return shutil.disk_usage(path or "/").free / 1024**3

    def is_recording(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            running = self.is_recording()
            if self._proc is not None and not running and not self._stopping and self._error is None:
                rc = self._proc.returncode
                if rc not in (0, -signal.SIGINT):
                    self._error = f"arecord s'est arrêté (code {rc}) : " + " | ".join(self._stderr_tail)
            files = self._list_files(self._session_dir) if self._session_dir else []
            if running:
                state, elapsed = "recording", time.monotonic() - self._started
            else:
                state = "error" if self._error else "idle"
                elapsed = (self._stopped - self._started) if self._started and self._stopped else 0.0
            return {
                "state": state,
                "session": os.path.basename(self._session_dir) if self._session_dir else None,
                "elapsed_s": round(elapsed, 1),
                "overruns": self._overruns,
                "free_gb": round(self.free_gb(), 2),
                "remaining_s": self._remaining_s(self.free_gb()),
                "files": files,
                "error": self._error,
                "storage": self._storage_summary(),
                "actual": self._actual_format(),
                "levels": self._levels_summary(),
                "alerts": self._alerts(),
                "version": system.version(),
                "stderr_tail": list(self._stderr_tail),
                "config": {
                    "device": self.cfg.device, "channels": self.cfg.channels,
                    "rate": self.cfg.rate, "format": self.cfg.format,
                    "segment_seconds": self.cfg.segment_seconds,
                },
            }

    # ---- santé du Pi, bilan de prise, alertes -----------------------------------
    def _health_loop(self, proc: subprocess.Popen, directory: str) -> None:
        """Pendant la prise : relève l'état du Pi toutes les 2 s et tient à jour recording.json."""
        take = self._take_health
        start_pw = (self._health.snapshot(max_age=0).get("power") or {})
        last_write = time.monotonic()
        while True:
            try:
                snap = self._health.snapshot(max_age=0)
                take["samples"] = take.get("samples", 0) + 1
                for key, val in (("max_temp_c", snap["temp_c"]), ("max_cpu_pct", snap["cpu_pct"]), ("max_load1", snap["load1"])):
                    if val is not None:
                        take[key] = max(take.get(key, 0), val)
                pw = snap.get("power") or {}
                if pw.get("undervoltage_now") or (pw.get("undervoltage_seen") and not start_pw.get("undervoltage_seen")):
                    take["undervoltage"] = True  # y compris une chute plus brève que l'intervalle de mesure
                if pw.get("throttled_now") or pw.get("freq_capped_now") or (
                        pw.get("throttled_seen") and not start_pw.get("throttled_seen")):
                    take["throttled"] = True
            except Exception:  # la surveillance ne doit jamais gêner l'enregistrement
                pass
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - last_write >= 30:
                self._write_recording_info(directory)
                last_write = time.monotonic()
        self._write_recording_info(directory)

    def _write_recording_info(self, directory: str) -> None:
        """Bilan de la prise : durée, dépassements, état du Pi, derniers messages d'arecord."""
        c = self.cfg
        info = {
            "version": 1, "app_version": system.version(), "started": self._started_iso,
            "duration_s": round(time.monotonic() - self._started, 1),
            # sans le verrou de l'enregistreur : stop() le tient pendant qu'il attend ce fil (blocage sinon)
            "state": "error" if self._error else ("recording" if self._proc is not None and self._proc.poll() is None else "ok"),
            "error": self._error, "device": c.device, "format": c.format, "rate": c.rate, "channels": c.channels,
            "segment_seconds": c.segment_seconds, "storage": self._storage_summary()["label"],
            "free_gb_start": self._free_start, "free_gb_end": round(self.free_gb(), 2),
            "overruns": {"count": self._overruns, "at_s": list(self._overrun_at)},
            "health": dict(self._take_health), "stderr_tail": list(self._stderr_tail)[-10:],
        }
        tmp = os.path.join(directory, "recording.json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(directory, "recording.json"))
        except OSError:
            pass

    def _alerts(self) -> list[dict]:
        """Problèmes à montrer sur l'accueil : ceux de la prise (conservés jusqu'à la suivante) et l'état actuel du Pi."""
        out: list[dict] = []
        n = self._overruns
        if n:
            first = f" (le premier à {health_mod.fmt_time(self._overrun_at[0])})" if self._overrun_at else ""
            out.append({"level": "error", "code": "overruns", "message": (
                f"{n} dépassement{'s' if n > 1 else ''} : de l'audio a été perdu{first}. "
                "Enregistrez sur un disque USB plutôt que sur la carte SD, et vérifiez l'alimentation du Pi.")})
        take = self._take_health
        if take.get("undervoltage"):
            out.append({"level": "error", "code": "take_undervoltage", "message":
                        "Sous-tension pendant la prise : alimentation du Pi insuffisante (bloc secteur, câble, disque USB)."})
        if take.get("throttled"):
            out.append({"level": "warn", "code": "take_throttled", "message":
                        "Le processeur du Pi a été ralenti pendant la prise (alimentation ou chaleur)."})
        if take.get("max_temp_c", 0) >= health_mod.TEMP_ERROR:
            out.append({"level": "error", "code": "take_temp", "message":
                        f"Pi très chaud pendant la prise ({take['max_temp_c']:.0f} °C)."})
        mon = self._levels
        if mon is not None and mon.skipped_seconds() > 0:
            out.append({"level": "warn", "code": "levels_partial", "message":
                        "Analyse des niveaux partielle : le Pi était trop chargé (l'enregistrement n'est pas touché)."})
        if self._error:
            out.append({"level": "error", "code": "recorder", "message": f"Enregistrement interrompu : {self._error}"})
        skip = {"undervoltage", "undervoltage_seen"} if take.get("undervoltage") else set()
        seen = {a["code"] for a in out}
        out += [a for a in health_mod.assess(self._health.snapshot()) if a["code"] not in skip | seen]
        if self.is_recording():
            remaining = self._remaining_s(self.free_gb())
            if remaining < 600:
                out.append({"level": "error", "code": "space", "message":
                            f"Espace disque presque épuisé : environ {max(0, remaining // 60)} min d'enregistrement possible."})
            elif remaining < 1800:
                out.append({"level": "warn", "code": "space", "message":
                            f"Espace disque limité : environ {remaining // 60} min d'enregistrement possible."})
        return out

    def health_info(self) -> dict:
        snap = self._health.snapshot()
        return {"live": snap, "alerts": health_mod.assess(snap), "take": dict(self._take_health),
                "recording": self.is_recording()}

    def _level_names(self, count: int) -> list[str]:
        return channel_names(load_layout(self._session_dir) if self._session_dir else None, count)

    def _levels_summary(self) -> dict:
        """Voies saturées pendant la prise (pour l'avertissement de l'accueil)."""
        mon = self._levels
        if mon is None:
            return {"clipped": []}
        snap = mon.snapshot()
        names = self._level_names(mon.channels)
        return {"clipped": [{"channel": i + 1, "name": names[i], "first_s": c["events"][0] if c["events"] else None}
                            for i, c in enumerate(snap["channels"]) if c["clipped"]]}

    # ---- contrôle des niveaux avant la prise --------------------------------
    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        """À n'appeler qu'une fois le fil de lecture terminé (un descripteur fermé peut être réutilisé)."""
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, AttributeError):
                pass

    def _stop_monitor(self) -> bool:
        """Arrête le contrôle s'il tourne ; renvoie True si la carte vient d'être libérée par nos soins."""
        m = self._monitor
        if m is None or not (m.alive() or m.proc.poll() is None):
            return False
        m.stop.set()
        self._terminate(m.proc)
        if m.thread is not None:
            m.thread.join(timeout=3)
        if m.thread is None or not m.thread.is_alive():
            self._close_pipes(m.proc)
        return True

    def _wait_capture_free(self, timeout: float = 2.0) -> None:
        """Attend que la carte soit libre ; sans certitude (état illisible), on n'attend pas et on laisse arecord juger."""
        deadline = time.monotonic() + timeout
        while audio.capture_busy(self.cfg.device) is True and time.monotonic() < deadline:
            time.sleep(0.05)

    def monitor_start(self) -> dict:
        with self._lock:
            if self.is_recording():
                raise RecorderError("un enregistrement est en cours : les niveaux sont affichés en direct")
            m = self._monitor
            if m is not None and m.alive():
                m.last_poll = time.monotonic()
                return self.levels()
            c = self.cfg
            width = FORMAT_BYTES[c.format]
            cmd = [c.arecord_bin, "-D", c.device, "-c", str(c.channels), "-r", str(c.rate), "-f", c.format,
                   "-t", "raw", "-q", "-"]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            except OSError as exc:
                raise RecorderError(f"lancement d'arecord impossible : {exc}") from exc
            m = _Monitor(proc, LevelMonitor(None, c.rate, c.channels, width, msb_offset(c.format, width)))
            drain = threading.Thread(target=self._drain_stderr, args=(m,), daemon=True, name="monitor-stderr")
            drain.start()
            try:
                proc.wait(timeout=0.6)  # carte absente ou occupée : arecord s'arrête aussitôt
            except subprocess.TimeoutExpired:
                pass
            else:
                drain.join(timeout=1)
                self._terminate(proc)
                self._close_pipes(proc)
                tail = m.stderr[-1] if m.stderr else f"arecord s'est arrêté (code {proc.returncode})"
                raise RecorderError(f"contrôle des niveaux impossible : {tail}")
            m.started = m.last_poll = time.monotonic()  # le délai d'inactivité part du vrai démarrage du flux
            m.thread = threading.Thread(target=self._monitor_loop, args=(m,), daemon=True, name="monitor")
            self._monitor = m
            m.thread.start()
            if c.xr18_host:
                threading.Thread(target=self._monitor_layout, args=(m,), daemon=True, name="monitor-layout").start()
            return self.levels()

    def monitor_stop(self) -> dict:
        with self._lock:
            self._stop_monitor()
            return self.levels()

    def _drain_stderr(self, m: _Monitor) -> None:
        try:
            for raw in m.proc.stderr:  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    m.stderr.append(line)
        except (OSError, ValueError):
            pass

    def _monitor_layout(self, m: _Monitor) -> None:
        try:
            m.layout = xr18osc.fetch_layout(self.cfg.xr18_host, self.cfg.xr18_osc_port, timeout=3.0)
        except (xr18osc.OscError, OSError):
            pass  # noms génériques

    def _monitor_loop(self, m: _Monitor) -> None:
        fd = m.proc.stdout.fileno()  # type: ignore[union-attr]
        try:
            while not m.stop.is_set():
                now = time.monotonic()
                if now - m.last_poll > MONITOR_IDLE_S or now - m.started > MONITOR_MAX_S:
                    break  # personne ne regarde plus (ou trop long) : on rend la carte
                data = os.read(fd, 65536)
                if not data:
                    if not m.stop.is_set():
                        m.error = "flux audio interrompu" + (f" : {m.stderr[-1]}" if m.stderr else "")
                    break
                m.mon.feed(data)
        except (OSError, ValueError):
            if not m.stop.is_set():
                m.error = "flux audio interrompu"
        except Exception as exc:  # jamais d'incidence sur le reste du programme
            m.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._drain_and_terminate(m)
            self._close_pipes(m.proc)

    def _drain_and_terminate(self, m: _Monitor) -> None:
        """Arrête arecord en continuant de lire son flux : tube plein, il resterait bloqué en écriture et
        ne réagirait pas à l'arrêt (il faudrait le tuer de force, ce qui laisse la carte occupée un moment)."""
        proc, fd = m.proc, m.proc.stdout.fileno()  # type: ignore[union-attr]
        if proc.poll() is None:
            proc.terminate()
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                ready, _w, _x = select.select([fd], [], [], 0.2)
                if ready and not os.read(fd, 65536):
                    break  # fin du flux : arecord est sorti
            except (OSError, ValueError):
                break
            if not ready and proc.poll() is not None:
                break
        self._terminate(proc)  # tue de force s'il vit encore

    def levels(self) -> dict:
        """Niveaux : de la prise en cours, du contrôle en cours, ou de la dernière prise terminée."""
        m = self._monitor
        recording = self.is_recording()
        if recording and self._levels is not None:
            snap, mode = self._levels.snapshot(), "recording"
            snap["names"] = self._level_names(self._levels.channels)
        elif m is not None and m.alive():
            m.last_poll = time.monotonic()
            snap, mode = m.mon.snapshot(), "monitor"
            snap["names"] = channel_names(m.layout, m.mon.channels)
        elif self._levels is not None:
            snap, mode = self._levels.snapshot(), "last"
            snap["names"] = self._level_names(self._levels.channels)
        else:
            snap, mode = {"has_data": False, "elapsed_s": 0, "skipped_s": 0, "channels": [], "error": None,
                          "names": []}, "none"
        snap["mode"] = mode
        snap["active"] = mode in ("recording", "monitor")
        snap["monitor_error"] = m.error if (m is not None and not m.alive() and mode != "recording") else None
        return snap

    def reset_levels(self) -> dict:
        m = self._monitor
        if m is not None and m.alive() and not self.is_recording():
            m.mon.reset_display()
        elif self._levels is not None:
            self._levels.reset_display()
        return self.levels()

    def _actual_format(self) -> dict | None:
        """Fréquence, format et voies réellement écrits (d'après l'en-tête du premier WAV de la session)."""
        if not self._session_dir:
            return None
        try:
            first = min(n for n in os.listdir(self._session_dir) if n.lower().endswith(".wav"))
            info = wavtools.read_info(os.path.join(self._session_dir, first))
        except (OSError, ValueError):
            return None  # pas encore de fichier, ou en-tête illisible
        return {"rate": info.rate, "bits": info.bits, "channels": info.channels}

    @staticmethod
    def _list_files(directory: str) -> list[dict]:
        try:
            names = sorted(n for n in os.listdir(directory) if n.lower().endswith((".wav", ".rpp")))
        except OSError:
            return []
        out = []
        for n in names:
            try:
                out.append({"name": n, "bytes": os.path.getsize(os.path.join(directory, n))})
            except OSError:
                pass
        return out

    def _remaining_s(self, free_gb: float) -> int:
        """Durée d'enregistrement possible avec cet espace libre (la prise s'arrête sous la moitié du seuil minimal)."""
        usable = max(0.0, free_gb - self.cfg.min_free_gb / 2) * 1024**3
        return int(usable / self.cfg.bytes_per_second)

    def sessions(self) -> list[dict]:
        out = []
        for v in self.volumes():
            try:
                names = [n for n in os.listdir(v.path) if os.path.isdir(os.path.join(v.path, n))]
            except OSError:
                continue
            out += [{"name": n, "location": v.label, "files": self._list_files(os.path.join(v.path, n))}
                    for n in names]
        return sorted(out, key=lambda e: e["name"], reverse=True)

    def resolve_file(self, session: str, filename: str) -> str | None:
        """Chemin d'un fichier de session, quel que soit l'emplacement où elle a été enregistrée."""
        for v in self.volumes():
            path = os.path.join(v.path, session, filename)
            if os.path.isfile(path):
                return path
        return None

    def resolve_session(self, session: str) -> str | None:
        """Dossier d'une session, quel que soit l'emplacement où elle a été enregistrée."""
        for v in self.volumes():
            path = os.path.join(v.path, session)
            if os.path.isdir(path):
                return path
        return None

    def delete_session(self, session: str, confirm: str) -> dict:
        """Supprime définitivement une session (dossier et fichiers). Refusé pendant une prise ; `confirm` doit répéter le nom."""
        if not session or session in (".", "..") or "/" in session or "\\" in session or "\0" in session:
            raise RecorderError("nom de session invalide")
        if confirm != session:
            raise RecorderError("confirmation absente ou différente du nom de la session")
        with self._lock:
            if self.is_recording() or self._monitor is not None and self._monitor.alive():
                raise RecorderError("impossible pendant un enregistrement ou un contrôle des niveaux")
            path = self.resolve_session(session)
            if path is None or os.path.islink(path):
                raise RecorderError("session introuvable")
            size = sum(f["bytes"] for f in self._list_files(path))
            try:
                shutil.rmtree(path)
            except OSError as exc:
                raise RecorderError(f"suppression impossible : {exc}") from exc
            if self._session_dir == path:
                self._session_dir = None
                self._levels = None  # bilan de la prise supprimée
            return {"deleted": session, "freed_bytes": size}

    # ---- stockage ---------------------------------------------------------
    def volumes(self) -> list[storage.Volume]:
        return storage.list_volumes(self.cfg.output_dir, self.cfg.usb_auto)

    def _restore_choice(self) -> str:
        """Emplacement mémorisé s'il est encore disponible, sinon le dossier par défaut."""
        try:
            with open(self.cfg.state_file, encoding="utf-8") as f:
                saved = json.load(f).get("storage")
        except (OSError, ValueError, AttributeError):
            return self.cfg.output_dir
        if saved and saved in [v.path for v in self.volumes()]:
            return saved
        if saved and saved != self.cfg.output_dir:
            self._notice = f"Disque habituel absent ({saved}) : carte SD utilisée"
        return self.cfg.output_dir

    def _storage_summary(self) -> dict:
        vols = {v.path: v for v in self.volumes()}
        v = vols.get(self._active_dir)
        return {"path": self._active_dir, "label": v.label if v else self._active_dir,
                "kind": v.kind if v else None, "available": v is not None}

    def storage_info(self) -> dict:
        with self._lock:
            vols = self.volumes()
            if self._active_dir not in [v.path for v in vols]:
                self._notice = f"Emplacement absent : {self._active_dir}"
            return {"active": self._active_dir, "recording": self.is_recording(), "notice": self._notice,
                    "options": [dict(v.to_dict(), active=(v.path == self._active_dir), remaining_s=self._remaining_s(v.free_gb))
                                for v in vols]}

    def set_storage(self, path: str) -> dict:
        with self._lock:
            if self.is_recording():
                raise RecorderError("impossible de changer d'emplacement pendant un enregistrement")
            match = next((v for v in self.volumes() if v.path == path), None)
            if match is None:
                raise RecorderError("emplacement inconnu ou non monté")
            if not match.writable:
                raise RecorderError(f"{match.label} : dossier non inscriptible (droits du disque ?)")
            try:
                os.makedirs(match.path, exist_ok=True)
            except OSError as exc:
                raise RecorderError(f"{match.label} : création du dossier impossible ({exc})") from exc
            self._active_dir, self._notice = match.path, None
            try:
                os.makedirs(os.path.dirname(self.cfg.state_file) or ".", exist_ok=True)
                with open(self.cfg.state_file, "w", encoding="utf-8") as f:
                    json.dump({"storage": match.path}, f)
            except OSError:
                pass  # choix non mémorisé après redémarrage : sans gravité
        return self.storage_info()

    # ---- start / stop -----------------------------------------------------
    def start(self, name: str | None = None) -> str:
        with self._lock:
            if self.is_recording():
                raise RecorderError("un enregistrement est déjà en cours")
            if self._stop_monitor():  # REC pendant le contrôle des niveaux : on libère la carte, puis on enregistre
                self._wait_capture_free()
            if self._active_dir not in [v.path for v in self.volumes()]:
                raise RecorderError(f"emplacement d'enregistrement absent : {self._active_dir}")
            try:
                os.makedirs(self._active_dir, exist_ok=True)
            except OSError as exc:
                raise RecorderError(f"dossier d'enregistrement inutilisable : {exc}") from exc
            if self.free_gb() < self.cfg.min_free_gb:
                raise RecorderError(
                    f"espace disque insuffisant : {self.free_gb():.1f} Gio libres "
                    f"(minimum {self.cfg.min_free_gb} Gio)")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            label = sanitize_name(name)
            directory = os.path.join(self._active_dir, f"{stamp}-{label}" if label else stamp)
            os.makedirs(directory, exist_ok=False)
            basename = os.path.join(directory, "take.wav")
            cmd = self.build_command(basename)
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                              text=True, bufsize=1)
            except OSError as exc:
                raise RecorderError(f"lancement d'arecord impossible : {exc}") from exc
            self._session_dir = directory
            self._started = time.monotonic()
            self._stopped = 0.0
            self._overruns = 0
            self._overrun_at = []
            self._take_health = {}
            self._started_iso = datetime.now().isoformat(timespec="seconds")
            self._free_start = round(self.free_gb(), 2)
            self._error = None
            self._stopping = False
            self._stderr_tail.clear()
            threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True,
                             name="arecord-stderr").start()
            threading.Thread(target=self._watch_disk, args=(self._proc,), daemon=True,
                             name="disk-watch").start()
            width = FORMAT_BYTES[self.cfg.format]  # niveaux : lecture seule des fichiers, sans effet sur arecord
            self._levels = LevelMonitor(directory, self.cfg.rate, self.cfg.channels, width,
                                        msb_offset(self.cfg.format, width))
            proc = self._proc
            self._levels_thread = threading.Thread(target=self._levels.run, args=(lambda: proc.poll() is None,),
                                                   daemon=True, name="levels")
            self._levels_thread.start()
            self._health_thread = threading.Thread(target=self._health_loop, args=(proc, directory), daemon=True,
                                                   name="health")
            self._health_thread.start()
            self._layout_thread = None
            if self.cfg.xr18_host:  # après le lancement d'arecord : ne retarde pas l'enregistrement
                self._layout_thread = threading.Thread(target=self._snapshot_layout, args=(directory,),
                                                       daemon=True, name="xr18-layout")
                self._layout_thread.start()
            return os.path.basename(directory)

    def _snapshot_layout(self, directory: str) -> None:
        """Mémorise noms et couleurs des voies (channels.json) ; échec silencieux : l'audio prime."""
        try:
            layout = xr18osc.fetch_layout(self.cfg.xr18_host, self.cfg.xr18_osc_port)
            with open(os.path.join(directory, LAYOUT_FILE), "w", encoding="utf-8") as f:
                json.dump(layout.to_dict(), f, ensure_ascii=False, indent=2)
        except (xr18osc.OscError, OSError) as exc:
            self._stderr_tail.append(f"noms de voies XR18 indisponibles : {exc}")

    def _write_project(self) -> None:
        """Écrit le projet Reaper de la session (best effort)."""
        directory = self._session_dir
        if not directory:
            return
        if self._layout_thread is not None:
            self._layout_thread.join(timeout=4)
        try:
            write_project(directory, load_layout(directory))
        except (OSError, ValueError) as exc:
            self._stderr_tail.append(f"projet Reaper non généré : {exc}")

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if "overrun" in line.lower():
                self._overruns += 1
                if len(self._overrun_at) < 50:
                    self._overrun_at.append(round(time.monotonic() - self._started, 1))

    def _watch_disk(self, proc: subprocess.Popen) -> None:
        while proc.poll() is None:
            time.sleep(2)
            if self.free_gb() < self.cfg.min_free_gb / 2:
                with self._lock:
                    self._error = "espace disque épuisé : arrêt de l'enregistrement"
                    self._interrupt(proc)
                return

    def _interrupt(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)  # arecord finalise l'en-tête WAV sur SIGINT
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._stopped = time.monotonic()
        if self._levels_thread is not None:
            self._levels_thread.join(timeout=15)  # bilan des niveaux (levels.json) avant le projet Reaper
        if self._health_thread is not None:
            self._health_thread.join(timeout=15)  # bilan de la prise (recording.json)
        self._write_project()

    def stop(self) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise RecorderError("aucun enregistrement en cours")
            self._stopping = True
            self._interrupt(self._proc)
        return self.status()

    def shutdown(self) -> None:
        self._stop_monitor()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._stopping = True
                self._interrupt(self._proc)
