import asyncio
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

import requests
import websockets
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from google import genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

COMMENTARY_QUEUE: deque[tuple[str, str, str]] = deque()
COMMENTARY_LOCK = threading.Lock()
COMMENTARY_WORKER_ACTIVE = False
LAST_LLM_CALL_AT: float | None = None
LAST_PROCESSED_MOVE_COUNT = 0
COMMENTARY_SUBSCRIBERS: dict[str, dict] = {}
COMMENTARY_SUBSCRIBERS_LOCK = threading.Lock()
CURRENT_TURN = {
    "player_move": None,
    "ai_move": None,
    "player_commented": False,
    "ai_commented": False,
}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def demo_root():
    html = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <title>Gradium Chess Demo</title>
    <style>
      body {
        font-family: Arial, sans-serif;
        margin: 32px;
      }
      button {
        font-size: 16px;
        padding: 12px 18px;
      }
    </style>
  </head>
  <body>
    <button id="start-demo">Démarrer la démo</button>
    <script>
      const button = document.getElementById("start-demo");
      let socket;
      let ttsReady = false;
      let audioContext;
      let nextAudioTime = 0;
      const ttsSampleRate = 16000;
      let firstAudioLogged = false;

      function getSocketUrl() {
        const scheme = window.location.protocol === "https:" ? "wss" : "ws";
        return `${scheme}://${window.location.host}/ws/tts`;
      }

      function ensureAudioContext() {
        if (!audioContext) {
          audioContext = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: ttsSampleRate,
          });
        }
      }

      function schedulePcmPlayback(int16Samples) {
        ensureAudioContext();
        const samples = new Float32Array(int16Samples.length);
        for (let i = 0; i < int16Samples.length; i += 1) {
          samples[i] = int16Samples[i] / 32768;
        }
        const buffer = audioContext.createBuffer(1, samples.length, ttsSampleRate);
        buffer.getChannelData(0).set(samples);
        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(audioContext.destination);
        const startAt = Math.max(audioContext.currentTime, nextAudioTime);
        source.start(startAt);
        nextAudioTime = startAt + buffer.duration;
      }

      function handleSocketMessage(event) {
        if (typeof event.data === "string") {
          let message;
          try {
            message = JSON.parse(event.data);
          } catch (error) {
            return;
          }
          if (message.type === "ready") {
            ttsReady = true;
            return;
          }
          if (message.type === "audio" && message.audio) {
            if (!firstAudioLogged) {
              console.log("First audio message received:", message);
              firstAudioLogged = true;
            }
            const binary = atob(message.audio);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i += 1) {
              bytes[i] = binary.charCodeAt(i);
            }
            schedulePcmPlayback(new Int16Array(bytes.buffer));
            return;
          }
          return;
        }

        if (event.data instanceof ArrayBuffer) {
          schedulePcmPlayback(new Int16Array(event.data));
        } else if (event.data && event.data.arrayBuffer) {
          event.data.arrayBuffer().then((buffer) => {
            schedulePcmPlayback(new Int16Array(buffer));
          });
        }
      }

      function connectSocket() {
        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
          return;
        }
        socket = new WebSocket(getSocketUrl());
        socket.binaryType = "arraybuffer";
        socket.addEventListener("close", () => {
          ttsReady = false;
        });
        socket.addEventListener("message", handleSocketMessage);
      }

      function sendTts(text) {
        if (!ttsReady || !socket || socket.readyState !== WebSocket.OPEN) {
          return;
        }
        console.log("SEND TO TTS", text);
        const payload = { type: "text", text };
        socket.send(JSON.stringify(payload));
      }

      async function startDemo() {
        connectSocket();
        const response = await fetch("/start-game-demo");
        const data = await response.json();
        if (data.game_url) {
          window.open(data.game_url, "_blank", "noopener,noreferrer");
        }
        if (!data.game_id) {
          return;
        }
        const eventSource = new EventSource(`/events/${data.game_id}`);
        eventSource.addEventListener("commentary", (event) => {
          try {
            const payload = JSON.parse(event.data);
            if (payload && payload.text) {
              sendTts(payload.text);
            }
          } catch (error) {
            return;
          }
        });
      }

      button.addEventListener("click", async () => {
        ensureAudioContext();
        await audioContext.resume();
        startDemo();
      });
    </script>
  </body>
