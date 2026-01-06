import os
import json
import time
import threading
import urllib.parse
import urllib.request
from collections import deque
from typing import Dict, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import gradium  # pip install gradium

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
app = FastAPI(title="Chess Coach PoC (2 humans + Mistral text + Gradium TTS)")

DEFAULT_VOICE_COACH_ID = os.getenv("VOICE_ID", "b35yykvVppLXyw_l")  # Elise (fr)
AI_LEVEL = int(os.getenv("AI_LEVEL", "3"))  # unused now, kept for compatibility

# Lichess: we create a "challenge open" game (two humans)
LICHESS_TOKEN = os.getenv("LICHESS_TOKEN")
GRADIUM_API_KEY = os.getenv("GRADIUM_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

# ------------------------------------------------------------
# SSE infra (per game)
# ------------------------------------------------------------
EVENT_SUBSCRIBERS: Dict[str, dict] = {}
EVENT_SUBSCRIBERS_LOCK = threading.Lock()

def get_event_queue(game_id: str):
    import asyncio
    loop = asyncio.get_running_loop()
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if entry and entry["loop"] is loop:
            entry["subscribers"] += 1
            return entry["queue"]
        q: asyncio.Queue = asyncio.Queue()
        EVENT_SUBSCRIBERS[game_id] = {"queue": q, "loop": loop, "subscribers": 1}
        return q

def release_event_queue(game_id: str):
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if not entry:
            return
        entry["subscribers"] -= 1
        if entry["subscribers"] <= 0:
            EVENT_SUBSCRIBERS.pop(game_id, None)

def publish_event(game_id: str, event: str, payload: dict):
    import asyncio
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if not entry:
            return
        q = entry["queue"]
        loop = entry["loop"]
    msg = {"event": event, "payload": payload}
    try:
        asyncio.run_coroutine_threadsafe(q.put(msg), loop)
    except RuntimeError:
        pass

# ------------------------------------------------------------
# Commentary queue (simple FIFO)
# ------------------------------------------------------------
COMMENTARY_QUEUE: deque[tuple[str, str]] = deque()  # (game_id, move_uci)
COMMENTARY_LOCK = threading.Lock()
COMMENTARY_WORKER_ACTIVE = False

# ------------------------------------------------------------
# Game contexts
# ------------------------------------------------------------
GAME_CONTEXTS: Dict[str, dict] = {}
GAME_CONTEXTS_LOCK = threading.Lock()

# ------------------------------------------------------------
# Mistral commentary (VERY simple, safe fallback)
# ------------------------------------------------------------
def generate_commentary(move_uci: str) -> str:
    """
    Minimal: try Mistral if key exists, else fallback deterministic.
    We keep it simple and TTS-friendly.
    """
    if not MISTRAL_API_KEY:
        return f"Coup joué {move_uci}. Bien vu, continue comme ça."

    payload = {
        "model": "mistral-small-latest",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu es un coach d echecs vocal, fun et tres oral. "
                    "Reponds en francais. 1 phrase, 2 max. "
                    "Pas d emojis. Pas de guillemets typographiques. "
                    "Texte simple, lisible a voix haute."
                ),
            },
            {"role": "user", "content": f"Coup UCI: {move_uci}. Commente ce coup simplement."},
        ],
    }

    try:
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
        text = (text or "").strip()
        if text:
            return text
    except Exception:
        pass

    return f"Coup joué {move_uci}. Bien vu, continue comme ça."

# ------------------------------------------------------------
# Gradium TTS (HTTP simple via gradium python SDK)
# ------------------------------------------------------------
async def gradium_tts_wav(text: str, voice_id: str) -> bytes:
    if not GRADIUM_API_KEY:
        raise RuntimeError("Missing GRADIUM_API_KEY")

    client = gradium.client.GradiumClient(api_key=GRADIUM_API_KEY)
    result = await client.tts(
        setup={
            "model_name": "default",
            "voice_id": voice_id,
            "output_format": "wav",
        },
        text=text,
    )
    return result.raw_data

