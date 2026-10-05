#!/usr/bin/env python3
"""
ANALIZADOR DE RIVALES - interfaz web local

Uso:
    python app.py        ->  abre http://127.0.0.1:5000

Flujo: formulario (link de YouTube, minutos, equipo, color) -> descarga solo ese
tramo -> busca planos generales -> Claude Vision analiza cada frame -> informe
consolidado -> chat. Varios analisis del mismo equipo se pueden combinar en un
informe unico, y cualquier informe se puede exportar a PDF.
"""
import base64
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-haiku-4-5")
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-haiku-4-5")
PORT = int(os.getenv("PORT", "5000"))

MAX_DURATION_MIN = int(os.getenv("MAX_DURATION", "120"))   # un partido completo cabe
MIN_FRAMES, MAX_FRAMES = 3, 200
FRAMES_PER_MIN, MIN_AUTO_FRAMES = 2, 8              # modo automatico: 1 frame cada 30 s de video
ANALYSIS_WORKERS = max(1, int(os.getenv("ANALYSIS_WORKERS", "3")))   # frames analizados en paralelo
BLOCK_SIZE, LONG_THRESHOLD = 12, 24                 # >24 frames utiles: se resume por bloques de ~12 y luego se unifica
MAX_COMBINE = 8
GRASS_MIN = float(os.getenv("GRASS_MIN", "0.40"))   # fraccion minima de cancha visible para ser "plano general"
CANDIDATE_FACTOR, MAX_CANDIDATES = 4, 800           # se revisan ~4x los frames pedidos y se eligen los mejores
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]        # (los tests lo reemplazan)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,60}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
_meta_lock = threading.Lock()
_client = None


class FatalError(Exception):
    """Error que no vale la pena reintentar (clave invalida, modelo inexistente...)."""


class FormError(Exception):
    """Dato invalido en el formulario."""


class Cancelled(Exception):
    """El usuario cancelo el analisis."""


class Job:
    """Estado de un trabajo en curso: permite cancelarlo y matar el proceso de descarga."""

    def __init__(self):
        self.cancel = threading.Event()
        self.proc = None
        self.lock = threading.Lock()


JOBS = {}   # id de analisis -> Job (solo mientras esta en curso)


# ═══════════════════════════════════════════════════════════════
# CLAUDE
# ═══════════════════════════════════════════════════════════════

def get_client():
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise FatalError("Falta ANTHROPIC_API_KEY. Crea el archivo .env (copia .env.example) y pega tu clave.")
        import anthropic
        # reintentos con espera creciente: en analisis largos un limite de velocidad puntual no debe perder frames
        _client = anthropic.Anthropic(api_key=key, max_retries=6)
    return _client


def claude_text(model, messages, max_tokens=1000, system=None):
    """Llama a Claude y devuelve el texto. Traduce los errores comunes a mensajes claros."""
    import anthropic
    client = get_client()
    kwargs = dict(model=model, max_tokens=max_tokens, messages=messages)
    if system:
        kwargs["system"] = system
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise FatalError("La API key no es valida (revisa el archivo .env).")
    except anthropic.PermissionDeniedError:
        raise FatalError("Tu API key no tiene permiso para usar este modelo.")
    except anthropic.NotFoundError:
        raise FatalError(f"El modelo '{model}' no existe para tu cuenta. Cambia ANALYSIS_MODEL / CHAT_MODEL en .env.")
    except anthropic.BadRequestError as e:
        if "credit" in str(e).lower():
            raise FatalError("Tu cuenta de Anthropic no tiene saldo. Agrega creditos en console.anthropic.com.")
        raise
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


# ═══════════════════════════════════════════════════════════════
# PERSISTENCIA (un directorio por analisis: data/<id>/meta.json)
# ═══════════════════════════════════════════════════════════════

def adir(aid):
    return DATA / aid


def load_meta(aid):
    p = adir(aid) / "meta.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_meta(meta):
    d = adir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    with _meta_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, d / "meta.json")


