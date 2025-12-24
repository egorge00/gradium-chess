from __future__ import annotations

import asyncio
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
TTS_MANAGER: GradiumTTSManager | None = None
TTS_MANAGER_LOCK = threading.Lock()
GAME_CONTEXTS: dict[str, dict] = {}
GAME_CONTEXTS_LOCK = threading.Lock()
APP_LOOP: asyncio.AbstractEventLoop | None = None
TTS_THROTTLE_SECONDS = 1.0

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
      const ttsWatchdogs = new Map();
      const ttsPendingIds = new Set();
      let ttsWatchdogCounter = 0;
      const TTS_WATCHDOG_MS = 15000;
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

      function startTtsWatchdog(utteranceId) {
        const key = utteranceId || `local-${++ttsWatchdogCounter}`;
        const existing = ttsWatchdogs.get(key);
        if (existing) {
          clearTimeout(existing);
        }
        ttsPendingIds.add(key);
        const timer = setTimeout(() => {
          console.warn("tts watchdog fired", key);
          ttsWatchdogs.delete(key);
          if (ttsPendingIds.delete(key)) {
            markTtsComplete();
          }
        }, TTS_WATCHDOG_MS);
        ttsWatchdogs.set(key, timer);
        return key;
      }

      function clearTtsWatchdog(utteranceId) {
        if (!utteranceId) {
          return;
        }
        const timer = ttsWatchdogs.get(utteranceId);
        if (timer) {
          clearTimeout(timer);
          ttsWatchdogs.delete(utteranceId);
        }
        if (ttsPendingIds.delete(utteranceId)) {
          markTtsComplete();
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
        eventSource.addEventListener("tts-start", (event) => {
          let utteranceId;
          try {
            const payload = JSON.parse(event.data);
            utteranceId = payload && payload.utterance_id;
          } catch (error) {
            utteranceId = undefined;
          }
          markTtsPending();
          startTtsWatchdog(utteranceId);
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
        eventSource.addEventListener("tts-end", (event) => {
          let utteranceId;
          try {
            const payload = JSON.parse(event.data);
            utteranceId = payload && payload.utterance_id;
          } catch (error) {
            utteranceId = undefined;
          }
          clearTtsWatchdog(utteranceId);
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
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._lock = asyncio.Lock()
        self._last_tts_sent_at = 0.0

    async def speak(self, game_id: str, role: str, text: str) -> None:
        if role not in {"PLAYER_MOVE", "AI_MOVE"}:
            raise ValueError(f"Invalid role: {role}")
        voice_id = get_voice_id_for_game(game_id, role)
        if not voice_id:
            raise RuntimeError("Missing voice id for TTS")
        async with self._lock:
            await self._throttle()
            await self._speak_once(game_id, role, text, voice_id)

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_tts_sent_at
        if elapsed < TTS_THROTTLE_SECONDS:
            await asyncio.sleep(TTS_THROTTLE_SECONDS - elapsed)
        self._last_tts_sent_at = time.monotonic()

    async def _speak_once(self, game_id: str, role: str, text: str, voice_id: str) -> None:
        utterance_id = uuid.uuid4().hex
        publish_event(
            game_id,
            "tts-start",
            {"role": role, "text": text, "utterance_id": utterance_id},
        )
        logger.info("TTS connecting")
        ended = False
        try:
            async with websockets.connect(
                "wss://eu.api.gradium.ai/api/speech/tts",
                additional_headers={"x-api-key": self.api_key},
                max_size=None,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "setup",
                            "model_name": "default",
                            "voice_id": voice_id,
                            "output_format": "wav",
                        }
                    )
                )
                await ws.send(json.dumps({"type": "text", "text": text, "flush": True}))
                await self._stream_audio(ws, game_id, role, text, utterance_id)
        finally:
            if not ended:
                publish_event(
                    game_id,
                    "tts-end",
                    {"role": role, "text": text, "utterance_id": utterance_id},
                )
                logger.info(
                    "TTS end emitted | game_id=%s utterance_id=%s role=%s",
                    game_id,
                    utterance_id,
                    role,
                )
                ended = True

    async def _stream_audio(
        self,
        ws: websockets.WebSocketClientProtocol,
        game_id: str,
        role: str,
        text: str,
        utterance_id: str,
    ) -> None:
        sequence = 0
        while True:
            try:
                message = await ws.recv()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
            ) as exc:
                logger.warning("TTS connection closed | reason=%s", exc)
                break
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if isinstance(data, str):
                message_type = data
            elif isinstance(data, dict):
                message_type = data.get("type")
            else:
                continue
            if message_type is None:
                continue
            message_type = str(message_type).lower()
            if message_type == "audio":
                raw = data.get("audio")
                if not raw:
                    continue
                publish_event(
                    game_id,
                    "tts-audio",
                    {
                        "role": role,
                        "text": text,
                        "utterance_id": utterance_id,
                        "sequence": sequence,
                        "chunk": raw,
                    },
                )
                logger.info("TTS audio chunk | sequence=%s", sequence)
                sequence += 1
                continue
            if message_type in {"done", "end", "final", "eos", "eof", "end_of_stream"}:
                logger.info("TTS eos")
                break
            if message_type == "error":
                raise RuntimeError(data.get("message") or "TTS error")


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
    ensure_tts_manager()

    return {"game_id": game_id, "game_url": game_url}


def ensure_tts_manager() -> GradiumTTSManager | None:
    gradium_key = os.getenv("GRADIUM_API_KEY")
    if not gradium_key:
        logger.warning("GRADIUM_API_KEY not set; skipping TTS manager")
        return None
    with TTS_MANAGER_LOCK:
        global TTS_MANAGER
        if TTS_MANAGER:
            return TTS_MANAGER
        TTS_MANAGER = GradiumTTSManager(api_key=gradium_key)
        logger.info("TTS manager enabled")

    return TTS_MANAGER


def shutdown_tts_manager(game_id: str) -> None:
    with GAME_CONTEXTS_LOCK:
        GAME_CONTEXTS.pop(game_id, None)
    with TTS_MANAGER_LOCK:
        global TTS_MANAGER
        TTS_MANAGER = None


def get_voice_id_for_game(game_id: str, role: str) -> str:
    if role not in {"PLAYER_MOVE", "AI_MOVE"}:
        raise ValueError(f"Invalid role: {role}")
    with GAME_CONTEXTS_LOCK:
        game_context = GAME_CONTEXTS.get(game_id, {})
    voice_coach_id = game_context.get("voice_coach_id", DEFAULT_VOICE_COACH_ID)
    voice_ai_id = game_context.get("voice_ai_id", DEFAULT_VOICE_AI_ID)
    return voice_coach_id if role == "PLAYER_MOVE" else voice_ai_id


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
            manager = ensure_tts_manager()
            if not manager:
                logger.warning(
                    "No TTS manager for game_id=%s, skipping TTS",
                    game_id,
                )
            elif APP_LOOP:
                voice_id = get_voice_id_for_game(game_id, role)
                logger.info(
                    "TTS speak | game_id=%s | role=%s | voice_id=%s | text_len=%s",
                    game_id,
                    role,
                    voice_id,
                    len(commentary),
                )
                future = asyncio.run_coroutine_threadsafe(
                    manager.speak(game_id, role, commentary), APP_LOOP
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
      const ttsWatchdogs = new Map();
      const ttsPendingIds = new Set();
      let ttsWatchdogCounter = 0;
      const TTS_WATCHDOG_MS = 15000;
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

      function startTtsWatchdog(utteranceId) {
        const key = utteranceId || `local-${++ttsWatchdogCounter}`;
        const existing = ttsWatchdogs.get(key);
        if (existing) {
          clearTimeout(existing);
        }
        ttsPendingIds.add(key);
        const timer = setTimeout(() => {
          console.warn("tts watchdog fired", key);
          ttsWatchdogs.delete(key);
          if (ttsPendingIds.delete(key)) {
            markTtsComplete();
          }
        }, TTS_WATCHDOG_MS);
        ttsWatchdogs.set(key, timer);
        return key;
      }

      function clearTtsWatchdog(utteranceId) {
        if (!utteranceId) {
          return;
        }
        const timer = ttsWatchdogs.get(utteranceId);
        if (timer) {
          clearTimeout(timer);
          ttsWatchdogs.delete(utteranceId);
        }
        if (ttsPendingIds.delete(utteranceId)) {
          markTtsComplete();
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
        eventSource.addEventListener("tts-start", (event) => {
          let utteranceId;
          try {
            const payload = JSON.parse(event.data);
            utteranceId = payload && payload.utterance_id;
          } catch (error) {
            utteranceId = undefined;
          }
          markTtsPending();
          startTtsWatchdog(utteranceId);
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
        eventSource.addEventListener("tts-end", (event) => {
          let utteranceId;
          try {
            const payload = JSON.parse(event.data);
            utteranceId = payload && payload.utterance_id;
          } catch (error) {
            utteranceId = undefined;
          }
          clearTtsWatchdog(utteranceId);
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
