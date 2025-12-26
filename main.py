"""
Gradium Chess - minimal PoC backend

Objective (PoC):
- Allow a user to open a URL and start a Lichess human vs AI game (level default 3)
- Stream moves from Lichess, generate short one-line commentary per move,
  and stream TTS audio in near real-time to the browser (SSE events).
- Prioritize player move commentary (player -> AI order).
- No user accounts. Shareable by URL.

Notes:
- Secrets (LICHESS_TOKEN, GRADIUM_API_KEY, MISTRAL_API_KEY) must remain on the backend.
- The backend runs a small FastAPI app that serves a demo page and SSE endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from typing import Dict

import requests
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gradium-chess")

app = FastAPI(title="Gradium Chess PoC")

# -------------------------
# Global runtime state
# -------------------------
# Simple FIFO queue of (game_id, move_uci, role)
COMMENTARY_QUEUE: deque[tuple[str, str, str]] = deque()
COMMENTARY_LOCK = threading.Lock()
COMMENTARY_WORKER_ACTIVE = False

# Event subscribers: game_id -> {"queue": asyncio.Queue, "loop": loop, "subscribers": int}
EVENT_SUBSCRIBERS: Dict[str, dict] = {}
EVENT_SUBSCRIBERS_LOCK = threading.Lock()

# Game contexts (per game)
GAME_CONTEXTS: Dict[str, dict] = {}
GAME_CONTEXTS_LOCK = threading.Lock()

# Which event loop the FastAPI app uses (set on startup)
APP_LOOP: asyncio.AbstractEventLoop | None = None

# Minimal defaults
DEFAULT_VOICE_COACH_ID = "b35yykvVppLXyw_l"  # Elise (FR) in docs
DEFAULT_VOICE_AI_ID = "axlOaUiFyOZhy4nv"  # Leo (FR) in docs

# Throttle between LLM calls (seconds)
LLM_THROTTLE_SECONDS = 0.9
_LAST_LLM_CALL_AT: float | None = None

# TTS manager placeholder (lazy init)
TTS_MANAGER: "GradiumTTSManager" | None = None
TTS_MANAGER_LOCK = threading.Lock()


# -------------------------
# Utilities: Events / SSE
# -------------------------
def get_event_queue(game_id: str) -> asyncio.Queue:
    """
    Return (and create if missing) an asyncio.Queue attached to the current loop
    for the given game_id. Multiple clients on the same loop will share the queue.
    """
    loop = asyncio.get_running_loop()
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if entry and entry["loop"] is loop:
            entry["subscribers"] += 1
            return entry["queue"]
        queue: asyncio.Queue = asyncio.Queue()
        EVENT_SUBSCRIBERS[game_id] = {"queue": queue, "loop": loop, "subscribers": 1}
        return queue


def release_event_queue(game_id: str) -> None:
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if not entry:
            return
        entry["subscribers"] -= 1
        if entry["subscribers"] <= 0:
            EVENT_SUBSCRIBERS.pop(game_id, None)


def publish_event(game_id: str, event: str, payload: dict) -> None:
    """
    Publish an event to the SSE queue for a given game_id.
    This function can be called from background threads (it schedules put on the loop).
    """
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if not entry:
            return
        queue = entry["queue"]
        loop = entry["loop"]

    message = {"event": event, "payload": payload}
    try:
        asyncio.run_coroutine_threadsafe(queue.put(message), loop)
    except RuntimeError as exc:
        logger.warning("Failed to publish event for game %s: %s", game_id, exc)


# -------------------------
# Gradium TTS manager
# -------------------------
class GradiumTTSManager:
    """
    Minimal manager to send text to Gradium TTS websocket and stream audio back
    as SSE chunks.

    Behavior:
    - Maintains a queue of TTS jobs.
    - Ensures small throttle between remote requests to avoid flooding.
    - Publishes SSE events:
      - tts-start: {"utterance_id", "sample_rate", ...}
      - tts-audio: {"utterance_id", "audio"}  (base64 PCM chunks)
      - tts-end: {"utterance_id"}
    """

    def __init__(self, api_key: str, region: str = "eu"):
        self.api_key = api_key
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._last_sent = 0.0
        self._sse_chunk_size = 4096  # bytes, even for Int16 alignment
        self._sample_rate = 24000
        self._output_format = "pcm_24000"
        self._region = region

    async def speak(self, game_id: str, role: str, text: str, voice_id: str) -> None:
        """
        Schedule a TTS job and wait for completion.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._queue.put(
            {"game_id": game_id, "role": role, "text": text, "voice_id": voice_id, "done": fut}
        )
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
        await fut

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            fut = job["done"]
            try:
                # throttle small gaps
                elapsed = time.monotonic() - self._last_sent
                if elapsed < 0.5:
                    await asyncio.sleep(0.5 - elapsed)
                await self._perform_tts(job["game_id"], job["role"], job["text"], job["voice_id"])
                self._last_sent = time.monotonic()
                if not fut.done():
                    fut.set_result(None)
            except Exception as exc:
                logger.exception("TTS job failed: %s", exc)
                if not fut.done():
                    fut.set_exception(exc)

    async def _ensure_ws(self) -> websockets.WebSocketClientProtocol:
        if self._ws and not self._ws.closed:
            return self._ws
        url = f"wss://{self._region}.api.gradium.ai/api/speech/tts"
        headers = [("x-api-key", self.api_key)]
        try:
            self._ws = await websockets.connect(url, additional_headers=headers, max_size=None)
        except TypeError:
            # older websockets takes extra_headers
            self._ws = await websockets.connect(url, extra_headers=headers, max_size=None)
        return self._ws

    async def _perform_tts(self, game_id: str, role: str, text: str, voice_id: str) -> None:
        utterance_id = uuid.uuid4().hex
        ws = None
        try:
            ws = await self._ensure_ws()
            # send setup
            setup_payload = {
                "type": "setup",
                "model_name": "default",
                "voice_id": voice_id,
                "output_format": self._output_format,
            }
            await ws.send(json.dumps(setup_payload))
            # try to read an initial ready/error (non-blocking with timeout)
            try:
                ready_msg = await asyncio.wait_for(ws.recv(), timeout=3)
                # parse optional error/ready messages; ignore otherwise
                try:
                    data = json.loads(ready_msg)
                    # if server complains about format, fallback to pcm 48k
                    if isinstance(data, dict) and data.get("type") == "error":
                        msg = str(data.get("message", ""))
                        if "pcm_24000" in msg or "output_format" in msg:
                            # fallback
                            self._output_format = "pcm"
                            self._sample_rate = 48000
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "setup",
                                        "model_name": "default",
                                        "voice_id": voice_id,
                                        "output_format": "pcm",
                                        "sample_rate": 48000,
                                    }
                                )
                            )
                except Exception:
                    pass
            except asyncio.TimeoutError:
                # continue anyway
                pass

            publish_event(
                game_id,
                "tts-start",
                {
                    "role": role,
                    "text": text,
                    "utterance_id": utterance_id,
                    "sample_rate": self._sample_rate,
                    "channels": 1,
                },
            )
            # send text message
            await ws.send(json.dumps({"type": "text", "text": text}))

            # receive audio frames (binary) or JSON chunks
            seq = 0
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    raw = bytes(message)
                    # split into SSE-friendly base64 pieces
                    offset = 0
                    chunk_size = self._sse_chunk_size
                    if chunk_size % 2 != 0:
                        chunk_size -= 1
                    total_len = len(raw)
                    while offset < total_len:
                        piece = raw[offset : offset + chunk_size]
                        offset += len(piece)
                        seq += 1
                        encoded = base64.b64encode(piece).decode("ascii")
                        publish_event(
                            game_id,
                            "tts-audio",
                            {"role": role, "utterance_id": utterance_id, "sequence": seq, "audio": encoded},
                        )
                    continue

                # try JSON
                try:
                    data = json.loads(message)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    t = data.get("type")
                    if t == "audio" and data.get("audio"):
                        seq += 1
                        encoded = data.get("audio")
                        publish_event(
                            game_id,
                            "tts-audio",
                            {"role": role, "utterance_id": utterance_id, "sequence": seq, "audio": encoded},
                        )
                    elif t == "error":
                        raise RuntimeError(data.get("message") or "TTS error")
                    elif t == "end_of_stream":
                        break
                # else ignore
        except websockets.exceptions.ConnectionClosedOK:
            # closed normally
            pass
        except Exception as exc:
            logger.warning("Gradium TTS error: %s", exc)
            # mark connection as unusable and drop it
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None
        finally:
            publish_event(game_id, "tts-end", {"role": role, "utterance_id": utterance_id})