def mark_interrupted():
    """Si el servidor se reinicio a mitad de un analisis, lo marca como interrumpido."""
    for p in DATA.glob("*/meta.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m.get("status") in ("queued", "running"):
                m["status"] = "error"
                m["error"] = "El analisis se interrumpio (el servidor se reinicio). Vuelve a lanzarlo."
                save_meta(m)
        except Exception:
            pass


def norm(text):
    """Para comparar nombres de equipo sin importar mayusculas, tildes ni espacios."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().lower()


# ═══════════════════════════════════════════════════════════════
# YOUTUBE
# ═══════════════════════════════════════════════════════════════

def clean_youtube_url(url):
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise ValueError("El enlace debe ser de YouTube (youtube.com o youtu.be).")
    vid = None
    parts = [s for s in p.path.split("/") if s]
    if host == "youtu.be" and parts:
        vid = parts[0]
    elif "v" in parse_qs(p.query):
        vid = parse_qs(p.query)["v"][0]
    elif len(parts) >= 2 and parts[0] in ("live", "embed", "shorts"):
        vid = parts[1]
    if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise ValueError("No pude leer el ID del video en ese enlace.")
    return f"https://www.youtube.com/watch?v={vid}"


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def mmss(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def cookie_args():
    """Cookies de YouTube para evitar el bloqueo 'Sign in to confirm you're not a bot'.
    COOKIES_FILE (cookies.txt) tiene prioridad; si no, COOKIES_BROWSER (firefox, chrome, edge...)."""
    f = os.getenv("COOKIES_FILE", "").strip().strip('"')
    if f:
        if not Path(f).is_file():
            raise FatalError(f"COOKIES_FILE apunta a un archivo que no existe: {f}")
        return ["--cookies", f]
    b = os.getenv("COOKIES_BROWSER", "").strip().lower()
    if b:
        if b not in ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium", "safari"):
            raise FatalError(f"COOKIES_BROWSER='{b}' no es valido. Usa: firefox, chrome, edge, brave...")
        return ["--cookies-from-browser", b]
    return []


def kill_tree(proc):
    """Mata yt-dlp y los procesos que lanzo (ffmpeg), para que cancelar de verdad corte la descarga."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_ytdlp(args, job=None, timeout=None):
    """Ejecuta yt-dlp. Si el job se cancela (o pasa el timeout) mata el proceso y sus hijos."""
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    if os.name != "nt":
        kw["start_new_session"] = True      # grupo propio -> se puede matar todo el arbol
    proc = subprocess.Popen(YTDLP_CMD + args, **kw)
    if job is not None:
        with job.lock:
            job.proc = proc
        if job.cancel.is_set():
            kill_tree(proc)
    out = {}

    def reader():
        out["stdout"], out["stderr"] = proc.communicate()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.time() + timeout if timeout else None
    try:
        while t.is_alive():
            t.join(0.25)
            if job is not None and job.cancel.is_set():
                kill_tree(proc)
                t.join(5)
                raise Cancelled()
            if deadline and time.time() > deadline:
                kill_tree(proc)
                t.join(5)
                raise RuntimeError("YouTube tardo demasiado en responder. Intenta de nuevo.")
    finally:
        if job is not None:
            with job.lock:
                job.proc = None
    return SimpleNamespace(returncode=proc.returncode, stdout=out.get("stdout", ""), stderr=out.get("stderr", ""))


def fetch_section(url, start_s, end_s, out_dir, job=None, max_height=720, timeout=None, prefix="video"):
    """Descarga SOLO el tramo pedido (solo video; el audio no hace falta)."""
    if shutil.which("ffmpeg") is None:
        raise FatalError("ffmpeg no esta instalado. Instalalo con:  winget install Gyan.FFmpeg  "
                         "y reinicia VS Code / la terminal.")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(prefix + ".*"):
        old.unlink()
    h = max_height
    fmt = f"bestvideo[height<={h}][vcodec^=avc1]/bestvideo[height<={h}][ext=mp4]/bestvideo[height<={h}]/best[height<={h}]"
    base_args = cookie_args() + [
        "--no-playlist", "-f", fmt,
        "--download-sections", f"*{hms(start_s)}-{hms(end_s)}",
        "-o", str(out_dir / (prefix + ".%(ext)s")),
        url,
    ]
    attempts = []
    if shutil.which("node"):
        # yt-dlp reciente: node + script solver del desafio JS de YouTube (se baja de GitHub)
        attempts.append(["--js-runtimes", "node", "--remote-components", "ejs:github"] + base_args)
        attempts.append(["--js-runtimes", "node"] + base_args)  # versiones intermedias
    attempts.append(base_args)
    last = None
    for args in attempts:
        last = run_ytdlp(args, job=job, timeout=timeout)
        if last.returncode == 0:
            break
        if "no such option" not in (last.stderr or "").lower():
            break  # error real, no de version de yt-dlp
    if last.returncode != 0:
        raw = (last.stderr or last.stdout or "").strip()
        tail = "\n".join(raw.splitlines()[-6:])
        low = raw.lower()
        if "No module named yt_dlp" in raw:
            raise FatalError("Falta yt-dlp. Ejecuta:  pip install -r requirements.txt")
        if "not a bot" in low or "sign in to confirm" in low:
            if cookie_args():
                raise FatalError(
                    "YouTube sigue pidiendo confirmar que no eres un robot, aun usando tus cookies. "
                    "Abre YouTube en ese navegador, inicia sesion, reproduce un video y vuelve a intentar. "
                    "Si usas Chrome o Edge prueba con Firefox, o exporta un archivo cookies.txt "
                    "(ver LEEME.md).\n\nDetalle de yt-dlp:\n" + tail)
            raise FatalError(
                "YouTube bloqueo la descarga pidiendo iniciar sesion (proteccion anti-bot). "
                "Soluciona esto en 1 minuto: inicia sesion en YouTube en Firefox, agrega "
                "COOKIES_BROWSER=firefox al archivo .env y reinicia la app (ver LEEME.md).")
        if "challenge" in low or "needs to be reloaded" in low or "javascript runtime" in low:
            raise FatalError(
                "yt-dlp no pudo resolver el desafio de JavaScript de YouTube. Prueba, en este orden:\n"
                "1) Actualizar yt-dlp:  python -m pip install -U yt-dlp   (y reiniciar la app)\n"
                "2) Verificar Node.js:  node --version   (si no responde, reinstalalo y reabre la terminal)\n"
                "3) Revisar tu conexion: yt-dlp baja un script de github.com la primera vez.\n\n"
                "Detalle de yt-dlp:\n" + tail)
        if "could not copy" in low and "cookie" in low:
            raise FatalError(
                "No pude leer las cookies del navegador. Cierra por completo ese navegador "
                "(tambien en la bandeja del sistema) y reintenta, o usa Firefox / un cookies.txt.\n\n"
                + tail)
        raise RuntimeError("yt-dlp no pudo descargar el tramo:\n" + tail)
    files = [f for f in sorted(out_dir.glob(prefix + ".*")) if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("yt-dlp termino sin error pero no genero el archivo de video.")
    return files[0]


def download_section(url, start_min, dur_min, out_dir, job=None):
    return fetch_section(url, start_min * 60, (start_min + dur_min) * 60, out_dir, job=job)


# ═══════════════════════════════════════════════════════════════
# FRAMES: busqueda de planos generales
# ═══════════════════════════════════════════════════════════════

def grass_ratio(frame):
    """Fraccion del fotograma cubierta por cancha (verde). Un plano general tiene mucha;
    primeros planos, publico, banco y repeticiones con graficos tienen poca."""
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (320, max(1, int(h * 320 / w))))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (30, 35, 35), (90, 255, 255))
    return float(mask.mean() / 255.0)


