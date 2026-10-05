#!/usr/bin/env python3
"""
ANALIZADOR DE RIVALES - interfaz web local

Uso:
    python app.py        ->  abre http://127.0.0.1:5000

Flujo: formulario (link de YouTube, minutos, equipo, color) -> descarga solo ese
tramo -> extrae frames -> Claude Vision analiza cada frame -> informe consolidado
-> chat para hacer preguntas sobre el analisis.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-haiku-4-5")
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-haiku-4-5")
PORT = int(os.getenv("PORT", "5000"))

MAX_DURATION_MIN = 30
MIN_FRAMES, MAX_FRAMES, DEFAULT_FRAMES = 3, 20, 8
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
        _client = anthropic.Anthropic(api_key=key)
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


# ═══════════════════════════════════════════════════════════════
# YOUTUBE + FRAMES
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


def run_ytdlp(args):
    return subprocess.run([sys.executable, "-m", "yt_dlp"] + args,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


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


def download_section(url, start_min, dur_min, out_dir):
    """Descarga SOLO el tramo pedido (solo video, max 720p; el audio no hace falta)."""
    if shutil.which("ffmpeg") is None:
        raise FatalError("ffmpeg no esta instalado. Instalalo con:  winget install Gyan.FFmpeg  "
                         "y reinicia VS Code / la terminal.")
    start_s, end_s = start_min * 60, (start_min + dur_min) * 60
    for old in out_dir.glob("video.*"):
        old.unlink()
    base_args = [
        "--no-playlist",
        "-f", "bestvideo[height<=720][vcodec^=avc1]/bestvideo[height<=720][ext=mp4]/bestvideo[height<=720]/best[height<=720]",
        "--download-sections", f"*{hms(start_s)}-{hms(end_s)}",
        "-o", str(out_dir / "video.%(ext)s"),
        url,
    ]
    base_args = cookie_args() + base_args
    attempts = []
    if shutil.which("node"):
        # yt-dlp reciente: node + script solver del desafio JS de YouTube (se baja de GitHub)
        attempts.append(["--js-runtimes", "node", "--remote-components", "ejs:github"] + base_args)
        attempts.append(["--js-runtimes", "node"] + base_args)  # versiones intermedias
    attempts.append(base_args)
    last = None
    for args in attempts:
        last = run_ytdlp(args)
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
    files = sorted(out_dir.glob("video.*"))
    files = [f for f in files if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("yt-dlp termino sin error pero no genero el archivo de video.")
    return files[0]


def extract_frames(video_path, n, out_dir, start_min, dur_min):
    """n frames repartidos uniformemente; la hora mostrada es la del VIDEO de YouTube."""
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total / fps if total > 0 else dur_min * 60
    if duration <= 0:
        raise RuntimeError("No pude leer el video descargado.")
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(n):
        t = (i + 0.5) * duration / n
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        if w > 1280:
            frame = cv2.resize(frame, (1280, int(h * 1280 / w)))
        name = f"frame_{i:02d}.jpg"
        cv2.imwrite(str(out_dir / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        sec = start_min * 60 + t
        frames.append({"file": name, "time": mmss(sec), "seconds": int(sec)})
    cap.release()
    if not frames:
        raise RuntimeError("No se pudo extraer ningun frame del video.")
    return frames


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
No inventes nombres ni numeros que no aparezcan en las observaciones."""


def chat_system(m):
    useful = [f for f in m["frames"] if f.get("ok")]
    obs = "\n\n".join(f"[min {f['time']}] {f['analysis']}" for f in useful)
    return f"""Eres el analista tactico de un cuerpo tecnico de futbol. Respondes en espanol, claro y practico.

Contexto: analisis de {m['team']} (camiseta {m['color']}, {m['position']}) hecho a partir de fotogramas del video \
{m['clean_url']}, tramo de los minutos {m['start_minute']} a {m['start_minute'] + m['duration_minutes']} del video.

Reglas:
- Basa las respuestas en el analisis de abajo y cita el minuto cuando puedas.
- Si el analisis no alcanza para responder, dilo y sugiere que tramo analizar o que dato falta.
- Distingue lo observado de lo inferido. No inventes jugadores, numeros ni estadisticas.

=== INFORME CONSOLIDADO ===
{m['report']}

=== OBSERVACIONES POR FRAME ===
{obs}"""


# ═══════════════════════════════════════════════════════════════
# TRABAJO EN SEGUNDO PLANO
# ═══════════════════════════════════════════════════════════════