# ------------------------------------------------------------
# Lichess create "open challenge" (two humans)
# ------------------------------------------------------------
def create_lichess_human_game() -> dict:
    """
    Create a Lichess open challenge (anyone can join) so that two humans can play.
    We'll ask for 10+0 and casual (not rated).
    """
    if not LICHESS_TOKEN:
        raise HTTPException(status_code=500, detail="LICHESS_TOKEN not set")

    # Lichess endpoint: /api/challenge/open
    # We do a casual game with 10+0.
    url = "https://lichess.org/api/challenge/open"

    form = {
        "rated": "false",
        "clock.limit": "600",
        "clock.increment": "0",
        # You can add "name" or "variant" if needed.
    }
    data = urllib.parse.urlencode(form).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {LICHESS_TOKEN}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Lichess open challenge failed: {exc}")

    # Typical response contains "challenge": {"id": "...", ...}
    challenge = payload.get("challenge", payload)
    game_id = challenge.get("id") or payload.get("id")
    if not game_id:
        raise HTTPException(status_code=502, detail="Lichess response missing id")

    # Open challenge link is like https://lichess.org/<id>
    game_url = f"https://lichess.org/{game_id}"
    embed_url = f"https://lichess.org/embed/{game_id}?theme=blue&bg=light"

    return {"game_id": game_id, "game_url": game_url, "embed_url": embed_url}

# ------------------------------------------------------------
# Stream moves from lichess game (board API stream)
# ------------------------------------------------------------
def stream_game_thread(game_id: str) -> None:
    """
    Stream game events and enqueue every new move UCI.
    For open challenge, stream will start once game begins.
    """
    if not LICHESS_TOKEN:
        return

    stream_url = f"https://lichess.org/api/board/game/stream/{game_id}"
    headers = {
        "Authorization": f"Bearer {LICHESS_TOKEN}",
        "Accept": "application/json",
    }

    last_processed = 0

    try:
        with requests.get(stream_url, headers=headers, stream=True, timeout=90) as resp:
            resp.raise_for_status()

            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    ev = json.loads(line.decode("utf-8"))
                except Exception:
                    continue

                typ = ev.get("type")
                if typ == "gameFull":
                    moves_text = (ev.get("state", {}) or {}).get("moves", "") or ""
                    moves = moves_text.split() if moves_text else []
                    last_processed = len(moves)
                    publish_event(game_id, "status", {"text": "Partie démarrée. J écoute les coups…"})
                elif typ == "gameState":
                    status = ev.get("status")
                    if status and status != "started":
                        publish_event(game_id, "status", {"text": f"Partie terminée ({status})."})
                        break
                    moves_text = ev.get("moves", "") or ""
                    moves = moves_text.split() if moves_text else []
                    if len(moves) <= last_processed:
                        continue
                    new_moves = moves[last_processed:]
                    for mv in new_moves:
                        enqueue_commentary(game_id, mv)
                    last_processed = len(moves)
    except Exception as exc:
        publish_event(game_id, "status", {"text": f"Stream lichess interrompu: {exc}"})

def enqueue_commentary(game_id: str, move_uci: str) -> None:
    global COMMENTARY_WORKER_ACTIVE
    with COMMENTARY_LOCK:
        COMMENTARY_QUEUE.append((game_id, move_uci))
        if COMMENTARY_WORKER_ACTIVE:
            return
        COMMENTARY_WORKER_ACTIVE = True
    t = threading.Thread(target=commentary_worker, daemon=True)
    t.start()