def ensure_tts_manager() -> GradiumTTSManager | None:
    gradium_key = os.getenv("GRADIUM_API_KEY")
    if not gradium_key:
        return None
    global TTS_MANAGER
    with TTS_MANAGER_LOCK:
        if TTS_MANAGER:
            return TTS_MANAGER
        TTS_MANAGER = GradiumTTSManager(api_key=gradium_key, region=os.getenv("GRADIUM_REGION", "eu"))
        logger.info("TTS manager created")
        return TTS_MANAGER


# -------------------------
# Commentary generation
# -------------------------
def throttle_llm_calls() -> None:
    global _LAST_LLM_CALL_AT
    now = time.monotonic()
    if _LAST_LLM_CALL_AT is None:
        _LAST_LLM_CALL_AT = now
        return
    elapsed = now - _LAST_LLM_CALL_AT
    if elapsed >= LLM_THROTTLE_SECONDS:
        _LAST_LLM_CALL_AT = now
        return
    sleep = LLM_THROTTLE_SECONDS - elapsed
    _LAST_LLM_CALL_AT = now + sleep
    time.sleep(sleep)


def generate_commentary_local(move_uci: str, role: str) -> str:
    """
    Very small deterministic fallback commentary when no LLM key is available.
    Produces one short sentence.
    """
    # basic heuristics for demonstrative comments
    templates_player = [
        "Bien joué, tu avances une pièce vers le centre.",
        "Pas mal, tu développes tes pièces.",
        "Attention à la sécurité de ton roi.",
        "Tu gagnes de l'espace, continue.",
        "Coup simple et solide.",
    ]
    templates_ai = [
        "Je réponds en consolidant le centre.",
        "Je t'attaque la case faible.",
        "Je développe et mets la pression.",
        "Je crée des menaces sur ton roi.",
        "Je simplifie la position en échangeant.",
    ]
    import hashlib

    h = int(hashlib.sha1(move_uci.encode()).hexdigest()[:8], 16)
    if role == "PLAYER_MOVE":
        return templates_player[h % len(templates_player)]
    return templates_ai[h % len(templates_ai)]