def is_discarded(text):
    """True si Claude marco el frame como 'FRAME NO UTIL' (con o sin tilde, con o sin negritas)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    return re.match(r"^[\W_]*FRAME NO UTIL", plain) is not None


def analyze_frame(m, frame, frames_dir):
    with open(frames_dir / frame["file"], "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": frame_prompt(m, frame["time"])},
    ]
    return claude_text(ANALYSIS_MODEL, [{"role": "user", "content": content}], max_tokens=900)


def run_job(aid):
    meta = load_meta(aid)
    d = adir(aid)

    def log(msg, progress=None):
        meta["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
        if progress is not None:
            meta["progress"] = progress
        save_meta(meta)

    try:
        meta["status"] = "running"
        log(f"Descargando minutos {meta['start_minute']}-{meta['start_minute'] + meta['duration_minutes']} del video...", 5)
        video = download_section(meta["clean_url"], meta["start_minute"], meta["duration_minutes"], d)

        log("Extrayendo frames...", 35)
        frames_dir = d / "frames"
        frames = extract_frames(video, meta["n_frames"], frames_dir, meta["start_minute"], meta["duration_minutes"])
        try:
            video.unlink()  # ya no se necesita; ahorra espacio
        except OSError:
            pass
        meta["frames"] = frames
        log(f"{len(frames)} frames listos. Analizando con {ANALYSIS_MODEL}...", 40)

        done = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(analyze_frame, meta, fr, frames_dir): fr for fr in frames}
            for fut in as_completed(futs):
                fr = futs[fut]
                try:
                    text = fut.result()
                    fr["analysis"] = text
                    fr["ok"] = not is_discarded(text)
                except FatalError:
                    for other in futs:
                        other.cancel()
                    raise
                except Exception as e:  # fallo puntual de un frame
                    fr["analysis"] = f"(no se pudo analizar este frame: {e})"
                    fr["ok"] = False
                done += 1
                state = "util" if fr["ok"] else "descartado"
                log(f"Frame {done}/{len(frames)} (min {fr['time']}): {state}", 40 + int(50 * done / len(frames)))

        useful = [f for f in sorted(frames, key=lambda x: x["seconds"]) if f["ok"]]
        if not useful:
            raise RuntimeError("Ningun frame sirvio para analisis tactico (repeticiones, primeros planos...). "
                               "Prueba otro tramo, o revisa el color de camiseta.")
        log(f"Consolidando informe con {len(useful)} frames utiles...", 92)
        meta["report"] = claude_text(ANALYSIS_MODEL,
                                     [{"role": "user", "content": consolidation_prompt(meta, useful)}],
                                     max_tokens=2500)
        meta["status"] = "done"
        meta["progress"] = 100
        log("Listo. Ya puedes hacer preguntas en el chat.", 100)
    except Exception as e:
        meta["status"] = "error"
        meta["error"] = str(e)
        save_meta(meta)


# ═══════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════

def valid_id(aid):
    if not ID_RE.match(aid or ""):
        abort(404)
    if not (adir(aid) / "meta.json").exists():
        abort(404)
    return aid


def public(meta, full=True):
    if full:
        return meta
    keys = ("id", "created", "team", "position", "color", "start_minute", "duration_minutes", "status")
    return {k: meta.get(k) for k in keys}


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
        "frames": {"min": MIN_FRAMES, "max": MAX_FRAMES, "default": DEFAULT_FRAMES},
    })


@app.get("/api/analisis")
def list_analyses():
    items = []
    for p in DATA.glob("*/meta.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                items.append(public(json.load(f), full=False))
        except Exception:
            continue
    items.sort(key=lambda m: m.get("created") or "", reverse=True)
    return jsonify(items)


@app.post("/api/analisis")
def create_analysis():
    b = request.get_json(silent=True) or {}

    def to_int(value, default):
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise FormError("Revisa los numeros del formulario (minuto, duracion, frames).")

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
        n = to_int(b.get("n_frames"), DEFAULT_FRAMES)
        if start < 0:
            raise FormError("El minuto de inicio no puede ser negativo.")
        if not 1 <= dur <= MAX_DURATION_MIN:
            raise FormError(f"La duracion debe estar entre 1 y {MAX_DURATION_MIN} minutos.")
        if not MIN_FRAMES <= n <= MAX_FRAMES:
            raise FormError(f"Los frames deben ser entre {MIN_FRAMES} y {MAX_FRAMES}.")
    except (FormError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    aid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    meta = {
        "id": aid, "created": datetime.now().isoformat(timespec="seconds"),
        "url": b.get("url"), "clean_url": clean,
        "team": team[:60], "position": position, "color": color[:40],
        "rival_color": (b.get("rival_color") or "").strip()[:40],
        "start_minute": start, "duration_minutes": dur, "n_frames": n,
        "model": ANALYSIS_MODEL, "status": "queued", "progress": 0, "log": [],
        "frames": [], "report": "", "chat": [], "error": "",
    }
    save_meta(meta)
    threading.Thread(target=run_job, args=(aid,), daemon=True).start()
    return jsonify({"id": aid}), 201


@app.get("/api/analisis/<aid>")
def get_analysis(aid):
    return jsonify(load_meta(valid_id(aid)))


@app.delete("/api/analisis/<aid>")
def delete_analysis(aid):
    valid_id(aid)
    shutil.rmtree(adir(aid), ignore_errors=True)
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
