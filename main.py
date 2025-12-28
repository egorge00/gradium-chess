import asyncio
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
import gradium

app = FastAPI()

VOICE_1_ID = "b35yykvVppLXyw_l"      # Elise
VOICE_2_ID = "axlOaUiFyOZhy4nv"      # Pierre

TEXT_1 = "Bonjour, je m'appelle Georges"
TEXT_2 = "Bonjour, je m'appelle Pierre"


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

        <button onclick="play('/tts1')">Go</button>
        <button onclick="play('/tts2')">Go2</button>

        <script>
            async function play(endpoint) {
                const response = await fetch(endpoint, { method: 'POST' });
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


@app.post("/tts1")
async def tts_1():
    return await generate_tts(VOICE_1_ID, TEXT_1)


@app.post("/tts2")
async def tts_2():
    return await generate_tts(VOICE_2_ID, TEXT_2)


async def generate_tts(voice_id: str, text: str):
    try:
        client = gradium.client.GradiumClient()

        result = await client.tts(
            setup={
                "model_name": "default",
                "voice_id": voice_id,
                "output_format": "wav",
            },
            text=text,
        )

        return Response(
            content=result.raw_data,
            media_type="audio/wav",
            headers={"Content-Disposition": "inline; filename=tts.wav"},
        )

    except Exception as e:
        print("Erreur TTS :", e)
        return Response(
            content=f"Erreur TTS : {str(e)}",
            status_code=500,
        )