def commentary_worker() -> None:
    global COMMENTARY_WORKER_ACTIVE
    while True:
        with COMMENTARY_LOCK:
            if not COMMENTARY_QUEUE:
                COMMENTARY_WORKER_ACTIVE = False
                return
            game_id, move_uci = COMMENTARY_QUEUE.popleft()

        # 1) Generate text
        text = generate_commentary(move_uci)
        publish_event(game_id, "commentary", {"move": move_uci, "text": text})

        # 2) Generate WAV via Gradium and send as SSE "tts-wav" (base64)
        try:
            import asyncio
            wav_bytes = asyncio.run(gradium_tts_wav(text=text, voice_id=DEFAULT_VOICE_COACH_ID))
            import base64
            b64 = base64.b64encode(wav_bytes).decode("ascii")
            publish_event(game_id, "tts-wav", {"audio_b64": b64, "mime": "audio/wav"})
        except Exception as exc:
            publish_event(game_id, "tts-error", {"error": str(exc)})

        # small gap to avoid spamming TTS
        time.sleep(0.2)

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Chess Coach PoC</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    button { padding: 10px 14px; font-size: 16px; cursor: pointer; }
    .layout { display: grid; grid-template-columns: 420px 1fr; gap: 20px; align-items: start; margin-top: 16px; }
    .board iframe { width: 420px; height: 520px; border: 0; border-radius: 10px; background: #f3f4f6; }
    .panel { background: #f3f4f6; border-radius: 10px; padding: 14px; }
    #coachText { font-size: 18px; line-height: 1.3; }
    #status { margin-top: 8px; color: #444; }
    #log { margin-top: 12px; white-space: pre-wrap; font-family: ui-monospace, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
  </style>
</head>
<body>
  <h1>Chess Coach PoC</h1>
  <p>Deux humains jouent sur Lichess. Le coach affiche le texte (Mistral) et le lit (Gradium).</p>

  <button id="start">Démarrer une partie</button>
  <div id="status"></div>

  <div class="layout" style="display:none" id="layout">
    <div class="board">
      <iframe id="lichessFrame" src=""></iframe>
    </div>

    <div class="panel">
      <h3>Coach vocal</h3>
      <div id="coachText">En attente d’un coup…</div>
      <div id="log"></div>
    </div>
  </div>

  <script>
    const startBtn = document.getElementById("start");
    const statusEl = document.getElementById("status");
    const layoutEl = document.getElementById("layout");
    const frameEl = document.getElementById("lichessFrame");
    const coachTextEl = document.getElementById("coachText");
    const logEl = document.getElementById("log");

    let gameId = null;
    let audioCtx = null;

    function ensureAudio() {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") audioCtx.resume().catch(()=>{});
    }

    function log(line) {
      const t = new Date().toLocaleTimeString();
      logEl.textContent = `[${t}] ${line}\\n` + logEl.textContent;
    }

    function b64ToUint8Array(b64) {
      const bin = atob(b64);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }

    async function playWavFromB64(b64) {
      ensureAudio();
      const bytes = b64ToUint8Array(b64);
      // decodeAudioData expects an ArrayBuffer that contains a complete audio file (WAV ok)
      const buf = bytes.buffer;
      const audioBuffer = await audioCtx.decodeAudioData(buf.slice(0));
      const src = audioCtx.createBufferSource();
      src.buffer = audioBuffer;
      src.connect(audioCtx.destination);
      src.start();
    }

    startBtn.addEventListener("click", async () => {
      ensureAudio();
      startBtn.disabled = true;
      statusEl.textContent = "Création de la partie…";

      try {
        const res = await fetch("/start");
        if (!res.ok) {
          const t = await res.text().catch(()=> "");
          alert("Erreur start: " + (t || res.status));
          startBtn.disabled = false;
          statusEl.textContent = "";
          return;
        }
        const data = await res.json();
        gameId = data.game_id;

        layoutEl.style.display = "grid";
        frameEl.src = data.embed_url;
        statusEl.textContent = "Partie créée. Partage le lien Lichess à ton ami pour qu'il rejoigne: " + data.game_url;
        log("Game: " + data.game_id);

        const es = new EventSource("/events/" + gameId);

        es.addEventListener("status", (e) => {
          try {
            const p = JSON.parse(e.data);
            if (p.text) statusEl.textContent = p.text;
            log("STATUS: " + (p.text || ""));
          } catch {}
        });

        es.addEventListener("commentary", (e) => {
          try {
            const p = JSON.parse(e.data);
            if (p.text) coachTextEl.textContent = p.text;
            log("TEXT: " + (p.text || ""));
          } catch {}
        });

        es.addEventListener("tts-wav", async (e) => {
          try {
            const p = JSON.parse(e.data);
            if (!p.audio_b64) return;
            await playWavFromB64(p.audio_b64);
            log("AUDIO: played");
          } catch (err) {
            console.error(err);
            log("AUDIO: decode error");
          }
        });

        es.addEventListener("tts-error", (e) => {
          try {
            const p = JSON.parse(e.data);
            alert("Erreur TTS: " + (p.error || "unknown"));
          } catch {
            alert("Erreur TTS");
          }
        });

      } finally {
        startBtn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/start")
def start_game(request: Request):
    # Create open challenge game for 2 humans
    data = create_lichess_human_game()

    game_id = data["game_id"]
    with GAME_CONTEXTS_LOCK:
        GAME_CONTEXTS[game_id] = {
            "game_id": game_id,
            "voice_id": DEFAULT_VOICE_COACH_ID,
        }

    # Start stream thread
    t = threading.Thread(target=stream_game_thread, args=(game_id,), daemon=True)
    t.start()

    # Notify client
    publish_event(game_id, "status", {"text": "Partie créée. En attente que quelqu un rejoigne et que les coups commencent."})
    return data

@app.get("/events/{game_id}")
async def events(game_id: str):
    q = get_event_queue(game_id)

    async def gen():
        try:
            while True:
                msg = await q.get()
                event = msg.get("event", "message")
                payload = msg.get("payload", {})
                yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            raise
        finally:
            release_event_queue(game_id)

    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/debug/ping-tts", response_class=HTMLResponse)
def debug_ping_tts():
    return "<html><body>OK</body></html>"
