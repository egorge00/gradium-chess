import os
import asyncio
import threading
import json
import time
import requests
from collections import deque
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
import gradium

app = FastAPI(title="Gradium Chess Coach PoC")

# =========================
# CONFIG
# =========================
VOICE_COACH_ID = "b35yykvVppLXyw_l"   # Coach (joueur)
VOICE_AI_ID = "axlOaUiFyOZhy4nv"      # IA (ordinateur)

LICHESS_TOKEN = os.getenv("LICHESS_TOKEN")

# =========================
# STATE
# =========================
COMMENTARY_QUEUE = deque()
QUEUE_LOCK = threading.Lock()
WORKER_RUNNING = False

GAME_AUDIO = {}  # game_id -> deque[wav_bytes]
GAME_AUDIO_LOCK = threading.Lock()


# =========================
# COMMENTARY
# =========================
def generate_commentary(move_uci: str, role: str) -> str:
    """
    Commentaires simples, lisibles à voix haute
    """
    if role == "PLAYER":
        return "Bon coup, tu développes ta position."
    return "L'ordinateur répond en contrôlant le centre."


# =========================
# GRADIUM TTS
# =========================
async def generate_tts(voice_id: str, text: str) -> bytes:
    client = gradium.client.GradiumClient()
    result = await client.tts(
        setup={
            "model_name": "default",
            "voice_id": voice_id,
            "output_format": "wav",
        },
        text=text,
    )
    return result.raw_data


def process_commentary_queue():
    global WORKER_RUNNING

    while True:
        with QUEUE_LOCK:
            if not COMMENTARY_QUEUE:
                WORKER_RUNNING = False
                return
            game_id, role, move = COMMENTARY_QUEUE.popleft()

        text = generate_commentary(move, role)
        voice = VOICE_COACH_ID if role == "PLAYER" else VOICE_AI_ID

        try:
            audio = asyncio.run(generate_tts(voice, text))
        except Exception as e:
            print("Erreur TTS:", e)
            continue

        with GAME_AUDIO_LOCK:
            GAME_AUDIO[game_id].append(audio)


def enqueue_commentary(game_id: str, role: str, move: str):
    global WORKER_RUNNING

    with QUEUE_LOCK:
        COMMENTARY_QUEUE.append((game_id, role, move))
        if WORKER_RUNNING:
            return
        WORKER_RUNNING = True

    threading.Thread(target=process_commentary_queue, daemon=True).start()


# =========================
# LICHESS STREAM
# =========================
def stream_lichess_game(game_id: str):
    headers = {"Authorization": f"Bearer {LICHESS_TOKEN}"}
    url = f"https://lichess.org/api/board/game/stream/{game_id}"

    moves_seen = []

    with requests.get(url, headers=headers, stream=True) as r:
        for line in r.iter_lines():
            if not line:
                continue

            data = json.loads(line.decode())
            if data.get("type") == "gameState":
                moves = data.get("moves", "").split()
                new_moves = moves[len(moves_seen):]
                moves_seen = moves

                for i, move in enumerate(new_moves):
                    absolute_index = len(moves_seen) - len(new_moves) + i
                    role = "PLAYER" if absolute_index % 2 == 0 else "AI"
                    enqueue_commentary(game_id, role, move)


# =========================
# ROUTES
# =========================
@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Gradium Chess Coach</title>
        <style>
            body { font-family: sans-serif; padding: 20px; }
            #board { margin-top: 20px; }
            iframe { border: none; }
        </style>
    </head>
    <body>

        <h1>Gradium Chess Coach</h1>
        <button onclick="start()">Démarrer la démo</button>

        <div id="board"></div>

        <script>
        let gameId = null;

        async function start(){
            const r = await fetch('/start');
            const j = await r.json();
            gameId = j.game_id;

            document.getElementById('board').innerHTML = `
                <iframe
                    src="https://lichess.org/${gameId}/embed"
                    width="400"
                    height="444"
                    allowfullscreen
                ></iframe>
            `;

            pollAudio();
        }

        async function pollAudio(){
            setInterval(async ()=>{
                if(!gameId) return;
                const r = await fetch('/audio/' + gameId);
                if(r.status === 200){
                    const blob = await r.blob();
                    new Audio(URL.createObjectURL(blob)).play();
                }
            }, 3000);
        }
        </script>

    </body>
    </html>
    """


@app.get("/start")
def start_game():
    payload = {
        "level": 3,
        "clock.limit": 600,
        "clock.increment": 0,
        "rated": "false",
    }

    r = requests.post(
        "https://lichess.org/api/challenge/ai",
        headers={"Authorization": f"Bearer {LICHESS_TOKEN}"},
        data=payload,
    )
    data = r.json()

    game_id = data["id"]

    with GAME_AUDIO_LOCK:
        GAME_AUDIO[game_id] = deque()

    threading.Thread(
        target=stream_lichess_game,
        args=(game_id,),
        daemon=True
    ).start()

    return {
        "game_id": game_id
    }


@app.get("/audio/{game_id}")
def get_audio(game_id: str):
    with GAME_AUDIO_LOCK:
        if game_id not in GAME_AUDIO or not GAME_AUDIO[game_id]:
            return Response(status_code=204)
        audio = GAME_AUDIO[game_id].popleft()

    return Response(content=audio, media_type="audio/wav")
