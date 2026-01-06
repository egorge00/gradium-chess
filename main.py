import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

import gradium  # pip install gradium

app = FastAPI()

TEXT = "Bonjour, je m'appelle Georges"
VOICE_ID = "b35yykvVppLXyw_l"  # Elise (fr)
OUTPUT_FORMAT = "wav"


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Gradium TTS Simple</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 32px; }
    button { font-size: 16px; padding: 10px 14px; }
    #status { margin-top: 12px; color: #555; }
  </style>
</head>
<body>
  <h1>Gradium TTS Test</h1>
  <p>Phrase : <b>Bonjour, je m'appelle Georges</b></p>
  <button id="go">Go</button>
  <div id="status"></div>
  <audio id="player" controls style="margin-top:12px; width: 100%; max-width: 520px;"></audio>

  <script>
    const btn = document.getElementById("go");
    const statusEl = document.getElementById("status");
    const player = document.getElementById("player");

    btn.addEventListener("click", async () => {
      statusEl.textContent = "Génération audio…";
      btn.disabled = true;

      try {
        const res = await fetch("/tts", { method: "POST" });
        const ct = res.headers.get("content-type") || "";

        if (!res.ok) {
          const msg = await res.text().catch(() => "");
          alert("Erreur TTS : " + (msg || ("HTTP " + res.status)));
          statusEl.textContent = "";
          return;
        }

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);

        player.src = url;
        player.load();
        await player.play();

        statusEl.textContent = "Lecture OK";
      } catch (e) {
        alert("Erreur TTS : " + e);
        statusEl.textContent = "";
      } finally {
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


@app.post("/tts")
async def tts():
    api_key = os.getenv("GRADIUM_API_KEY")
    if not api_key:
        return Response("Missing GRADIUM_API_KEY", status_code=500)

    # Conformément à la doc Gradium: client.tts(setup={voice_id, output_format, model_name}, text=...)
    # output_format "wav" renvoie directement un WAV lisible côté navigateur.
    client = gradium.client.GradiumClient(api_key=api_key)

    try:
        result = await client.tts(
            setup={
                "model_name": "default",
                "voice_id": VOICE_ID,
                "output_format": OUTPUT_FORMAT,
            },
            text=TEXT,
        )
    except Exception as e:
        # On renvoie un message lisible par ton alert() côté front
        return Response(f"Gradium error: {e}", status_code=502)

    return Response(content=result.raw_data, media_type="audio/wav")
