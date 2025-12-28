import os
import json
import asyncio
import struct
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

app = FastAPI()

TEXT = "Bonjour, je m'appelle Georges"
VOICE_ID = "b35yykvVppLXyw_l"
SAMPLE_RATE = 24000


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
  <h1>Gradium TTS</h1>
  <button id="go">Go</button>

  <script>
    document.getElementById("go").onclick = async () => {
      const res = await fetch("/tts", { method: "POST" });
      if (!res.ok) {
        alert("Erreur TTS");
        return;
      }

      const buffer = await res.arrayBuffer();
      const audioCtx = new AudioContext();
      const audioBuffer = await audioCtx.decodeAudioData(buffer);
      const src = audioCtx.createBufferSource();
      src.buffer = audioBuffer;
      src.connect(audioCtx.destination);
      src.start();
    };
  </script>
</body>
</html>
"""


@app.post("/tts")
async def tts():
    api_key = os.getenv("GRADIUM_API_KEY")
    if not api_key:
        return Response("Missing GRADIUM_API_KEY", status_code=500)

    pcm_chunks = []

    async with websockets.connect(
        "wss://eu.api.gradium.ai/api/speech/tts",
        additional_headers=[("x-api-key", api_key)],
        max_size=None,
    ) as ws:
        # 1️⃣ setup
        await ws.send(json.dumps({
            "type": "setup",
            "model_name": "default",
            "voice_id": VOICE_ID,
            "output_format": "pcm_24000",
        }))

        # attendre ready
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "ready":
                break

        # 2️⃣ envoyer le texte
        await ws.send(json.dumps({
            "type": "text",
            "text": TEXT,
        }))

        # 3️⃣ recevoir l'audio
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                pcm_chunks.append(msg)
            else:
                data = json.loads(msg)
                if data.get("type") in ("end", "done", "final"):
                    break

    pcm = b"".join(pcm_chunks)

    # 4️⃣ convertir PCM → WAV
    wav = pcm_to_wav(pcm, SAMPLE_RATE)

    return Response(content=wav, media_type="audio/wav")


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm)
    chunk_size = 36 + data_size

    return (
        b"RIFF"
        + struct.pack("<I", chunk_size)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate,
                       byte_rate, block_align, bits_per_sample)
        + b"data"
        + struct.pack("<I", data_size)
        + pcm
    )
