import os
import requests
import urllib3
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

# ⚠️ Gradium utilise un certificat self-signed en HTTP simple
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

TEXT = "Bonjour, je m'appelle Georges"
VOICE_ID = "b35yykvVppLXyw_l"  # ta voix coach


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Gradium TTS Simple</title>
</head>
<body>
  <h1>Gradium TTS – Test simple</h1>
  <p>Phrase : « Bonjour, je m'appelle Georges »</p>
  <button id="go">Go</button>

  <script>
    document.getElementById("go").onclick = async () => {
      try {
        const res = await fetch("/tts", { method: "POST" });
        if (!res.ok) {
          const txt = await res.text();
          alert("Erreur TTS : " + txt);
          return;
        }

        const arrayBuffer = await res.arrayBuffer();
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);
        source.start();
      } catch (e) {
        alert("Erreur JS : " + e);
      }
    };
  </script>
</body>
</html>
"""


@app.post("/tts")
def tts():
    api_key = os.getenv("GRADIUM_API_KEY")
    if not api_key:
        return Response("GRADIUM_API_KEY manquante", status_code=500)

    payload = {
        "text": TEXT,
        "voice_id": VOICE_ID,
        "format": "wav"
    }

    try:
        r = requests.post(
            "https://api.gradium.ai/v1/tts/synthesize",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
            verify=False,  # IMPORTANT pour Gradium en HTTP simple
        )
    except requests.RequestException as e:
        return Response(f"Erreur requête Gradium : {e}", status_code=502)

    if r.status_code != 200:
        return Response(
            f"Erreur Gradium {r.status_code}: {r.text}",
            status_code=502,
        )

    return Response(
        content=r.content,
        media_type="audio/wav",
    )
