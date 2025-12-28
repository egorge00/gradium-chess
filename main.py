import os
import requests
import urllib3
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

app = FastAPI()

TEXT = "Bonjour, je m'appelle Georges"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Gradium TTS Test</title>
</head>
<body>
  <h1>Test Gradium TTS</h1>
  <p>Phrase : Bonjour, je m'appelle Georges</p>
  <button id="go">Go</button>

  <script>
    const button = document.getElementById("go");

    button.addEventListener("click", async () => {
      const response = await fetch("/tts", { method: "POST" });
      const arrayBuffer = await response.arrayBuffer();

      const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
      const source = audioCtx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(audioCtx.destination);
      source.start();
    });
  </script>
</body>
</html>
"""

@app.post("/tts")
def tts():
    api_key = os.getenv("GRADIUM_API_KEY")
    if not api_key:
        return Response("Missing GRADIUM_API_KEY", status_code=500)

    payload = {
        "text": TEXT,
        "voice_id": "b35yykvVppLXyw_l",
        "format": "wav"
    }

    try:
        r = requests.post(
            "https://api.gradium.ai/v1/tts/synthesize",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30,
            verify=False,
        )
    except requests.RequestException as e:
        return Response(f"Gradium request failed: {e}", status_code=502)

    if r.status_code != 200:
        return Response(f"Gradium error: {r.status_code} {r.text}", status_code=502)

    return Response(
        content=r.content,
        media_type="audio/wav"
    )
