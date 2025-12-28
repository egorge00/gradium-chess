import os
import requests
import urllib3
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

app = FastAPI()

TEXT = "Bonjour, je m'appelle Georges"
VOICE_ID = "b35yykvVppLXyw_l"
GRADIUM_URL = "https://eu.api.gradium.ai/api/speech/tts"

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
  <p>Phrase : <b>Bonjour, je m'appelle Georges</b></p>
  <button id="go">Go</button>

  <script>
    document.getElementById("go").addEventListener("click", async () => {
      const response = await fetch("/tts", { method: "POST" });

      if (!response.ok) {
        const text = await response.text();
        alert("Erreur TTS : " + text);
        return;
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.play();
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
        "type": "text",
        "text": TEXT,
        "voice_id": VOICE_ID,
    }

    try:
        r = requests.post(
            GRADIUM_URL,
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
            verify=False,
        )
    except requests.RequestException as e:
        return Response(f"Gradium request failed: {e}", status_code=502)

    if r.status_code != 200:
        return Response(
            f"Gradium error {r.status_code}: {r.text}",
            status_code=502,
        )

    return Response(content=r.content, media_type="audio/wav")
