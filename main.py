import os
import uuid
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response, JSONResponse
import gradium

# -----------------------------
# Config
# -----------------------------
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
GRADIUM_API_KEY = os.getenv("GRADIUM_API_KEY")

VOICE_ID = "b35yykvVppLXyw_l"  # FR
TEXT_MODEL = "mistral-small-latest"

app = FastAPI()


# -----------------------------
# Frontend
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Chess Coach PoC</title>

  <link rel="stylesheet"
    href="https://unpkg.com/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.css" />

  <script src="https://unpkg.com/chess.js@1.0.0/chess.min.js"></script>
  <script src="https://unpkg.com/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.js"></script>

  <style>
    body { font-family: Arial, sans-serif; margin: 32px; }
    #layout { display: flex; gap: 24px; }
    #commentary {
      border: 1px solid #ddd;
      padding: 12px;
      border-radius: 8px;
      min-height: 80px;
    }
  </style>
</head>
<body>

<h1>Chess Coach – PoC</h1>

<div id="layout">
  <div id="board" style="width:420px"></div>

  <div style="flex:1">
    <h3>Coach</h3>
    <div id="commentary">Joue un coup…</div>
    <audio id="player" controls style="margin-top:12px;width:100%"></audio>
  </div>
</div>

<script>
  const game = new Chess();
  const commentaryEl = document.getElementById("commentary");
  const player = document.getElementById("player");

  const board = Chessboard("board", {
    draggable: true,
    position: "start",

    onDrop: async (from, to) => {
      const move = game.move({ from, to, promotion: "q" });
      if (!move) return "snapback";

      const res = await fetch("/move", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          uci: from + to,
          san: move.san,
          fen: game.fen(),
          turn: move.color === "w" ? "white" : "black"
        })
      });

      const data = await res.json();

      commentaryEl.textContent = data.text || "—";

      if (data.audio_url) {
        player.src = data.audio_url;
        await player.play();
      }
    }
  });
</script>

</body>
</html>
"""


# -----------------------------
# Move → Mistral → Gradium
# -----------------------------
@app.post("/move")
def on_move(payload: dict):
    san = payload.get("san", "")
    turn = payload.get("turn", "")

    # ---- Mistral
    text = generate_commentary_mistral(san, turn)

    # ---- Gradium
    audio_id, audio_bytes = synthesize_gradium(text)

    return {
        "text": text,
        "audio_url": f"/audio/{audio_id}.wav"
    }


# -----------------------------
# Audio endpoint
# -----------------------------
AUDIO_CACHE = {}

@app.get("/audio/{audio_id}.wav")
def get_audio(audio_id: str):
    audio = AUDIO_CACHE.get(audio_id)
    if not audio:
        return Response("Audio not found", status_code=404)

    return Response(content=audio, media_type="audio/wav")


# -----------------------------
# Mistral
# -----------------------------
def generate_commentary_mistral(san: str, turn: str) -> str:
    if not MISTRAL_API_KEY:
        return "Coup joué."

    system_prompt = (
        "Tu es un coach d echecs vocal.\n"
        "1 phrase maximum.\n"
        "Style simple, oral, naturel.\n"
        "Pas d emojis. Pas de jargon.\n"
    )

    user_prompt = (
        f"Le joueur vient de jouer {san}. "
        f"Il joue les {turn}. "
        "Commente ce coup simplement."
    )

    try:
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": TEXT_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            },
            timeout=15
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Coup intéressant, voyons la suite."


# -----------------------------
# Gradium
# -----------------------------
def synthesize_gradium(text: str):
    client = gradium.client.GradiumClient(api_key=GRADIUM_API_KEY)

    result = client.tts(
        setup={
            "model_name": "default",
            "voice_id": VOICE_ID,
            "output_format": "wav"
        },
        text=text
    )

    audio_id = uuid.uuid4().hex
    AUDIO_CACHE[audio_id] = result.raw_data
    return audio_id, result.raw_data