def generate_commentary_mistral(move_uci: str, role: str) -> str | None:
    """
    If MISTRAL_API_KEY is set, call Mistral chat completions to produce a 1-2 sentence
    commentary in French (or a short English fallback). Otherwise use a local heuristic.
    """
    mistral_key = os.getenv("MISTRAL_API_KEY")
    if not mistral_key:
        return generate_commentary_local(move_uci, role)

    # Throttle LLM calls across threads
    throttle_llm_calls()

    system_prompt = """Tu es un coach d'echecs vocal, familier et encourageant. Réponds par 1 phrase (2 max), lisible à voix haute, sans emojis ni guillemets, pas de caractères non-ASCII."""

    payload = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Rôle: {role}. Coup joué (UCI): {move_uci}. Commente ce coup simplement (1 phrase).",
            },
        ],
    }

    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {mistral_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if text:
            # sanitize: keep ASCII-ish text
            return text.replace("“", '"').replace("”", '"')
    except Exception as exc:
        logger.warning("Mistral commentary failed: %s", exc)
    return generate_commentary_local(move_uci, role)


# -------------------------
# Commentary queue processing
# -------------------------
def enqueue_commentary(game_id: str, move_uci: str, role: str) -> None:
    """
    Enqueue a move for commentary. Maintains logic so player move commentary is prioritized
    before AI commentary for the same half-turn.
    """
    global COMMENTARY_WORKER_ACTIVE

    with COMMENTARY_LOCK:
        if role == "PLAYER_MOVE":
            # append player move; if there is a pending ai_move for same turn, schedule it after player's comment
            COMMENTARY_QUEUE.append((game_id, move_uci, "PLAYER_MOVE"))
        else:
            # AI move: if player commentary already queued but not yet processed, ensure AI comes after
            COMMENTARY_QUEUE.append((game_id, move_uci, "AI_MOVE"))

        if COMMENTARY_WORKER_ACTIVE or not COMMENTARY_QUEUE:
            return
        COMMENTARY_WORKER_ACTIVE = True

    worker = threading.Thread(target=process_commentary_queue, daemon=True)
    worker.start()


def process_commentary_queue() -> None:
    """
    Worker thread that consumes COMMENTARY_QUEUE and:
    - generates a short commentary (mistral or local)
    - publishes "commentary" SSE event with role/text
    - sends to TTS manager to speak and publish tts events (non-blocking)
    """
    global COMMENTARY_WORKER_ACTIVE

    while True:
        with COMMENTARY_LOCK:
            if not COMMENTARY_QUEUE:
                COMMENTARY_WORKER_ACTIVE = False
                return
            game_id, move_uci, role = COMMENTARY_QUEUE.popleft()

        # generate commentary
        commentary = generate_commentary_mistral(move_uci, role)
        if not commentary:
            continue

        logger.info('COMMENTARY game=%s role=%s text="%s"', game_id, role, commentary)
        # publish simple commentary event (for text log)
        publish_event(game_id, "commentary", {"role": role, "text": commentary, "move": move_uci})

        manager = ensure_tts_manager()
        if manager and APP_LOOP:
            # choose voice for role
            with GAME_CONTEXTS_LOCK:
                gc = GAME_CONTEXTS.get(game_id, {})
            voice_id = gc.get("voice_coach_id" if role == "PLAYER_MOVE" else "voice_ai_id", DEFAULT_VOICE_COACH_ID)
            # schedule TTS speak on asyncio loop
            fut = asyncio.run_coroutine_threadsafe(manager.speak(game_id, role, commentary, voice_id), APP_LOOP)

            # attach callback to log TTS completion or failure
            def _done(f):
                try:
                    f.result()
                except Exception:
                    logger.exception("TTS failed for game %s role %s", game_id, role)

            fut.add_done_callback(_done)


# -------------------------
# Lichess streaming
# -------------------------
def stream_game_state_thread(game_id: str) -> None:
    """
    Blocking thread target — connects to Lichess stream and enqueues commentary
    for moves as they arrive. Runs in a separate daemon thread.
    """
    lichess_token = os.getenv("LICHESS_TOKEN")
    if not lichess_token:
        logger.error("LICHESS_TOKEN not set; cannot stream game %s", game_id)
        return

    stream_url = f"https://lichess.org/api/board/game/stream/{game_id}"
    headers = {"Authorization": f"Bearer {lichess_token}", "Accept": "application/json"}
    human_color = None
    last_processed = 0

    try:
        with requests.get(stream_url, headers=headers, stream=True, timeout=90) as resp:
            resp.raise_for_status()
            logger.info("Connected to lichess stream for game %s", game_id)
            for line in resp.iter_lines(chunk_size=65536):
                if not line:
                    continue
                try:
                    ev = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                typ = ev.get("type")
                if typ == "gameFull":
                    white = ev.get("white", {})
                    black = ev.get("black", {})
                    if "aiLevel" in white:
                        human_color = "black"
                    elif "aiLevel" in black:
                        human_color = "white"
                    else:
                        human_color = None
                    moves = ev.get("state", {}).get("moves", "")
                    last_processed = len(moves.split()) if moves else 0
                    logger.info("gameFull received game=%s human_color=%s", game_id, human_color)
                elif typ == "gameState":
                    status = ev.get("status")
                    if status and status != "started":
                        logger.info("Game %s finished status=%s", game_id, status)
                        break
                    moves_text = ev.get("moves", "")
                    moves = moves_text.split() if moves_text else []
                    if len(moves) <= last_processed:
                        continue
                    new_moves = moves[last_processed:]
                    for idx, mv in enumerate(new_moves, start=last_processed):
                        mover_color = "white" if idx % 2 == 0 else "black"
                        role = "PLAYER_MOVE" if mover_color == human_color else "AI_MOVE"
                        logger.info("game=%s new move %s role=%s", game_id, mv, role)
                        enqueue_commentary(game_id, mv, role)
                    last_processed = len(moves)
    except Exception as exc:
        logger.warning("Lichess streaming for game %s failed: %s", game_id, exc)
    finally:
        # cleanup game context (free voices)
        with GAME_CONTEXTS_LOCK:
            GAME_CONTEXTS.pop(game_id, None)
        logger.info("Stopped streaming game %s", game_id)


# -------------------------
# HTTP endpoints
# -------------------------
@app.on_event("startup")
async def _startup():
    global APP_LOOP
    APP_LOOP = asyncio.get_running_loop()
    logger.info("App loop set")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def demo_html():
    """
    Very small demo page that uses SSE to receive events (commentary + tts chunks)
    and plays PCM chunks in the browser via AudioContext.
    For brevity the page is kept minimal; the front (GitHub Pages) can host a nicer UI.
    """
    html = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<title>Gradium Chess Demo</title>
<style>body{{font-family:Arial,Helvetica,sans-serif;margin:24px}}button{{padding:12px 18px;font-size:16px}}</style>
</head>
<body>
<h1>Gradium Chess — Demo</h1>
<p><button id="start">Démarrer la démo</button> <button id="tts-test">Tester la voix</button></p>
<div id="log" style="white-space:pre-wrap;font-family:monospace;background:#f3f4f6;padding:12px;border-radius:8px;max-height:200px;overflow:auto"></div>
<script>
let gameId = null;
let audioCtx, nextTime=0, queue=Promise.resolve(), currentUtterance=null, sr=24000;
function log(s){document.getElementById('log').textContent = new Date().toLocaleTimeString() + ' ' + s + '\\n' + document.getElementById('log').textContent}
function ensureAudio(){ if(!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)(); if(audioCtx.state==='suspended') audioCtx.resume().catch(()=>{}); }
function base64ToInt16(base64){
  const binary = atob(base64);
  const len = binary.length;
  const buf = new ArrayBuffer(len);
  const view = new Uint8Array(buf);
  for(let i=0;i<len;i++) view[i]=binary.charCodeAt(i);
  return new Int16Array(buf);
}
function pcmToAudioBuffer(pcm16, sampleRate){
  ensureAudio();
  const audioBuffer = audioCtx.createBuffer(1, pcm16.length, sampleRate);
  const ch = audioBuffer.getChannelData(0);
  for(let i=0;i<pcm16.length;i++) ch[i] = pcm16[i]/32768;
  return audioBuffer;
}
function schedule(buffer){
  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  if(!Number.isFinite(nextTime) || nextTime < now) nextTime = now + 0.05;
  src.start(nextTime);
  nextTime += buffer.duration;
  return new Promise(r=>src.onended=r);
}
function playChunk(base64, sampleRate){
  const pcm = base64ToInt16(base64);
  const audioBuffer = pcmToAudioBuffer(pcm, sampleRate);
  queue = queue.then(()=>schedule(audioBuffer));
  return queue;
}
document.getElementById('start').addEventListener('click', async ()=>{
  const r = await fetch('/start-game-demo');
  const j = await r.json();
  gameId = j.game_id;
  if(j.game_url) window.open(j.game_url,'_blank');
  log('Game created: ' + gameId);
  const es = new EventSource('/events/' + gameId);
  es.addEventListener('commentary', e=>{
    try{ const p = JSON.parse(e.data); log(p.role + ': ' + p.text); }catch{}
  });
  es.addEventListener('tts-start', e=>{
    try{ const p = JSON.parse(e.data); currentUtterance = p.utterance_id; if(p.sample_rate) sr = p.sample_rate; ensureAudio(); nextTime = audioCtx.currentTime + 0.05; }catch{}
  });
  es.addEventListener('tts-audio', e=>{
    try{
      const p = JSON.parse(e.data);
      if(!p.audio) return;
      if(p.utterance_id !== currentUtterance) return;
      playChunk(p.audio, sr).catch(err=>console.error(err));
    }catch(err){}
  });
  es.addEventListener('tts-end', e=>{
    try{ const p = JSON.parse(e.data); if(p.utterance_id===currentUtterance) currentUtterance=null; }catch{}
  });
});
document.getElementById('tts-test').addEventListener('click', async ()=>{
  if(!gameId){ alert('Start a game first'); return;}
  await fetch('/debug/tts-inject', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({game_id:gameId})});
});
</script>
</body>
</html>"""
    return Response(content=html, media_type="text/html")


@app.get("/start-game-demo")
def start_game_demo(request: Request):
    """
    Creates a lichess human vs AI game and starts the background stream thread.
    Returns JSON {game_id, game_url}
    """
    lichess_token = os.getenv("LICHESS_TOKEN")
    if not lichess_token:
        raise HTTPException(status_code=500, detail="LICHESS_TOKEN not set")

    ai_level = 3
    try:
        ai_level_env = os.getenv("AI_LEVEL")
        if ai_level_env:
            ai_level = int(ai_level_env)
    except Exception:
        ai_level = 3

    # voices can be overridden by query params for testing
    voice_coach = request.query_params.get("voice_coach", DEFAULT_VOICE_COACH_ID)
    voice_ai = request.query_params.get("voice_ai", DEFAULT_VOICE_AI_ID)

    payload = urllib.parse.urlencode({"level": ai_level, "clock.limit": 600, "clock.increment": 0, "rated": "false"}).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        "https://lichess.org/api/challenge/ai",
        data=payload,
        headers={
            "Authorization": f"Bearer {lichess_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.exception("Lichess create game failed: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to create lichess game")

    game_id = data.get("id") or data.get("game", {}).get("id") or data.get("challenge", {}).get("id")
    if not game_id:
        raise HTTPException(status_code=502, detail="Lichess response missing game id")

    game_url = f"https://lichess.org/{game_id}"
    with GAME_CONTEXTS_LOCK:
        GAME_CONTEXTS[game_id] = {"game_id": game_id, "voice_coach_id": voice_coach, "voice_ai_id": voice_ai}

    # start blocking stream in a daemon thread (so FastAPI thread isn't blocked)
    t = threading.Thread(target=stream_game_state_thread, args=(game_id,), daemon=True)
    t.start()

    return {"game_id": game_id, "game_url": game_url}


@app.get("/events/{game_id}")
async def events(game_id: str):
    """
    SSE endpoint for clients to receive commentary and TTS audio chunks.
    """
    queue = get_event_queue(game_id)

    async def event_stream():
        try:
            while True:
                message = await queue.get()
                event = message.get("event", "commentary")
                payload = message.get("payload", {})
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            # client disconnected
            raise
        finally:
            release_event_queue(game_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/debug/tts-inject")
async def debug_tts_inject(request: Request):
    """
    Trigger a debug TTS message into the game stream to test playback.
    """
    body = await request.json()
    game_id = body.get("game_id")
    if not game_id:
        raise HTTPException(status_code=400, detail="game_id missing")

    manager = ensure_tts_manager()
    if not manager:
        raise HTTPException(status_code=500, detail="TTS manager not available (set GRADIUM_API_KEY)")

    if not APP_LOOP:
        raise HTTPException(status_code=500, detail="App loop not ready")

    text = "Salut, je suis le coach. Ceci est un test audio."
    with GAME_CONTEXTS_LOCK:
        ctx = GAME_CONTEXTS.get(game_id, {})
    voice = ctx.get("voice_coach_id", DEFAULT_VOICE_COACH_ID)

    asyncio.run_coroutine_threadsafe(manager.speak(game_id, "PLAYER_MOVE", text, voice), APP_LOOP)
    return {"status": "ok"}


# -------------------------
# End of file
# -------------------------
