import os
import requests
import urllib3
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

# --- CONFIG -------------------------------------------------

TEXT = "Bonjour, je m'appelle Georges"
VOICE_ID = "b35yykvVppLXyw_l"
GRADIUM_TTS_URL = "https://api.gradium.ai/v1/tts/synthesize"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

# --- FRONT --------------------------------------------------

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
      console.log("CLICK OK");

      const response = await fetch("/tts", { method: "POST" });

      if (!response.ok) {
        alert("Erreur TTS");
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

# --- BACKEND ------------------------------------------------

@app.post("/tts")
def tts():
    api_key = os.getenv("GRADIUM_API_KEY")
    if not api_key:
        return Response("Missing GRADIUM_API_KEY", status_code=500)

    payload = {
        "text": TEXT,
        "voice_id": VOICE_ID,
        "format": "wav"
    }

    try:
        response = requests.post(
            GRADIUM_TTS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
            verify=False,  # recommandé par Gradium en dev
        )
    except requests.RequestException as e:
        return Response(f"Gradium request failed: {e}", status_code=502)

    if response.status_code != 200:
        return Response(
            f"Gradium error {response.status_code}: {response.text}",
            status_code=502,
        )

    return Response(
        content=response.content,
        media_type="audio/wav"
    )