</html>
"""
    return Response(content=html, media_type="text/html")


@app.get("/env-check")
def env_check():
    ai_level_value = os.getenv("AI_LEVEL")
    try:
        ai_level = int(ai_level_value) if ai_level_value is not None else None
    except ValueError:
        ai_level = None

    return {
        "has_lichess_token": bool(os.getenv("LICHESS_TOKEN")),
        "has_gradium_key": bool(os.getenv("GRADIUM_API_KEY")),
        "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
        "ai_level": ai_level,
    }


def start_game_internal(background_tasks: BackgroundTasks) -> dict:
    lichess_token = os.getenv("LICHESS_TOKEN")
    if not lichess_token:
        raise HTTPException(status_code=500, detail="LICHESS_TOKEN not set")

    ai_level_value = os.getenv("AI_LEVEL", "3")
    try:
        ai_level = int(ai_level_value)
    except ValueError:
        ai_level = 3

    payload = urllib.parse.urlencode(
        {
            "level": ai_level,
            "clock.limit": 600,
            "clock.increment": 0,
            "rated": "false",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        "https://lichess.org/api/challenge/ai",
        data=payload,
        headers={
            "Authorization": f"Bearer {lichess_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request) as response:
        data = json.loads(response.read().decode("utf-8"))

    game_id = (
        data.get("id")
        or data.get("game", {}).get("id")
        or data.get("challenge", {}).get("id")
    )
    if not game_id:
        raise HTTPException(status_code=502, detail="Lichess response missing game id")

    game_url = f"https://lichess.org/{game_id}"

    background_tasks.add_task(stream_game_state, game_id)

    return {"game_id": game_id, "game_url": game_url}


@app.post("/start-game")
def start_game(background_tasks: BackgroundTasks):
    return start_game_internal(background_tasks)


@app.get("/start-game-demo")
def start_game_demo(background_tasks: BackgroundTasks):
    return start_game_internal(background_tasks)


@app.get("/start-game-test")
def start_game_test(background_tasks: BackgroundTasks):
    return start_game_internal(background_tasks)


def stream_game_state(game_id: str) -> None:
    global LAST_PROCESSED_MOVE_COUNT

    lichess_token = os.getenv("LICHESS_TOKEN")
    if not lichess_token:
        logger.error("LICHESS_TOKEN not set; cannot stream game")
        return

    stream_url = f"https://lichess.org/api/board/game/stream/{game_id}"
    headers = {
        "Authorization": f"Bearer {lichess_token}",
        "Accept": "application/json",
    }

    human_color = None
    LAST_PROCESSED_MOVE_COUNT = 0

    try:
        with requests.get(
            stream_url, headers=headers, stream=True, timeout=60
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    logger.warning("Failed to decode stream line: %s", line)
                    continue
                event_type = event.get("type")
                if event_type == "gameFull":
                    white_player = event.get("white", {})
                    black_player = event.get("black", {})
                    if "aiLevel" in white_player:
                        human_color = "black"
                    elif "aiLevel" in black_player:
                        human_color = "white"
                    else:
                        human_color = None
                    logger.info("gameFull human_color=%s", human_color)

                    initial_moves = event.get("state", {}).get("moves", "")
                    initial_moves_list = (
                        initial_moves.split() if initial_moves else []
                    )
                    LAST_PROCESSED_MOVE_COUNT = len(initial_moves_list)
                    continue

                if event_type == "gameState":
                    moves_text = event.get("moves", "")
                    moves = moves_text.split() if moves_text else []

                    new_moves = moves[LAST_PROCESSED_MOVE_COUNT:]
                    for index, move in enumerate(
                        new_moves, start=LAST_PROCESSED_MOVE_COUNT
                    ):
                        mover_color = "white" if index % 2 == 0 else "black"
                        if mover_color == human_color:
                            logger.info("PLAYER_MOVE move=%s", move)
                            enqueue_commentary(game_id, move, "PLAYER_MOVE")
                        else:
                            logger.info("AI_MOVE move=%s", move)
                            enqueue_commentary(game_id, move, "AI_MOVE")
                    LAST_PROCESSED_MOVE_COUNT = len(moves)
    except requests.RequestException as exc:
        logger.warning("Lichess stream request failed: %s", exc)


@app.get("/debug/stream/{game_id}")
def debug_stream(game_id: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(stream_game_state, game_id)
    return {"status": "streaming", "game_id": game_id}


@app.get("/debug/gemini-test")
def debug_gemini_test():
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        return {"error": "GEMINI_API_KEY not set"}

    try:
        client = genai.Client(api_key=gemini_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents="Dis bonjour en français en une seule phrase.",
        )
        text = (response.text or "").strip()
        if not text:
            return {"error": "Empty response from Gemini"}
        return {"text": text}
    except Exception as exc:
        logger.warning("Gemini test request failed: %s", exc)
        return {"error": str(exc)}


@app.get("/debug/mistral-test")
def debug_mistral_test():
    mistral_key = os.getenv("MISTRAL_API_KEY")
    if not mistral_key:
        return {"error": "MISTRAL_API_KEY not set"}

    payload = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": "Tu es un assistant poli."},
            {"role": "user", "content": "Dis bonjour en français en une seule phrase."},
        ],
    }

    try:
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {mistral_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Mistral test request failed: %s", exc)
        return {"error": str(exc)}

    data = response.json()
    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    )
    return {"text": content}


@app.get("/debug/gradium-test")
def debug_gradium_test():
    gradium_key = os.getenv("GRADIUM_API_KEY")
    if not gradium_key:
        return JSONResponse(
            status_code=500, content={"error": "GRADIUM_API_KEY not set"}
        )

    payload = {
        "text": "Salut ! Je suis ton coach d’échecs. Test audio.",
        "voice_id": "YTpq7expH9539ERJ",
        "format": "pcm",
    }

    try:
        response = requests.post(
            "https://api.gradium.ai/v1/tts/synthesize",
            headers={
                "Authorization": f"Bearer {gradium_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            # TODO: Temporary PoC workaround for Gradium's self-signed SSL cert.
            verify=False,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Gradium TTS request failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return Response(content=response.content, media_type="application/octet-stream")


def enqueue_commentary(game_id: str, move_uci: str, role: str) -> None:
    if role not in {"PLAYER_MOVE", "AI_MOVE"}:
        raise ValueError(f"Invalid role: {role}")

    global COMMENTARY_WORKER_ACTIVE

    with COMMENTARY_LOCK:
        if role == "PLAYER_MOVE":
            if CURRENT_TURN["player_move"]:
                _reset_current_turn()
            CURRENT_TURN["player_move"] = move_uci
            CURRENT_TURN["player_commented"] = True
            COMMENTARY_QUEUE.append((game_id, move_uci, role))
            if CURRENT_TURN["ai_move"]:
                COMMENTARY_QUEUE.append((game_id, CURRENT_TURN["ai_move"], "AI_MOVE"))
                CURRENT_TURN["ai_commented"] = True
                _reset_current_turn()
        else:
            if CURRENT_TURN["ai_move"]:
                _reset_current_turn()
            CURRENT_TURN["ai_move"] = move_uci
            if CURRENT_TURN["player_commented"]:
                COMMENTARY_QUEUE.append((game_id, move_uci, role))
                CURRENT_TURN["ai_commented"] = True
                _reset_current_turn()

        if COMMENTARY_WORKER_ACTIVE or not COMMENTARY_QUEUE:
            return
        COMMENTARY_WORKER_ACTIVE = True

    worker = threading.Thread(target=process_commentary_queue, daemon=True)
    worker.start()


def _reset_current_turn() -> None:
    CURRENT_TURN["player_move"] = None
    CURRENT_TURN["ai_move"] = None
    CURRENT_TURN["player_commented"] = False
    CURRENT_TURN["ai_commented"] = False


def process_commentary_queue() -> None:
    global COMMENTARY_WORKER_ACTIVE

    while True:
        with COMMENTARY_LOCK:
            if not COMMENTARY_QUEUE:
                COMMENTARY_WORKER_ACTIVE = False
                return
            game_id, move_uci, role = COMMENTARY_QUEUE.popleft()

        commentary = generate_commentary_mistral(move_uci, role)
        if commentary:
            logger.info(
                'COMMENTARY role=%s text="%s"',
                role,
                commentary.replace('"', "'"),
            )
            publish_commentary_event(game_id, role, commentary, move_uci)


def generate_commentary_mistral(move_uci: str, role: str) -> str | None:
    if role not in {"PLAYER_MOVE", "AI_MOVE"}:
        raise ValueError(f"Invalid role: {role}")

    mistral_key = os.getenv("MISTRAL_API_KEY")
    if not mistral_key:
        logger.warning("MISTRAL_API_KEY not set; skipping commentary")
        return None

    throttle_mistral_calls()

    payload = {
        "model": "mistral-small-latest",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu es un coach d’échecs calme et pédagogique. "
                    "Tu commentes le coup en maximum deux phrases."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Rôle: {role}. Coup joué (notation UCI): {move_uci}. "
                    "Commente ce coup simplement."
                ),
            },
        ],
    }

    try:
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {mistral_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Mistral commentary request failed: %s", exc)
        return None

    data = response.json()
    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    )
    return content or None


def throttle_mistral_calls() -> None:
    global LAST_LLM_CALL_AT

    with COMMENTARY_LOCK:
        now = time.monotonic()
        if LAST_LLM_CALL_AT is None:
            LAST_LLM_CALL_AT = now
            return
        elapsed = now - LAST_LLM_CALL_AT
        if elapsed >= 1.0:
            LAST_LLM_CALL_AT = now
            return
        sleep_for = 1.0 - elapsed
        LAST_LLM_CALL_AT = now + sleep_for

    time.sleep(sleep_for)


def get_commentary_queue(game_id: str) -> asyncio.Queue:
    loop = asyncio.get_running_loop()
    with COMMENTARY_SUBSCRIBERS_LOCK:
        entry = COMMENTARY_SUBSCRIBERS.get(game_id)
        if entry and entry["loop"] is loop:
            entry["subscribers"] += 1
            return entry["queue"]
        queue: asyncio.Queue = asyncio.Queue()
        COMMENTARY_SUBSCRIBERS[game_id] = {
            "queue": queue,
            "loop": loop,
            "subscribers": 1,
        }
        return queue


def release_commentary_queue(game_id: str) -> None:
    with COMMENTARY_SUBSCRIBERS_LOCK:
        entry = COMMENTARY_SUBSCRIBERS.get(game_id)
        if not entry:
            return
        entry["subscribers"] -= 1
        if entry["subscribers"] <= 0:
            COMMENTARY_SUBSCRIBERS.pop(game_id, None)


def publish_commentary_event(game_id: str, role: str, text: str, move: str) -> None:
    with COMMENTARY_SUBSCRIBERS_LOCK:
        entry = COMMENTARY_SUBSCRIBERS.get(game_id)
        if not entry:
            return
        queue = entry["queue"]
        loop = entry["loop"]

    payload = {"role": role, "text": text, "move": move}
    try:
        asyncio.run_coroutine_threadsafe(queue.put(payload), loop)
    except RuntimeError as exc:
        logger.warning("Failed to publish commentary event: %s", exc)


@app.get("/events/{game_id}")
async def events(game_id: str):
    queue = get_commentary_queue(game_id)

    async def event_stream():
        try:
            while True:
                payload = await queue.get()
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: commentary\ndata: {data}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            release_commentary_queue(game_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.websocket("/ws/tts")
async def websocket_tts_proxy(websocket: WebSocket):
    await websocket.accept()

    gradium_key = os.getenv("GRADIUM_API_KEY")
    if not gradium_key:
        await websocket.close(code=1011)
        return

    setup_payload = {
        "type": "setup",
        "model_name": "default",
        "voice_id": "YTpq7expH9539ERJ",
        "output_format": "wav",
    }

    try:
        async with websockets.connect(
            "wss://eu.api.gradium.ai/api/speech/tts",
            additional_headers={"x-api-key": gradium_key},
        ) as gradium_ws:
            await gradium_ws.send(json.dumps(setup_payload))

            ready_event = asyncio.Event()

            async def forward_client_to_gradium():
                try:
                    while True:
                        client_message = await websocket.receive_text()
                        try:
                            message = json.loads(client_message)
                        except json.JSONDecodeError:
                            continue

                        if message.get("type") != "text":
                            continue

                        await ready_event.wait()
                        logger.info("FORWARD TEXT")
                        await gradium_ws.send(client_message)
                except WebSocketDisconnect:
                    await gradium_ws.close()
                except websockets.exceptions.ConnectionClosed:
                    await websocket.close()

            async def forward_gradium_to_client():
                try:
                    while True:
                        gradium_message = await gradium_ws.recv()
                        if isinstance(gradium_message, bytes):
                            logger.info("FORWARD AUDIO")
                            await websocket.send_bytes(gradium_message)
                            continue

                        await websocket.send_text(gradium_message)
                        try:
                            message = json.loads(gradium_message)
                        except json.JSONDecodeError:
                            continue

                        message_type = message.get("type")
                        if message_type == "ready":
                            logger.info("GRADIUM READY")
                            ready_event.set()
                        elif message_type == "audio":
                            logger.info("FORWARD AUDIO")
                        elif message_type == "end_of_stream":
                            logger.info("END OF STREAM")
                except websockets.exceptions.ConnectionClosed:
                    pass
                except WebSocketDisconnect:
                    await gradium_ws.close()
                finally:
                    await websocket.close()

            tasks = [
                asyncio.create_task(forward_client_to_gradium()),
                asyncio.create_task(forward_gradium_to_client()),
            ]

            await asyncio.gather(*tasks)
    except websockets.exceptions.ConnectionClosed:
        await websocket.close()




@app.get("/demo")
def demo():
    html = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <title>Gradium Chess Demo</title>
    <style>
      body {
        font-family: "Helvetica Neue", Arial, sans-serif;
        margin: 32px;
        background: #0f172a;
        color: #e2e8f0;
      }
      h1 {
        margin-bottom: 8px;
      }
      .card {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 12px;
        padding: 16px 20px;
        margin-top: 16px;
      }
      .status {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        font-size: 12px;
        background: #1e293b;
        margin-left: 8px;
      }
      .status.connected {
        background: #16a34a;
        color: white;
      }
      .log {
        white-space: pre-wrap;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono",
          "Courier New", monospace;
        font-size: 12px;
        background: #0b1220;
        border-radius: 8px;
        padding: 12px;
        max-height: 200px;
        overflow: auto;
      }
    </style>
  </head>
  <body>
    <h1>Gradium Chess Demo</h1>
    <div class="card">
      <div>Game ID: <span id="game-id">—</span></div>
      <div>Game URL: <a id="game-url" href="#" target="_blank" rel="noreferrer">—</a></div>
      <div>
        SSE: <span id="sse-status" class="status">connecting…</span>
        TTS: <span id="tts-status" class="status">connecting…</span>
      </div>
    </div>

    <div class="card">
      <h3>Commentary log</h3>
      <div id="log" class="log"></div>
    </div>

    <script>
      const logEl = document.getElementById("log");
      const gameIdEl = document.getElementById("game-id");
      const gameUrlEl = document.getElementById("game-url");
      const sseStatusEl = document.getElementById("sse-status");
      const ttsStatusEl = document.getElementById("tts-status");

      let socket;
      let ttsReady = false;
      let speaking = false;
      let sendLoopActive = false;
      let lastSentAt = 0;

      let audioContext;
      let nextAudioTime = 0;

      const ttsQueue = [];
      let expectedRole = "PLAYER_MOVE";
      let bufferedPlayer = null;
      let bufferedAi = null;

      function log(message) {
        const line = `[${new Date().toLocaleTimeString()}] ${message}\\n`;
        logEl.textContent = line + logEl.textContent;
      }

      function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
      }

      function getSocketUrl() {
        const scheme = window.location.protocol === "https:" ? "wss" : "ws";
        return `${scheme}://${window.location.host}/ws/tts`;
      }

      function ensureAudioContext() {
        if (!audioContext) {
          audioContext = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: 48000,
          });
        }
      }

      function schedulePcmPlayback(int16Samples) {
        ensureAudioContext();
        const samples = new Float32Array(int16Samples.length);
        for (let i = 0; i < int16Samples.length; i += 1) {
          samples[i] = int16Samples[i] / 32768;
        }
        const buffer = audioContext.createBuffer(1, samples.length, 24000);
        buffer.getChannelData(0).set(samples);
        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(audioContext.destination);
        const startAt = Math.max(audioContext.currentTime, nextAudioTime);
        source.start(startAt);
        nextAudioTime = startAt + buffer.duration;
      }

      function handleSocketMessage(event) {
        if (typeof event.data === "string") {
          let message;
          try {
            message = JSON.parse(event.data);
          } catch (error) {
            return;
          }
          if (message.type === "ready") {
            ttsReady = true;
            ttsStatusEl.classList.add("connected");
            ttsStatusEl.textContent = "ready";
            pumpTtsQueue();
            return;
          }
          if (message.type === "audio" && message.audio) {
            const binary = atob(message.audio);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i += 1) {
              bytes[i] = binary.charCodeAt(i);
            }
            schedulePcmPlayback(new Int16Array(bytes.buffer));
            return;
          }
          if (message.type === "end_of_stream") {
            speaking = false;
            pumpTtsQueue();
          }
          return;
        }

        if (event.data instanceof ArrayBuffer) {
          schedulePcmPlayback(new Int16Array(event.data));
        } else if (event.data && event.data.arrayBuffer) {
          event.data.arrayBuffer().then((buffer) => {
            schedulePcmPlayback(new Int16Array(buffer));
          });
        }
      }

      function connectSocket() {
        socket = new WebSocket(getSocketUrl());
        socket.binaryType = "arraybuffer";
        socket.addEventListener("open", () => {
          ttsStatusEl.textContent = "connecting…";
        });
        socket.addEventListener("close", () => {
          ttsReady = false;
          ttsStatusEl.classList.remove("connected");
          ttsStatusEl.textContent = "disconnected";
        });
        socket.addEventListener("message", handleSocketMessage);
      }

      function enqueueTts(text, role) {
        ttsQueue.push({ text, role });
        log(`${role}: ${text}`);
        pumpTtsQueue();
      }

      function flushBuffered() {
        let flushed = true;
        while (flushed) {
          flushed = false;
          if (expectedRole === "PLAYER_MOVE" && bufferedPlayer) {
            const payload = bufferedPlayer;
            bufferedPlayer = null;
            enqueueTts(payload.text, payload.role);
            expectedRole = "AI_MOVE";
            flushed = true;
          } else if (expectedRole === "AI_MOVE" && bufferedAi) {
            const payload = bufferedAi;
            bufferedAi = null;
            enqueueTts(payload.text, payload.role);
            expectedRole = "PLAYER_MOVE";
            flushed = true;
          }
        }
      }

      function handleCommentary(payload) {
        if (!payload || !payload.text || !payload.role) {
          return;
        }
        if (payload.role === expectedRole) {
          enqueueTts(payload.text, payload.role);
          expectedRole = expectedRole === "PLAYER_MOVE" ? "AI_MOVE" : "PLAYER_MOVE";
          flushBuffered();
          return;
        }
        if (payload.role === "PLAYER_MOVE") {
          bufferedPlayer = payload;
        } else if (payload.role === "AI_MOVE") {
          bufferedAi = payload;
        }
      }

      async function pumpTtsQueue() {
        if (sendLoopActive) {
          return;
        }
        sendLoopActive = true;
        while (ttsQueue.length > 0) {
          if (!socket || socket.readyState !== WebSocket.OPEN || !ttsReady) {
            await sleep(200);
            continue;
          }
          if (speaking) {
            await sleep(100);
            continue;
          }
          const now = Date.now();
          const elapsed = now - lastSentAt;
          if (elapsed < 1000) {
            await sleep(1000 - elapsed);
          }
          const next = ttsQueue.shift();
          if (!next) {
            continue;
          }
          const payload = { type: "text", text: next.text };
          socket.send(JSON.stringify(payload));
          lastSentAt = Date.now();
          speaking = true;
        }
        sendLoopActive = false;
      }

      async function startDemo() {
        const response = await fetch("/start-game-demo");
        const data = await response.json();
        gameIdEl.textContent = data.game_id || "—";
        gameUrlEl.textContent = data.game_url || "—";
        gameUrlEl.href = data.game_url || "#";

        const eventSource = new EventSource(`/events/${data.game_id}`);
        eventSource.addEventListener("commentary", (event) => {
          try {
            const payload = JSON.parse(event.data);
            handleCommentary(payload);
          } catch (error) {
            return;
          }
        });
        eventSource.addEventListener("open", () => {
          sseStatusEl.textContent = "connected";
          sseStatusEl.classList.add("connected");
        });
        eventSource.addEventListener("error", () => {
          sseStatusEl.textContent = "disconnected";
          sseStatusEl.classList.remove("connected");
        });
      }

      connectSocket();
      startDemo();
    </script>
  </body>
</html>
"""
    return Response(content=html, media_type="text/html")
