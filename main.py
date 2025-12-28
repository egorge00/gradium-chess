import os
import asyncio
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
import gradium

app = FastAPI()

VOICE_ID = "b35yykvVppLXyw_l"
TEXT_TO_SPEAK = "Bonjour, je m'appelle Georges"


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Gradium TTS Demo</title>
    </head>
    <body style="font-family: sans-serif; padding: 40px;">
        <h1>Gradium TTS</h1>
        <button onclick="play()">Go</button>

        <script>
            async function play() {
                const response = await fetch('/tts', { method: 'POST' });
                if (!response.ok) {
                    alert("Erreur TTS");
                    return;
                }
                const blob = await response.blob();
                const audio = new Audio(URL.createObjectURL(blob));
                audio.play();
            }
        </script>
    </body>
    </html>
    """


@app.post("/tts")
async def tts():
    try:
        client = gradium.client.GradiumClient()

        result = await client.tts(
            setup={
                "model_name": "default",
                "voice_id": VOICE_ID,
                "output_format": "wav",
            },
            text=TEXT_TO_SPEAK,
        )

        return Response(
            content=result.raw_data,
            media_type="audio/wav",
            headers={
                "Content-Disposition": "inline; filename=tts.wav"
            }
        )

    except Exception as e:
        print("Erreur TTS :", e)
        return Response(
            content=f"Erreur TTS : {str(e)}",
            status_code=500
        )
