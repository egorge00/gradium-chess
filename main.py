import os
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

app = FastAPI()

TEXT = "Bonjour, je m'appelle Georges"

@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Mistral Voxtral TTS Test</title>
</head>
<body>
  <h1>Test Mistral Voxtral TTS</h1>
  <p>Phrase : Bonjour, je m'appelle Georges</p>
  <button id="go">Go</button>

  <script>
    const button = document.getElementById("go");

    button.addEventListener("click", async () => {
      try {
        const response = await fetch("/tts", { method: "POST" });
        if (!response.ok) {
          const text = await response.text();
          alert("Erreur TTS : " + text);
          return;
        }

        const arrayBuffer = await response.arrayBuffer();
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);

        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);
        source.start();
      } catch (e) {
        alert("Erreur lecture audio");
        console.error(e);
      }
    });
  </script>
</body>
</html>
"""


@app.post("/tts")
def tts():
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        return Response("Missing MISTRAL_API_KEY", status_code=500)

    payload = {
        "model": "voxtral-mini",
        "input": TEXT,
        "voice": "alloy",
        "format": "wav"
    }

    try:
        r = requests.post(
            "https://api.mistral.ai/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        return Response(f"Request failed: {e}", status_code=502)

    if r.status_code != 200:
        return Response(
            f"Mistral error {r.status_code}: {r.text}",
            status_code=502,
        )

    return Response(
        content=r.content,
        media_type="audio/wav",
    )
