import json
import logging
import os
import urllib.parse
import urllib.request

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/env-check")
def env_check():
    ai_level_value = os.getenv("AI_LEVEL")
    try:
        ai_level = int(ai_level_value) if ai_level_value is not None else None
    except ValueError:
        ai_level = None

    return {
        "has_lichess_token": bool(os.getenv("LICHESS_TOKEN")),
        "has_gradium_key": bool(os.getenv("GRADIUM_API_KEY")),
        "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
        "ai_level": ai_level,
    }


def start_game_internal(background_tasks: BackgroundTasks) -> dict:
    lichess_token = os.getenv("LICHESS_TOKEN")
    if not lichess_token:
        raise HTTPException(status_code=500, detail="LICHESS_TOKEN not set")

    ai_level_value = os.getenv("AI_LEVEL", "3")
    try:
        ai_level = int(ai_level_value)
    except ValueError:
        ai_level = 3

    payload = urllib.parse.urlencode(
        {
            "level": ai_level,
            "clock.limit": 600,
            "clock.increment": 0,
            "rated": "false",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        "https://lichess.org/api/challenge/ai",
        data=payload,
        headers={
            "Authorization": f"Bearer {lichess_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request) as response:
        data = json.loads(response.read().decode("utf-8"))

    game_id = (
        data.get("id")
        or data.get("game", {}).get("id")
        or data.get("challenge", {}).get("id")
    )
    if not game_id:
        raise HTTPException(status_code=502, detail="Lichess response missing game id")

    game_url = f"https://lichess.org/{game_id}"

    background_tasks.add_task(stream_game_state, game_id)

    return {"game_id": game_id, "game_url": game_url}


@app.post("/start-game")
def start_game(background_tasks: BackgroundTasks):
    return start_game_internal(background_tasks)


@app.get("/start-game-demo")
def start_game_demo(background_tasks: BackgroundTasks):
    return start_game_internal(background_tasks)


@app.get("/start-game-test")
def start_game_test(background_tasks: BackgroundTasks):
    return start_game_internal(background_tasks)


def stream_game_state(game_id: str) -> None:
    lichess_token = os.getenv("LICHESS_TOKEN")
    if not lichess_token:
        logger.error("LICHESS_TOKEN not set; cannot stream game")
        return

    stream_url = f"https://lichess.org/api/board/game/stream/{game_id}"
    headers = {
        "Authorization": f"Bearer {lichess_token}",
        "Accept": "application/json",
    }

    human_color = None
    last_moves: list[str] = []

    try:
        with requests.get(
            stream_url, headers=headers, stream=True, timeout=60
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    logger.warning("Failed to decode stream line: %s", line)
                    continue
                event_type = event.get("type")
                if event_type == "gameFull":
                    white_player = event.get("white", {})
                    black_player = event.get("black", {})
                    if "aiLevel" in white_player:
                        human_color = "black"
                    elif "aiLevel" in black_player:
                        human_color = "white"
                    else:
                        human_color = None
                    logger.info("gameFull human_color=%s", human_color)

                    initial_moves = event.get("state", {}).get("moves", "")
                    last_moves = initial_moves.split() if initial_moves else []
                    continue

                if event_type == "gameState":
                    moves_text = event.get("moves", "")
                    moves = moves_text.split() if moves_text else []

                    if moves[: len(last_moves)] != last_moves:
                        last_moves = []

                    new_moves = moves[len(last_moves) :]
                    for index, move in enumerate(new_moves, start=len(last_moves)):
                        mover_color = "white" if index % 2 == 0 else "black"
                        if mover_color == human_color:
                            logger.info("PLAYER_MOVE move=%s", move)
                            commentary = generate_commentary(move, "PLAYER_MOVE")
                            if commentary:
                                logger.info(
                                    'COMMENTARY role=PLAYER_MOVE text="%s"',
                                    commentary.replace('"', "'"),
                                )
                        else:
                            logger.info("AI_MOVE move=%s", move)
                            commentary = generate_commentary(move, "AI_MOVE")
                            if commentary:
                                logger.info(
                                    'COMMENTARY role=AI_MOVE text="%s"',
                                    commentary.replace('"', "'"),
                                )
                    last_moves = moves
    except requests.RequestException as exc:
        logger.warning("Lichess stream request failed: %s", exc)


@app.get("/debug/stream/{game_id}")
def debug_stream(game_id: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(stream_game_state, game_id)
    return {"status": "streaming", "game_id": game_id}


@app.get("/debug/gemini-test")
def debug_gemini_test():
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        return {"error": "GEMINI_API_KEY not set"}

    try:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(
            "Dis bonjour en français en une seule phrase."
        )
        text = (response.text or "").strip()
        if not text:
            return {"error": "Empty response from Gemini"}
        return {"text": text}
    except Exception as exc:
        logger.warning("Gemini test request failed: %s", exc)
        return {"error": str(exc)}


def generate_commentary(move_uci: str, role: str) -> str | None:
    if role not in {"PLAYER_MOVE", "AI_MOVE"}:
        raise ValueError(f"Invalid role: {role}")

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("OPENAI_API_KEY not set; skipping commentary")
        return None

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0.6,
        "max_tokens": 120,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu es un coach d’échecs calme et pédagogique. "
                    "Commente le coup donné. 1 phrase (2 max)."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Rôle: {role}. Coup joué (UCI): {move_uci}. "
                    "Commente ce coup."
                ),
            },
        ],
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("OpenAI commentary request failed: %s", exc)
        return None

    data = response.json()
    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    )
    return content or None