def save_jpg(path, frame, max_w=1280, quality=85):
    h, w = frame.shape[:2]
    if w > max_w:
        frame = cv2.resize(frame, (max_w, int(h * max_w / w)))
    cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, quality])


def spread(items, n):
    """n elementos repartidos de forma pareja a lo largo de una lista ordenada en el tiempo."""
    if len(items) <= n:
        return list(items)
    idx = sorted(set(int(round(x)) for x in np.linspace(0, len(items) - 1, n)))
    return [items[i] for i in idx]


def extract_frames(video_path, n, out_dir, start_min, dur_min, job=None):
    """Revisa ~4n tomas repartidas por el tramo, descarta las que no son planos generales
    (sin llamar a la API) y se queda con n repartidas en el tiempo.
    La hora mostrada es la del VIDEO de YouTube."""
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total / fps if total > 0 else dur_min * 60
    if duration <= 0:
        raise RuntimeError("No pude leer el video descargado.")
    n_cand = min(MAX_CANDIDATES, max(n, n * CANDIDATE_FACTOR))
    cands = []
    for i in range(n_cand):
        if job is not None and job.cancel.is_set():
            raise Cancelled()
        t = (i + 0.5) * duration / n_cand
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if ok:
            cands.append({"t": t, "ratio": grass_ratio(frame)})
    if not cands:
        raise RuntimeError("No se pudo extraer ningun frame del video.")
    wide = [c for c in cands if c["ratio"] >= GRASS_MIN]
    fallback = False
    if len(wide) >= n:
        pick = spread(wide, n)
    elif len(wide) >= MIN_FRAMES:
        pick = wide
    else:  # casi no hay planos generales: se toman los 3 con mas cancha para no quedar sin nada
        fallback = True
        pick = sorted(sorted(cands, key=lambda c: -c["ratio"])[:MIN_FRAMES], key=lambda c: c["t"])
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, c in enumerate(pick):
        cap.set(cv2.CAP_PROP_POS_MSEC, c["t"] * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        name = f"frame_{i:02d}.jpg"
        save_jpg(out_dir / name, frame)
        sec = start_min * 60 + c["t"]
        frames.append({"file": name, "time": mmss(sec), "seconds": int(sec), "grass": round(c["ratio"], 2)})
    cap.release()
    if not frames:
        raise RuntimeError("No se pudo extraer ningun frame del video.")
    stats = {"reviewed": len(cands), "wide": len(wide), "kept": len(frames), "fallback": fallback}
    return frames, stats


def auto_frames(duration_min):
    """Cuantos frames analizar cuando el usuario no lo fija: 1 cada 30 s de video."""
    return max(MIN_AUTO_FRAMES, min(MAX_FRAMES, int(duration_min * FRAMES_PER_MIN)))


# ═══════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════

def team_block(m):
    pos = "local" if m["position"] == "local" else "visitante"
    lines = [f"Equipo a analizar: {m['team']} (juega de {pos}).",
             f"Color de camiseta de {m['team']}: {m['color']}."]
    if m.get("rival_color"):
        lines.append(f"El equipo rival viste de {m['rival_color']}.")
    lines.append("Identifica a ese equipo SOLO por el color de camiseta "
                 "(el lado de la cancha cambia entre tiempos; no lo uses).")
    return "\n".join(lines)


def frame_prompt(m, time_label):
    return f"""Eres analista tactico de futbol. Observa este fotograma (minuto {time_label} del video).

{team_block(m)}

Si el fotograma NO sirve para analisis tactico (repeticion, primer plano, publico, banco, cartel, \
transicion de pantalla, o no se distingue al equipo), responde unicamente: "FRAME NO UTIL: <motivo breve>".

Si sirve, describe SOLO lo que se ve, sin inventar, con estas secciones breves:
- Fase del juego: (ataque posicional, defensa organizada, transicion, balon parado, etc.)
- Formacion / lineas visibles: (que jugadores se ven y como se distribuyen; aclara si el plano no muestra toda la cancha)
- Defensa: (zona u hombre, altura de la linea, presion, espacios)
- Construccion / ataque: (por donde progresan, ancho, velocidad)
- Jugadores identificables: (numero de camiseta SOLO si es legible)
- Debilidades u oportunidades visibles:
Termina con "Confianza: alta/media/baja"."""


def consolidation_prompt(m, useful):
    obs = "\n\n".join(f"[min {f['time']}]\n{f['analysis']}" for f in useful)
    return f"""Eres analista tactico de un cuerpo tecnico. A partir de {len(useful)} observaciones de fotogramas \
de un partido, elabora el informe del equipo {m['team']} (camiseta {m['color']}, {m['position']}).

OBSERVACIONES:
{obs}

Redacta en espanol, en Markdown, con estas secciones:
## Resumen
## Formacion y estructura
## Fase defensiva
## Construccion y ataque
## Jugadores clave
## Fortalezas
## Debilidades a explotar
## Como enfrentarlo
## Limitaciones del analisis

Reglas: cita el minuto entre parentesis cuando respaldes una afirmacion, por ejemplo (min 43:10). \
Distingue lo observado de lo inferido: una formacion deducida de pocos planos de TV es una hipotesis, dilo. \
No inventes nombres ni numeros que no aparezcan en las observaciones. No uses tablas."""


def block_prompt(m, chunk):
    obs = "\n\n".join(f"[min {f['time']}]\n{f['analysis']}" for f in chunk)
    return f"""Eres analista tactico de un cuerpo tecnico. RESUMEN DE BLOQUE: te paso {len(chunk)} observaciones de \
fotogramas del equipo {m['team']} (camiseta {m['color']}, {m['position']}) entre los minutos {chunk[0]['time']} y \
{chunk[-1]['time']} del video.

OBSERVACIONES:
{obs}

Resume SOLO lo que muestran, en espanol y Markdown, en 250 a 400 palabras, con estos puntos:
- Formacion y estructura: cual aparece y en cuantas observaciones (por ejemplo "4-3-3 en 6 de 9")
- Defensa: bloque, altura de la linea, presion, espacios
- Construccion y ataque
- Jugadores identificables: solo numeros de camiseta legibles
- Cambios dentro del bloque o situaciones raras
- Debilidades visibles
Cita los minutos entre parentesis. Distingue lo observado de lo inferido. No inventes nada. No uses tablas."""


def long_consolidation_prompt(m, blocks):
    txt = "\n\n".join(f"### Bloque {i}: minutos {b['from']} a {b['to']} ({b['n']} frames)\n{b['summary']}"
                     for i, b in enumerate(blocks, 1))
    return f"""Eres analista tactico de un cuerpo tecnico. A partir de {len(blocks)} resumenes por bloque de tiempo \
de un tramo largo de partido, elabora el informe del equipo {m['team']} (camiseta {m['color']}, {m['position']}).

RESUMENES POR BLOQUE:
{txt}

Redacta en espanol, en Markdown, con estas secciones:
## Resumen
## Evolucion durante el tramo
## Formacion y estructura
## Fase defensiva
## Construccion y ataque
## Jugadores clave
## Fortalezas
## Debilidades a explotar
## Como enfrentarlo
## Limitaciones del analisis

Reglas: da mas peso a lo que se repite en varios bloques y marca con su minuto los cambios entre bloques \
(formacion, intensidad de presion, altura de linea). Cita minutos entre parentesis, por ejemplo (min 43:10). \
Distingue lo observado de lo inferido: una formacion deducida de planos de TV es una hipotesis, dilo. \
No inventes nombres ni numeros que no aparezcan en los resumenes. No uses tablas."""


def summarize_blocks(meta, useful, job, log):
    """Informe de un tramo largo: resume por bloques de tiempo (en paralelo) y luego los unifica.
    Resumir por partes evita que se pierda detalle cuando hay decenas de observaciones."""
    k = -(-len(useful) // BLOCK_SIZE)                 # techo
    size = -(-len(useful) // k)
    chunks = [useful[i:i + size] for i in range(0, len(useful), size)]
    results = [None] * len(chunks)

    def work(i):
        if job.cancel.is_set():
            raise Cancelled()
        return claude_text(ANALYSIS_MODEL, [{"role": "user", "content": block_prompt(meta, chunks[i])}],
                           max_tokens=1300)

    done = 0
    with ThreadPoolExecutor(max_workers=min(ANALYSIS_WORKERS, len(chunks))) as pool:
        futs = {pool.submit(work, i): i for i in range(len(chunks))}
        for fut in as_completed(futs):
            try:
                results[futs[fut]] = fut.result()
            except (FatalError, Cancelled):
                for other in futs:
                    other.cancel()
                raise
            done += 1
            log(f"Resumen por bloques: {done}/{len(chunks)}", 90 + int(6 * done / len(chunks)))
    return [{"from": c[0]["time"], "to": c[-1]["time"], "n": len(c), "summary": r}
            for c, r in zip(chunks, results)]


def source_blocks(m):
    """Texto con el informe de cada fuente de un analisis combinado."""
    matches, parts = {}, []
    for i, s in enumerate(m["sources"], 1):
        pk = matches.setdefault(s["clean_url"], len(matches) + 1)
        pos = "local" if s["position"] == "local" else "visitante"
        end = s["start_minute"] + s["duration_minutes"]
        parts.append(f"### Fuente F{i} (partido P{pk}, {pos}, camiseta {s['color']}, "
                     f"minutos {s['start_minute']}-{end} del video)\n{s['report']}")
    return "\n\n".join(parts), len(matches)


def combine_prompt(m):
    blocks, n_matches = source_blocks(m)
    return f"""Eres analista tactico de un cuerpo tecnico. Te paso {len(m['sources'])} informes parciales sobre \
el mismo equipo, {m['team']}, de {n_matches} partido(s) distintos (P1, P2...; una misma P puede tener varios tramos). \
Elabora UN informe unificado.

{blocks}

Redacta en espanol, en Markdown, con estas secciones:
## Resumen
## Lo que se repite (patrones consistentes)
## Variaciones entre partidos o tramos
## Formacion y estructura
## Fase defensiva
## Construccion y ataque
## Jugadores clave
## Fortalezas
## Debilidades a explotar
## Como enfrentarlo
## Limitaciones del analisis

Reglas: da mas peso a lo que aparece en varias fuentes y marca lo que solo aparece en una. \
Cita la fuente y el minuto cuando respaldes algo, por ejemplo (F2, min 43:10). \
Si las fuentes se contradicen, dilo en lugar de elegir una. No inventes datos que no esten en los informes. \
No uses tablas. En las limitaciones indica cuantos partidos y tramos hay detras de las conclusiones."""


def chat_system(m):
    if m.get("kind") == "combined":
        blocks, n_matches = source_blocks(m)
        return f"""Eres el analista tactico de un cuerpo tecnico de futbol. Respondes en espanol, claro y practico.

Contexto: informe combinado de {m['team']} a partir de {len(m['sources'])} analisis de {n_matches} partido(s) \
(F1, F2... son las fuentes; P1, P2... los partidos).

Reglas:
- Basa las respuestas en los informes de abajo y cita la fuente y el minuto cuando puedas.
- Si no alcanzan para responder, dilo y sugiere que tramo o partido analizar.
- Distingue lo observado de lo inferido. No inventes jugadores, numeros ni estadisticas. No uses tablas.

=== INFORME COMBINADO ===
{m['report']}

=== INFORMES DE CADA FUENTE ===
{blocks}"""
    useful = [f for f in m["frames"] if f.get("ok")]
    obs = "\n\n".join(f"[min {f['time']}] {f['analysis']}" for f in useful)
    blocks = m.get("blocks") or []
    blocks_txt = ""
    if blocks:
        blocks_txt = "\n\n=== RESUMENES POR BLOQUE DE TIEMPO ===\n" + "\n\n".join(
            f"[Bloque {i}: min {b['from']} a {b['to']}]\n{b['summary']}" for i, b in enumerate(blocks, 1))
    if len(obs) > 280_000:   # ~70k tokens: en tramos enormes se deja solo el resumen por bloques
        obs = "(omitidas por tamano: usa el informe y los resumenes por bloque)"
    return f"""Eres el analista tactico de un cuerpo tecnico de futbol. Respondes en espanol, claro y practico.

Contexto: analisis de {m['team']} (camiseta {m['color']}, {m['position']}) hecho a partir de fotogramas del video \
{m['clean_url']}, tramo de los minutos {m['start_minute']} a {m['start_minute'] + m['duration_minutes']} del video.

Reglas:
- Basa las respuestas en el analisis de abajo y cita el minuto cuando puedas.
- Si el analisis no alcanza para responder, dilo y sugiere que tramo analizar o que dato falta.
- Distingue lo observado de lo inferido. No inventes jugadores, numeros ni estadisticas. No uses tablas.

=== INFORME CONSOLIDADO ===
{m['report']}{blocks_txt}

=== OBSERVACIONES POR FRAME ===
{obs}"""


# ═══════════════════════════════════════════════════════════════
# TRABAJOS EN SEGUNDO PLANO
# ═══════════════════════════════════════════════════════════════

def is_discarded(text):
    """True si Claude marco el frame como 'FRAME NO UTIL' (con o sin tilde, con o sin negritas)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    return re.match(r"^[\W_]*FRAME NO UTIL", plain) is not None


def analyze_frame(m, frame, frames_dir, job):
    if job.cancel.is_set():
        raise Cancelled()
    with open(frames_dir / frame["file"], "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": frame_prompt(m, frame["time"])},
    ]
    return claude_text(ANALYSIS_MODEL, [{"role": "user", "content": content}], max_tokens=900)


def make_logger(meta):
    def log(msg, progress=None):
        meta["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
        if progress is not None:
            meta["progress"] = progress
        save_meta(meta)

    def phase(name):
        meta["phase"] = name
        meta["phase_ts"] = time.time()

    return log, phase


def run_job(aid):
    job = JOBS.setdefault(aid, Job())
    meta = load_meta(aid)
    d = adir(aid)
    log, phase = make_logger(meta)

    def check():
        if job.cancel.is_set():
            raise Cancelled()

    try:
        meta["status"] = "running"
        phase("download")
        log(f"Descargando minutos {meta['start_minute']}-{meta['start_minute'] + meta['duration_minutes']} del video...", 5)
        video = download_section(meta["clean_url"], meta["start_minute"], meta["duration_minutes"], d, job=job)
        check()

        phase("filter")
        log("Buscando planos generales (sin gastar en la API)...", 35)
        frames_dir = d / "frames"
        frames, stats = extract_frames(video, meta["n_frames"], frames_dir, meta["start_minute"],
                                       meta["duration_minutes"], job=job)
        try:
            video.unlink()  # ya no se necesita; ahorra espacio
        except OSError:
            pass
        meta["frames"], meta["filter"] = frames, stats
        msg = (f"Se revisaron {stats['reviewed']} tomas: {stats['wide']} son planos generales. "
               f"Se analizan {stats['kept']} (las demas no se envian a Claude).")
        if stats["fallback"]:
            msg = (f"Casi no hay planos generales en este tramo ({stats['wide']} de {stats['reviewed']}); "
                   f"se analizan los {stats['kept']} con mas cancha visible. Prueba otro minuto.")
        elif stats["kept"] < meta["n_frames"]:
            msg += f" Se pedian {meta['n_frames']}: no hubo mas planos generales en el tramo."
        log(msg + f" Analizando con {ANALYSIS_MODEL}...", 40)
        check()

        phase("analysis")
        done = 0
        last_err = ""
        with ThreadPoolExecutor(max_workers=ANALYSIS_WORKERS) as pool:
            futs = {pool.submit(analyze_frame, meta, fr, frames_dir, job): fr for fr in frames}
            for fut in as_completed(futs):
                fr = futs[fut]
                try:
                    text = fut.result()
                    fr["analysis"] = text
                    fr["ok"] = not is_discarded(text)
                except (FatalError, Cancelled):
                    for other in futs:
                        other.cancel()
                    raise
                except Exception as e:  # fallo puntual de un frame (los reintentos de la API ya se agotaron)
                    last_err = str(e)
                    fr["analysis"] = f"(no se pudo analizar este frame: {e})"
                    fr["ok"] = False
                    fr["failed"] = True
                done += 1
                state = "util" if fr["ok"] else ("FALLO" if fr.get("failed") else "descartado por Claude")
                log(f"Frame {done}/{len(frames)} (min {fr['time']}): {state}", 40 + int(50 * done / len(frames)))
        check()

        failed = [f for f in frames if f.get("failed")]
        if failed and len(failed) == len(frames):
            raise RuntimeError(f"No se pudo analizar ningun frame. Ultimo error: {last_err}")
        if failed:
            log(f"ATENCION: {len(failed)} de {len(frames)} frames no se pudieron analizar ({last_err[:120]}). "
                f"El informe se hace con los demas; conviene repetir el analisis si son muchos.")
        useful = [f for f in sorted(frames, key=lambda x: x["seconds"]) if f["ok"]]
        if not useful:
            raise RuntimeError("Ningun frame sirvio para analisis tactico (repeticiones, primeros planos...). "
                               "Prueba otro tramo, o revisa el color de camiseta.")
        phase("report")
        if len(useful) > LONG_THRESHOLD:
            log(f"Tramo largo ({len(useful)} frames utiles): resumiendo por bloques de tiempo antes del informe final...", 90)
            meta["blocks"] = summarize_blocks(meta, useful, job, log)
            check()
            log("Redactando el informe final a partir de los bloques...", 97)
            prompt = long_consolidation_prompt(meta, meta["blocks"])
            report = claude_text(ANALYSIS_MODEL, [{"role": "user", "content": prompt}], max_tokens=4000)
        else:
            log(f"Consolidando informe con {len(useful)} frames utiles...", 92)
            report = claude_text(ANALYSIS_MODEL, [{"role": "user", "content": consolidation_prompt(meta, useful)}],
                                 max_tokens=2500)
        check()
        meta["report"] = report
        meta["status"] = "done"
        meta["phase"] = ""
        log("Listo. Ya puedes hacer preguntas en el chat.", 100)
    except Cancelled:
        meta["status"] = "cancelled"
        meta["phase"] = ""
        meta["error"] = ""
        for f in d.glob("video.*"):
            try:
                f.unlink()
            except OSError:
                pass
        log("Cancelado por el usuario.")
    except Exception as e:
        meta["status"] = "error"
        meta["phase"] = ""
        meta["error"] = str(e)
        save_meta(meta)
    finally:
        JOBS.pop(aid, None)


def run_combined(aid):
    job = JOBS.setdefault(aid, Job())
    meta = load_meta(aid)
    log, phase = make_logger(meta)
    try:
        meta["status"], meta["error"], meta["report"] = "running", "", ""
        phase("report")
        log(f"Combinando {len(meta['sources'])} analisis de {meta['team']}...", 30)
        report = claude_text(ANALYSIS_MODEL, [{"role": "user", "content": combine_prompt(meta)}], max_tokens=3500)
        if job.cancel.is_set():
            raise Cancelled()
        meta["report"] = report
        meta["status"] = "done"
        meta["phase"] = ""
        log("Listo. Ya puedes hacer preguntas en el chat.", 100)
    except Cancelled:
        meta["status"], meta["phase"] = "cancelled", ""
        log("Cancelado por el usuario.")
    except Exception as e:
        meta["status"], meta["phase"], meta["error"] = "error", "", str(e)
        save_meta(meta)
    finally:
        JOBS.pop(aid, None)


def stop_job(aid, wait=10):
    """Cancela un trabajo en curso y espera a que termine."""
    job = JOBS.get(aid)
    if job is None:
        return
    job.cancel.set()
    with job.lock:
        p = job.proc
    kill_tree(p)
    t0 = time.time()
    while aid in JOBS and time.time() - t0 < wait:
        time.sleep(0.1)


# ═══════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════

def valid_id(aid):
    if not ID_RE.match(aid or ""):
        abort(404)
    if not (adir(aid) / "meta.json").exists():
        abort(404)
    return aid


def public(meta):
    keys = ("id", "created", "team", "position", "color", "start_minute", "duration_minutes", "status")
    out = {k: meta.get(k) for k in keys}
    out["kind"] = meta.get("kind", "analysis")
    out["n_sources"] = len(meta.get("sources", []))
    return out


def to_int(value, default):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise FormError("Revisa los numeros del formulario (minuto, duracion, frames).")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify({
        "has_key": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "model": ANALYSIS_MODEL,
        "chat_model": CHAT_MODEL,
        "max_duration": MAX_DURATION_MIN,
        "max_combine": MAX_COMBINE,
        "frames": {"min": MIN_FRAMES, "max": MAX_FRAMES, "per_min": FRAMES_PER_MIN, "min_auto": MIN_AUTO_FRAMES},
    })


@app.get("/api/analisis")
def list_analyses():
    items = []
    for p in DATA.glob("*/meta.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                items.append(public(json.load(f)))
        except Exception:
            continue
    items.sort(key=lambda m: m.get("created") or "", reverse=True)
    return jsonify(items)


@app.post("/api/analisis")
def create_analysis():
    b = request.get_json(silent=True) or {}
    try:
        clean = clean_youtube_url(b.get("url"))
        team = (b.get("team") or "").strip()
        color = (b.get("color") or "").strip()
        if not team:
            raise FormError("Escribe el nombre del equipo a analizar.")
        if not color:
            raise FormError("Indica el color de camiseta del equipo (asi lo distingue del rival).")
        position = b.get("position") if b.get("position") in ("local", "visitante") else "local"
        start = to_int(b.get("start_minute"), 0)
        dur = to_int(b.get("duration_minutes"), 10)
        n = to_int(b.get("n_frames"), None)       # vacio = automatico
        if start < 0:
            raise FormError("El minuto de inicio no puede ser negativo.")
        if not 1 <= dur <= MAX_DURATION_MIN:
            raise FormError(f"La duracion debe estar entre 1 y {MAX_DURATION_MIN} minutos.")
        n_auto = n is None
        if n_auto:
            n = auto_frames(dur)
        elif not MIN_FRAMES <= n <= MAX_FRAMES:
            raise FormError(f"Los frames deben ser entre {MIN_FRAMES} y {MAX_FRAMES} (o deja el campo vacio para automatico).")
    except (FormError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    aid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    meta = {
        "id": aid, "kind": "analysis", "created": datetime.now().isoformat(timespec="seconds"),
        "url": b.get("url"), "clean_url": clean,
        "team": team[:60], "position": position, "color": color[:40],
        "rival_color": (b.get("rival_color") or "").strip()[:40],
        "start_minute": start, "duration_minutes": dur, "n_frames": n, "n_frames_auto": n_auto,
        "model": ANALYSIS_MODEL, "status": "queued", "progress": 0, "phase": "", "phase_ts": 0,
        "log": [], "frames": [], "filter": None, "blocks": [], "report": "", "chat": [], "error": "",
    }
    save_meta(meta)
    JOBS[aid] = Job()
    threading.Thread(target=run_job, args=(aid,), daemon=True).start()
    return jsonify({"id": aid}), 201


@app.post("/api/combinar")
def combine():
    b = request.get_json(silent=True) or {}
    ids = b.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return jsonify({"error": "Selecciona los analisis a combinar."}), 400
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        return jsonify({"error": "Selecciona al menos 2 analisis para combinar."}), 400
    if len(ids) > MAX_COMBINE:
        return jsonify({"error": f"Puedes combinar hasta {MAX_COMBINE} analisis a la vez."}), 400
    sources = []
    for i in ids:
        m = load_meta(i) if ID_RE.match(i) else None
        if m is None:
            return jsonify({"error": "Uno de los analisis seleccionados ya no existe."}), 404
        if m.get("kind") == "combined":
            return jsonify({"error": "No se puede combinar un informe que ya es combinado."}), 400
        if m["status"] != "done":
            return jsonify({"error": f"El analisis de {m['team']} (min {m['start_minute']}) aun no termino."}), 400
        sources.append(m)
    teams = {norm(m["team"]) for m in sources}
    if len(teams) > 1:
        names = ", ".join(sorted({m["team"] for m in sources}))
        return jsonify({"error": f"Solo se pueden combinar analisis del mismo equipo (elegiste: {names})."}), 400
    sources.sort(key=lambda m: m["created"])
    first = sources[0]
    aid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    meta = {
        "id": aid, "kind": "combined", "created": datetime.now().isoformat(timespec="seconds"),
        "url": "", "clean_url": "", "team": first["team"], "position": first["position"], "color": first["color"],
        "rival_color": "", "start_minute": 0, "duration_minutes": 0, "n_frames": 0,
        "model": ANALYSIS_MODEL, "status": "queued", "progress": 0, "phase": "", "phase_ts": 0,
        "log": [], "frames": [], "filter": None, "report": "", "chat": [], "error": "",
        "sources": [{
            "id": m["id"], "created": m["created"], "team": m["team"], "position": m["position"],
            "color": m["color"], "clean_url": m["clean_url"], "start_minute": m["start_minute"],
            "duration_minutes": m["duration_minutes"], "n_useful": sum(1 for f in m["frames"] if f.get("ok")),
            "report": m["report"],
        } for m in sources],
    }
    save_meta(meta)
    JOBS[aid] = Job()
    threading.Thread(target=run_combined, args=(aid,), daemon=True).start()
    return jsonify({"id": aid}), 201


@app.get("/api/analisis/<aid>")
def get_analysis(aid):
    return jsonify(load_meta(valid_id(aid)))


@app.delete("/api/analisis/<aid>")
def delete_analysis(aid):
    valid_id(aid)
    stop_job(aid)
    shutil.rmtree(adir(aid), ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/analisis/<aid>/cancelar")
def cancel(aid):
    valid_id(aid)
    job = JOBS.get(aid)
    if job is None:
        return jsonify({"error": "No hay un analisis en curso para cancelar."}), 409
    job.cancel.set()
    with job.lock:
        p = job.proc
    kill_tree(p)
    return jsonify({"ok": True})


@app.post("/api/analisis/<aid>/regenerar")
def regenerate(aid):
    """Vuelve a generar un informe combinado que fallo o se cancelo."""
    meta = load_meta(valid_id(aid))
    if meta.get("kind") != "combined":
        return jsonify({"error": "Solo aplica a informes combinados."}), 400
    if meta["status"] not in ("error", "cancelled"):
        return jsonify({"error": "El informe ya esta en curso o terminado."}), 409
    meta.update(status="queued", error="", progress=0, log=[])
    save_meta(meta)
    JOBS[aid] = Job()
    threading.Thread(target=run_combined, args=(aid,), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/analisis/<aid>/chat")
def chat(aid):
    meta = load_meta(valid_id(aid))
    if meta["status"] != "done":
        return jsonify({"error": "El analisis todavia no termino."}), 409
    text = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    if not text:
        return jsonify({"error": "Escribe una pregunta."}), 400
    history = meta.get("chat", [])
    messages = [{"role": h["role"], "content": h["content"]} for h in history[-30:]]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    messages.append({"role": "user", "content": text[:4000]})
    try:
        answer = claude_text(CHAT_MODEL, messages, max_tokens=1400, system=chat_system(meta))
    except FatalError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"No pude consultar a Claude: {e}"}), 502
    meta = load_meta(aid)
    meta["chat"] = history + [{"role": "user", "content": text[:4000]},
                              {"role": "assistant", "content": answer}]
    save_meta(meta)
    return jsonify({"answer": answer})


@app.get("/api/analisis/<aid>/pdf")
def export_pdf(aid):
    meta = load_meta(valid_id(aid))
    if meta["status"] != "done":
        return jsonify({"error": "El analisis todavia no termino."}), 409
    with_frames = request.args.get("frames", "1") == "1"
    with_chat = request.args.get("chat", "0") == "1"
    try:
        import pdf_export
        data = pdf_export.build_pdf(meta, adir(aid) / "frames", with_frames, with_chat)
    except ImportError:
        return jsonify({"error": "Falta la libreria reportlab. Ejecuta:  python -m pip install -r requirements.txt"}), 500
    except Exception as e:
        return jsonify({"error": f"No pude crear el PDF: {e}"}), 500
    slug = re.sub(r"[^a-z0-9]+", "_", norm(meta["team"])).strip("_") or "equipo"
    name = f"informe_{slug}_{datetime.now():%Y%m%d}.pdf"
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=name)


@app.get("/media/<aid>/<path:name>")
def media(aid, name):
    valid_id(aid)
    return send_from_directory(adir(aid) / "frames", name)


if __name__ == "__main__":
    mark_interrupted()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Analizador de rivales -> {url}\n  (Ctrl+C para detener)\n")
    if os.getenv("NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
