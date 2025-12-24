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

import requests
import websockets
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from google import genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

DEFAULT_VOICE_COACH_ID = "b35yykvVppLXyw_l"
DEFAULT_VOICE_AI_ID = "axlOaUiFyOZhy4nv"

COMMENTARY_QUEUE: deque[tuple[str, str, str]] = deque()
COMMENTARY_LOCK = threading.Lock()
COMMENTARY_WORKER_ACTIVE = False
LAST_LLM_CALL_AT: float | None = None
LAST_PROCESSED_MOVE_COUNT = 0
EVENT_SUBSCRIBERS: dict[str, dict] = {}
EVENT_SUBSCRIBERS_LOCK = threading.Lock()
CURRENT_TURN = {
    "player_move": None,
    "ai_move": None,
    "player_commented": False,
    "ai_commented": False,
}
GAME_TTS_MANAGERS: dict[str, "GradiumTTSManager"] = {}
GAME_TTS_LOCK = threading.Lock()
GAME_CONTEXTS: dict[str, dict] = {}
GAME_CONTEXTS_LOCK = threading.Lock()
APP_LOOP: asyncio.AbstractEventLoop | None = None

@app.get("/health")
def health():
    return {"status": "ok"}


@app.on_event("startup")
async def start_tts_worker():
    global APP_LOOP
    APP_LOOP = asyncio.get_running_loop()


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
    <div id="tts-feedback" style="margin-top: 12px; color: #4b5563;"></div>
    <script>
      const button = document.getElementById("start-demo");
      const ttsFeedbackEl = document.getElementById("tts-feedback");
      const audioQueue = [];
      let isPlaying = false;
      let pendingTtsCount = 0;
      let audioContext = null;
      let decodeChain = Promise.resolve();

      function ensureAudioContext() {
        if (!audioContext) {
          audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioContext.state === "suspended") {
          audioContext.resume().catch(() => {});
        }
      }

      function base64ToArrayBuffer(base64) {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) {
          bytes[i] = binary.charCodeAt(i);
        }
        return bytes.buffer;
      }

      function showThinking() {
        ttsFeedbackEl.textContent = "🎧 Le coach réfléchit…";
      }

      function clearThinking() {
        ttsFeedbackEl.textContent = "";
      }

      function markTtsPending() {
        pendingTtsCount += 1;
        showThinking();
      }

      function markTtsComplete() {
        pendingTtsCount = Math.max(0, pendingTtsCount - 1);
        if (pendingTtsCount === 0) {
          clearThinking();
        }
      }

      async function playNextAudio() {
        if (isPlaying) {
          return;
        }
        const buffer = audioQueue.shift();
        if (!buffer) {
          return;
        }
        ensureAudioContext();
        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(audioContext.destination);
        isPlaying = true;
        source.addEventListener("ended", () => {
          isPlaying = false;
          playNextAudio();
        });
        source.start();
      }

      function enqueueAudioChunk(chunk) {
        ensureAudioContext();
        decodeChain = decodeChain
          .then(() => audioContext.decodeAudioData(base64ToArrayBuffer(chunk)))
          .then((buffer) => {
            audioQueue.push(buffer);
            playNextAudio();
          })
          .catch(() => {});
      }

      async function startDemo() {
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
            if (payload && payload.text && payload.role) {
              return;
            }
          } catch (error) {
            return;
          }
        });
        eventSource.addEventListener("tts-start", () => {
          markTtsPending();
        });
        eventSource.addEventListener("tts-audio", (event) => {
          try {
            const payload = JSON.parse(event.data);
            if (payload && payload.chunk) {
              enqueueAudioChunk(payload.chunk);
            }
          } catch (error) {
            return;
          }
        });
        eventSource.addEventListener("tts-end", () => {
          markTtsComplete();
        });
      }

      button.addEventListener("click", async () => {
        ensureAudioContext();
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


class GradiumTTSManager:
    def __init__(
        self,
        game_id: str,
        api_key: str,
        voice_coach_id: str,
        voice_ai_id: str,
    ) -> None:
        self.game_id = game_id
        self.api_key = api_key
        self.voice_coach_id = voice_coach_id
        self.voice_ai_id = voice_ai_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._worker_task: asyncio.Task | None = None
        self._active_voice_id: str | None = None
        self._last_tts_sent_at = 0.0

    async def start(self) -> None:
        if self._worker_task:
            return
        self._ws = await websockets.connect(
            "wss://eu.api.gradium.ai/api/speech/tts",
            additional_headers={"x-api-key": self.api_key},
            max_size=None,
        )
        await self._send_setup(self.voice_coach_id)
        self._worker_task = asyncio.create_task(self._worker())

    async def close(self) -> None:
        if self._worker_task:
            await self._queue.put(None)
            await self._worker_task
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def speak(self, role: str, text: str) -> None:
        if role not in {"PLAYER_MOVE", "AI_MOVE"}:
            raise ValueError(f"Invalid role: {role}")
        if not self._worker_task:
            await self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await self._queue.put((role, text, future))
        await future

    async def _send_setup(self, voice_id: str) -> None:
        if not self._ws:
            raise RuntimeError("TTS websocket not connected")
        payload = {
            "type": "setup",
            "model_name": "default",
            "voice_id": voice_id,
            "output_format": "wav",
        }
        await self._ws.send(json.dumps(payload))
        logger.info("TTS setup sent | game_id=%s | voice_id=%s", self.game_id, voice_id)
        await self._await_ready()
        self._active_voice_id = voice_id

    async def _await_ready(self) -> None:
        if not self._ws:
            raise RuntimeError("TTS websocket not connected")
        while True:
            message = await self._ws.recv()
            if isinstance(message, bytes):
                logger.info(
                    "TTS ready (binary) | game_id=%s | bytes=%s",
                    self.game_id,
                    len(message),
                )
                return
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            message_type = data.get("type")
            if message_type == "ready":
                logger.info("TTS ready | game_id=%s", self.game_id)
                return
            if message_type == "error":
                raise RuntimeError(data.get("message") or "TTS error")

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            role, text, future = item
            try:
                elapsed = time.monotonic() - self._last_tts_sent_at
                if elapsed < 1.0:
                    await asyncio.sleep(1.0 - elapsed)
                self._last_tts_sent_at = time.monotonic()
                await self._speak_once(role, text)
            except Exception as exc:
                logger.warning("TTS speak failed: %s", exc)
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(True)

    async def _speak_once(self, role: str, text: str) -> None:
        if not self._ws:
            raise RuntimeError("TTS websocket not connected")
        voice_id = (
            self.voice_coach_id if role == "PLAYER_MOVE" else self.voice_ai_id
        )
        if voice_id != self._active_voice_id:
            await self._send_setup(voice_id)
        utterance_id = uuid.uuid4().hex
        publish_event(
            self.game_id,
            "tts-start",
            {"role": role, "text": text, "utterance_id": utterance_id},
        )
        await self._ws.send(
            json.dumps({"type": "text", "text": text, "flush": True})
        )
        await self._stream_audio(role, text, utterance_id)

    async def _stream_audio(self, role: str, text: str, utterance_id: str) -> None:
        if not self._ws:
            raise RuntimeError("TTS websocket not connected")
        sequence = 0
        while True:
            message = await self._ws.recv()
            chunk: bytes | None = None
            is_final = False
            if isinstance(message, bytes):
                chunk = message
            else:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                message_type = data.get("type")
                if message_type in {"audio", "audio_chunk", "chunk"}:
                    raw = data.get("data") or data.get("audio")
                    if raw:
                        chunk = base64.b64decode(raw)
                    is_final = bool(
                        data.get("final") or data.get("is_final") or data.get("done")
                    )
                elif message_type in {"done", "end", "final"}:
                    is_final = True
                elif message_type == "error":
                    raise RuntimeError(data.get("message") or "TTS error")

            if chunk:
                publish_event(
                    self.game_id,
                    "tts-audio",
                    {
                        "role": role,
                        "text": text,
                        "utterance_id": utterance_id,
                        "sequence": sequence,
                        "chunk": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                sequence += 1
            if is_final:
                break

        publish_event(
            self.game_id,
            "tts-end",
            {"role": role, "text": text, "utterance_id": utterance_id},
        )


def start_game_internal(
    background_tasks: BackgroundTasks,
    voice_coach_id: str,
    voice_ai_id: str,
) -> dict:
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

    game_context = {
        "game_id": game_id,
        "voice_coach_id": voice_coach_id,
        "voice_ai_id": voice_ai_id,
    }
    with GAME_CONTEXTS_LOCK:
        GAME_CONTEXTS[game_id] = game_context

    background_tasks.add_task(stream_game_state, game_id)
    ensure_tts_manager(game_id)

    return {"game_id": game_id, "game_url": game_url}


def ensure_tts_manager(game_id: str) -> GradiumTTSManager | None:
    gradium_key = os.getenv("GRADIUM_API_KEY")
    if not gradium_key:
        logger.warning("GRADIUM_API_KEY not set; skipping TTS manager")
        return None

    with GAME_CONTEXTS_LOCK:
        game_context = GAME_CONTEXTS.get(game_id)
    if not game_context:
        logger.warning("Missing game context for game_id=%s", game_id)
        return None

    with GAME_TTS_LOCK:
        manager = GAME_TTS_MANAGERS.get(game_id)
        if manager:
            return manager
        manager = GradiumTTSManager(
            game_id=game_id,
            api_key=gradium_key,
            voice_coach_id=game_context["voice_coach_id"],
            voice_ai_id=game_context["voice_ai_id"],
        )
        GAME_TTS_MANAGERS[game_id] = manager
        logger.info(
            "TTS manager enabled | game_id=%s | coach_voice=%s | ai_voice=%s",
            game_id,
            manager.voice_coach_id,
            manager.voice_ai_id,
        )

    if APP_LOOP:
        asyncio.run_coroutine_threadsafe(manager.start(), APP_LOOP)
    else:
        logger.warning("App loop not available; cannot start TTS manager")
    return manager


def shutdown_tts_manager(game_id: str) -> None:
    with GAME_TTS_LOCK:
        manager = GAME_TTS_MANAGERS.pop(game_id, None)
    with GAME_CONTEXTS_LOCK:
        GAME_CONTEXTS.pop(game_id, None)
    if manager and APP_LOOP:
        asyncio.run_coroutine_threadsafe(manager.close(), APP_LOOP)


@app.post("/start-game")
def start_game(background_tasks: BackgroundTasks):
    return start_game_internal(
        background_tasks,
        DEFAULT_VOICE_COACH_ID,
        DEFAULT_VOICE_AI_ID,
    )


@app.get("/start-game-demo")
def start_game_demo(background_tasks: BackgroundTasks, request: Request):
    voice_coach_id = request.query_params.get(
        "voice_coach",
        DEFAULT_VOICE_COACH_ID,
    )
    voice_ai_id = request.query_params.get(
        "voice_ai",
        DEFAULT_VOICE_AI_ID,
    )
    return start_game_internal(
        background_tasks,
        voice_coach_id,
        voice_ai_id,
    )


@app.get("/start-game-test")
def start_game_test(background_tasks: BackgroundTasks):
    return start_game_internal(
        background_tasks,
        DEFAULT_VOICE_COACH_ID,
        DEFAULT_VOICE_AI_ID,
    )


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
                    status = event.get("status")
                    if status and status != "started":
                        logger.info("Game finished with status=%s", status)
                        break
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
    finally:
        shutdown_tts_manager(game_id)


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
            manager = ensure_tts_manager(game_id)
            if not manager:
                logger.warning(
                    "No TTS manager for game_id=%s, skipping TTS",
                    game_id,
                )
            elif APP_LOOP:
                voice_id = (
                    manager.voice_coach_id
                    if role == "PLAYER_MOVE"
                    else manager.voice_ai_id
                )
                logger.info(
                    "TTS speak | game_id=%s | role=%s | voice_id=%s | text_len=%s",
                    game_id,
                    role,
                    voice_id,
                    len(commentary),
                )
                future = asyncio.run_coroutine_threadsafe(
                    manager.speak(role, commentary), APP_LOOP
                )

                def _handle_tts_result(result_future: asyncio.Future) -> None:
                    try:
                        result_future.result()
                    except Exception:
                        logger.exception(
                            "TTS speak failed | game_id=%s | role=%s",
                            game_id,
                            role,
                        )

                future.add_done_callback(_handle_tts_result)
            else:
                logger.warning(
                    "App loop not available; skipping TTS for game_id=%s",
                    game_id,
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
                    "Tu es un coach d echecs vocal, fun, familier et tres oral. "
                    "Tu parles comme a un ami pendant une partie.\n\n"
                    "REGLES STRICTES (OBLIGATOIRES) :\n"
                    "- 1 phrase, 2 maximum\n"
                    "- texte destine a etre lu par une synthese vocale\n"
                    "- INTERDIT :\n"
                    "  - emojis\n"
                    "  - smileys\n"
                    "  - caracteres speciaux\n"
                    "  - guillemets typographiques\n"
                    "  - apostrophes fantaisie\n"
                    "- utiliser uniquement :\n"
                    "  - lettres\n"
                    "  - chiffres\n"
                    "  - virgules\n"
                    "  - points\n"
                    "  - points d exclamation simples\n"
                    "- pas de guillemets autour des phrases\n\n"
                    "STYLE :\n"
                    "- familier\n"
                    "- complice\n"
                    "- vivant\n"
                    "- taquin leger\n"
                    "- jamais mechant\n"
                    "- jamais scolaire\n\n"
                    "--------------------------------\n"
                    "CAS 1 - COUP DU JOUEUR HUMAIN\n"
                    "--------------------------------\n\n"
                    "Tu es le COACH.\n"
                    "Tu t adresses au joueur en disant tu.\n\n"
                    "Objectif :\n"
                    "- reagir a chaud\n"
                    "- commenter une seule idee simple\n"
                    "- encourager ou taquiner gentiment\n\n"
                    "Exemples de style :\n"
                    "- Allez, ca ouvre le centre, bonne idee.\n"
                    "- Ouh la, ta dame est un peu exposee.\n"
                    "- Pas mal, tu prends de l espace.\n"
                    "- Attention, ca peut vite se retourner.\n\n"
                    "--------------------------------\n"
                    "CAS 2 - COUP DE L ORDINATEUR\n"
                    "--------------------------------\n\n"
                    "Tu es L ORDINATEUR.\n"
                    "Tu parles a la premiere personne en disant je.\n\n"
                    "Objectif :\n"
                    "- expliquer ton intention\n"
                    "- ton confiant\n"
                    "- parfois provocateur, mais fun\n\n"
                    "Exemples de style :\n"
                    "- Je te vois venir, je developpe tranquille.\n"
                    "- Je prends, c etait trop tentant.\n"
                    "- Je ferme le centre, on va jouer serre.\n"
                    "- Je contre attaque tout de suite.\n\n"
                    "--------------------------------\n"
                    "IMPORTANT\n"
                    "--------------------------------\n\n"
                    "- Pas d emojis\n"
                    "- Pas de guillemets\n"
                    "- Pas de caracteres non ASCII\n"
                    "- Texte lisible a voix haute\n"
                    "- Maximum 2 phrases"
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


def get_event_queue(game_id: str) -> asyncio.Queue:
    loop = asyncio.get_running_loop()
    with EVENT_SUBSCRIBERS_LOCK:
        entry = EVENT_SUBSCRIBERS.get(game_id)
        if entry and entry["loop"] is loop:
            entry["subscribers"] += 1
            return entry["queue"]
        queue: asyncio.Queue = asyncio.Queue()
        EVENT_SUBSCRIBERS[game_id] = {
            "queue": queue,
            "loop": loop,
            "subscribers": 1,
        }
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
        logger.warning("Failed to publish event: %s", exc)


def publish_commentary_event(game_id: str, role: str, text: str, move: str) -> None:
    publish_event(
        game_id,
        "commentary",
        {"role": role, "text": text, "move": move},
    )


@app.get("/events/{game_id}")
async def events(game_id: str):
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
            raise
        finally:
            release_event_queue(game_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
        TTS: <span id="tts-status" class="status">idle</span>
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

      const audioQueue = [];
      let isPlaying = false;
      let pendingTtsCount = 0;
      let audioContext = null;
      let decodeChain = Promise.resolve();

      function ensureAudioContext() {
        if (!audioContext) {
          audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioContext.state === "suspended") {
          audioContext.resume().catch(() => {});
        }
      }

      function base64ToArrayBuffer(base64) {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) {
          bytes[i] = binary.charCodeAt(i);
        }
        return bytes.buffer;
      }

      function log(message) {
        const line = `[${new Date().toLocaleTimeString()}] ${message}\\n`;
        logEl.textContent = line + logEl.textContent;
      }

      function showThinking() {
        ttsStatusEl.textContent = "🎧 Le coach réfléchit…";
        ttsStatusEl.classList.add("connected");
      }

      function clearThinking() {
        ttsStatusEl.textContent = "idle";
        ttsStatusEl.classList.remove("connected");
      }

      function markTtsPending() {
        pendingTtsCount += 1;
        showThinking();
      }

      function markTtsComplete() {
        pendingTtsCount = Math.max(0, pendingTtsCount - 1);
        if (pendingTtsCount === 0) {
          clearThinking();
        }
      }

      async function playNextAudio() {
        if (isPlaying) {
          return;
        }
        const buffer = audioQueue.shift();
        if (!buffer) {
          return;
        }
        ensureAudioContext();
        const source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(audioContext.destination);
        isPlaying = true;
        source.addEventListener("ended", () => {
          ttsStatusEl.textContent = "idle";
          ttsStatusEl.classList.remove("connected");
          isPlaying = false;
          playNextAudio();
        });
        clearThinking();
        ttsStatusEl.textContent = "playing";
        ttsStatusEl.classList.add("connected");
        source.start();
      }

      function handleCommentary(payload) {
        if (!payload || !payload.text || !payload.role) {
          return;
        }
        log(`${payload.role}: ${payload.text}`);
      }

      function enqueueAudioChunk(chunk) {
        ensureAudioContext();
        decodeChain = decodeChain
          .then(() => audioContext.decodeAudioData(base64ToArrayBuffer(chunk)))
          .then((buffer) => {
            audioQueue.push(buffer);
            playNextAudio();
          })
          .catch(() => {
            ttsStatusEl.textContent = "error";
            ttsStatusEl.classList.remove("connected");
          });
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
        eventSource.addEventListener("tts-start", () => {
          markTtsPending();
        });
        eventSource.addEventListener("tts-audio", (event) => {
          try {
            const payload = JSON.parse(event.data);
            if (payload && payload.chunk) {
              enqueueAudioChunk(payload.chunk);
            }
          } catch (error) {
            return;
          }
        });
        eventSource.addEventListener("tts-end", () => {
          markTtsComplete();
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

      startDemo();

      document.addEventListener("click", () => {
        ensureAudioContext();
      });
    </script>
  </body>
</html>
"""
    return Response(content=html, media_type="text/html")
