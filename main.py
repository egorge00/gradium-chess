from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Optional

import chess
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import gradium

app = FastAPI()

# ----------------------------
# Config
# ----------------------------
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")

GRADIUM_API_KEY = os.getenv("GRADIUM_API_KEY", "")
VOICE_WHITE = os.getenv("GRADIUM_VOICE_WHITE", "b35yykvVppLXyw_l")
VOICE_BLACK = os.getenv("GRADIUM_VOICE_BLACK", "axlOaUiFyOZhy4nv")

# In-memory "single demo" state
BOARD = chess.Board()
MOVE_HISTORY_SAN: list[str] = []

# Audio cache: audio_id -> bytes(wav)
AUDIO_STORE: dict[str, bytes] = {}
AUDIO_STORE_CREATED_AT: dict[str, float] = {}
AUDIO_TTL_SECONDS = 10 * 60  # 10 minutes

# SSE subscribers (single global channel)
SUBSCRIBERS_LOCK = asyncio.Lock()
SUBSCRIBERS: set[asyncio.Queue] = set()

# One Gradium client (async)
_GRADIUM_CLIENT: Optional[gradium.client.GradiumClient] = None


# ----------------------------
# Helpers
# ----------------------------
def cleanup_audio_store() -> None:
    now = time.time()
    expired = [
        key
        for key, created_at in AUDIO_STORE_CREATED_AT.items()
        if now - created_at > AUDIO_TTL_SECONDS
    ]
    for key in expired:
        AUDIO_STORE.pop(key, None)
        AUDIO_STORE_CREATED_AT.pop(key, None)


async def publish_event(event: str, payload: dict) -> None:
    async with SUBSCRIBERS_LOCK:
        queues = list(SUBSCRIBERS)
    message = {"event": event, "payload": payload}
    for queue in queues:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            continue


def color_name(color: bool) -> str:
    return "white" if color == chess.WHITE else "black"


def mistral_generate_commentary(
    color_to_play: str,
    san_move: str,
    fen_after: str,
) -> str:
    """
    1 sentence, theatrical, fun, dynamic.
    No emojis. No guillemets. Short.
    """
    if not MISTRAL_API_KEY:
        side = "Blancs" if color_to_play == "white" else "Noirs"
        return f"{side} jouent {san_move}, et le plateau commence à chauffer."

    system_prompt = (
        "Tu es un commentateur d échecs très oral, fun et un peu théâtral.\n"
        "Règles strictes:\n"
        "- Une seule phrase.\n"
        "- Français.\n"
        "- Pas d emojis.\n"
        "- Pas de guillemets.\n"
        "- Ton énergique, vivant, impactant.\n"
        "- Tu parles des Blancs et des Noirs (pas de joueur humain/IA).\n"
        "- Pas de jargon lourd.\n"
    )

    user_prompt = (
        f"C est au tour des {('Blancs' if color_to_play=='white' else 'Noirs')}.\n"
        f"Coup joué: {san_move}\n"
        f"Position après le coup (FEN): {fen_after}\n"
        "Génère une seule phrase, courte, dynamique et théâtrale."
    )

    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.8,
    }

    try:
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        response.raise_for_status()
        data = response.json()
        text = (
            (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
            or ""
        )
        text = text.strip()
        text = text.replace("“", "").replace("”", "").replace('"', "")
        return text if text else "Le coup tombe, et la tension monte d un cran."
    except Exception:
        return "Le coup tombe, et la tension monte d un cran."


async def gradium_tts_wav(text: str, voice_id: str) -> bytes:
    if not GRADIUM_API_KEY:
        raise RuntimeError("Missing GRADIUM_API_KEY")

    global _GRADIUM_CLIENT
    if _GRADIUM_CLIENT is None:
        _GRADIUM_CLIENT = gradium.client.GradiumClient(api_key=GRADIUM_API_KEY)

    result = await _GRADIUM_CLIENT.tts(
        setup={"model_name": "default", "voice_id": voice_id, "output_format": "wav"},
        text=text,
    )
    return result.raw_data


# ----------------------------
# HTTP: UI
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Chess Coach Demo</title>

  <link rel="stylesheet" href="https://unpkg.com/chessboardjs@1.0.0/www/css/chessboard-1.0.0.min.css">
  <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
  <script src="https://unpkg.com/chessboardjs@1.0.0/www/js/chessboard-1.0.0.min.js"></script>
  <script src="https://unpkg.com/chess.js@1.0.0/chess.min.js"></script>

  <style>
    :root{
      --bg:#0b1020;
      --panel:#0f172a;
      --card:#111c36;
      --text:#e5e7eb;
      --muted:#94a3b8;
      --line:rgba(148,163,184,0.18);
      --accent:#7c3aed;
      --accent2:#22c55e;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Apple Color Emoji", "Segoe UI Emoji";
      background: radial-gradient(1200px 800px at 20% 0%, rgba(124,58,237,0.22), transparent 55%),
                  radial-gradient(900px 700px at 85% 15%, rgba(34,197,94,0.16), transparent 55%),
                  var(--bg);
      color:var(--text);
    }
    header{
      padding:20px 22px;
      border-bottom:1px solid var(--line);
      background: linear-gradient(to bottom, rgba(15,23,42,0.75), rgba(15,23,42,0.35));
      backdrop-filter: blur(10px);
      position: sticky; top: 0; z-index: 10;
    }
    .title{
      display:flex; align-items:center; gap:12px;
      font-weight:800; letter-spacing:0.2px;
    }
    .pill{
      font-size:12px;
      padding:4px 10px;
      border:1px solid var(--line);
      border-radius:999px;
      color:var(--muted);
    }
    main{
      padding:22px;
      display:grid;
      grid-template-columns: 560px 1fr;
      gap:18px;
      max-width: 1200px;
      margin: 0 auto;
    }
    .card{
      background: rgba(17,28,54,0.78);
      border:1px solid var(--line);
      border-radius:18px;
      box-shadow: 0 18px 40px rgba(0,0,0,0.30);
      overflow:hidden;
    }
    .card .hd{
      padding:14px 16px;
      border-bottom:1px solid var(--line);
      display:flex; align-items:center; justify-content:space-between;
      background: rgba(15,23,42,0.55);
    }
    .card .hd h2{
      margin:0; font-size:14px; letter-spacing:0.3px;
      color: #dbeafe;
    }
    .card .bd{ padding:16px; }
    .row{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    button{
      background: linear-gradient(180deg, rgba(124,58,237,0.95), rgba(124,58,237,0.75));
      border:1px solid rgba(124,58,237,0.45);
      color:white;
      padding:10px 12px;
      border-radius:12px;
      font-weight:700;
      cursor:pointer;
      box-shadow: 0 10px 24px rgba(124,58,237,0.20);
    }
    button.secondary{
      background: rgba(148,163,184,0.10);
      border:1px solid var(--line);
      box-shadow:none;
      color:var(--text);
      font-weight:650;
    }
    button:disabled{ opacity:0.55; cursor:not-allowed; }
    #board{ width: 520px; margin: 0 auto; }
    .status{
      color: var(--muted);
      font-size: 13px;
      display:flex; gap:10px; align-items:center;
    }
    .dot{
      width:8px;height:8px;border-radius:50%;
      background: rgba(148,163,184,0.55);
    }
    .dot.on{ background: var(--accent2); box-shadow: 0 0 0 4px rgba(34,197,94,0.12); }
    .moves{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace;
      font-size: 12px;
      color: #cbd5e1;
      background: rgba(2,6,23,0.35);
      border:1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      min-height: 120px;
      max-height: 220px;
      overflow:auto;
      line-height: 1.6;
      white-space: pre-wrap;
    }
    .log{
      display:flex; flex-direction:column; gap:10px;
      max-height: 520px;
      overflow:auto;
      padding-right: 6px;
    }
    .bubble{
      border:1px solid var(--line);
      border-radius: 16px;
      padding: 12px 12px;
      background: rgba(2,6,23,0.32);
    }
    .bubble .meta{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      display:flex; justify-content:space-between; gap:10px;
    }
    .tag{
      font-size: 11px; padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
    }
    .tag.white{ border-color: rgba(34,197,94,0.35); color: rgba(167,243,208,0.95); }
    .tag.black{ border-color: rgba(124,58,237,0.35); color: rgba(221,214,254,0.95); }

    @media (max-width: 1100px){
      main{ grid-template-columns: 1fr; }
      #board{ width: min(92vw, 520px); }
    }
  </style>
</head>
<body>
<header>
  <div class="title">
    <div style="width:10px;height:10px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 6px rgba(124,58,237,0.14)"></div>
    Chess Coach Demo
    <span class="pill">Mistral (texte) + Gradium (voix)</span>
  </div>
</header>

<main>
  <section class="card">
    <div class="hd">
      <h2>Plateau</h2>
      <div class="status"><span class="dot" id="sseDot"></span><span id="turnLabel">Tour: —</span></div>
    </div>
    <div class="bd">
      <div id="board"></div>
      <div style="height:12px"></div>
      <div class="row">
        <button id="resetBtn" class="secondary">Recommencer</button>
        <button id="soundBtn" class="secondary">Activer audio</button>
        <span id="hint" class="status">Glisse-dépose une pièce pour jouer.</span>
      </div>
      <div style="height:12px"></div>
      <div class="moves" id="movesBox">Coups : </div>
    </div>
  </section>

  <section class="card">
    <div class="hd">
      <h2>Coach log</h2>
      <div class="status" id="statusText">Prêt.</div>
    </div>
    <div class="bd">
      <div class="log" id="log"></div>
    </div>
  </section>
</main>

<script>
  const game = new Chess();
  let boardUI = null;

  const logEl = document.getElementById("log");
  const movesBox = document.getElementById("movesBox");
  const statusText = document.getElementById("statusText");
  const turnLabel = document.getElementById("turnLabel");
  const sseDot = document.getElementById("sseDot");

  let audioEnabled = false;
  let audioPrimed = false;
  const audioEl = new Audio();
  audioEl.preload = "auto";

  function nowTime(){
    const d = new Date();
    return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
  }

  function setTurnLabel(){
    turnLabel.textContent = "Tour: " + (game.turn() === "w" ? "Blancs" : "Noirs");
  }

  function appendMoveList(){
    const hist = game.history({verbose:false});
    let out = "Coups :\n";
    for(let i=0;i<hist.length;i+=2){
      const n = Math.floor(i/2)+1;
      const w = hist[i] || "";
      const b = hist[i+1] || "";
      out += n + ". " + w + (b ? ("   " + b) : "") + "\n";
    }
    movesBox.textContent = out.trim();
  }

  function bubble(side, text, san){
    const wrap = document.createElement("div");
    wrap.className = "bubble";
    const meta = document.createElement("div");
    meta.className = "meta";

    const left = document.createElement("div");
    left.innerHTML = '<span class="tag ' + side + '">' + (side==="white" ? "Blancs" : "Noirs") + '</span> ' +
                     '<span style="margin-left:8px;color:rgba(226,232,240,0.9)">' + (san || "") + '</span>';

    const right = document.createElement("div");
    right.textContent = nowTime();

    meta.appendChild(left);
    meta.appendChild(right);

    const body = document.createElement("div");
    body.style.fontSize = "15px";
    body.style.lineHeight = "1.4";
    body.textContent = text;

    wrap.appendChild(meta);
    wrap.appendChild(body);
    logEl.prepend(wrap);
  }

  async function primeAudio(){
    if(audioPrimed) return;
    try{
      audioEl.src = "";
      await audioEl.play().catch(()=>{});
      audioEl.pause();
      audioPrimed = true;
      statusText.textContent = "Audio prêt.";
    } catch(e){
    }
  }

  async function playAudioFromUrl(url){
    if(!audioEnabled) return;
    try{
      const res = await fetch(url);
      if(!res.ok) return;
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      audioEl.src = objectUrl;
      await audioEl.play();
    } catch(e){}
  }

  async function syncFromServer(){
    const res = await fetch("/api/state");
    const st = await res.json();
    game.load(st.fen);
    if(boardUI) boardUI.position(st.fen, true);
    setTurnLabel();
    appendMoveList();
  }

  async function sendMove(source, target){
    statusText.textContent = "Analyse du coup…";
    const payload = { from: source, to: target, promotion: "q" };

    const res = await fetch("/api/move", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    if(!res.ok){
      const msg = await res.text().catch(()=>"HTTP "+res.status);
      statusText.textContent = "Coup refusé.";
      alert(msg);
      await syncFromServer();
      return false;
    }

    const data = await res.json();
    game.load(data.fen);
    boardUI.position(data.fen, true);
    setTurnLabel();
    appendMoveList();
    statusText.textContent = "En attente du prochain coup…";
    return true;
  }

  function onDragStart (source, piece) {
    if (game.game_over()) return false;
    if (game.turn() === "w" && piece.search(/^b/) !== -1) return false;
    if (game.turn() === "b" && piece.search(/^w/) !== -1) return false;

    primeAudio();
  }

  async function onDrop (source, target) {
    const move = game.move({from: source, to: target, promotion: "q"});
    if (move === null) return "snapback";
    game.undo();

    const ok = await sendMove(source, target);
    return ok ? undefined : "snapback";
  }

  function onSnapEnd () {
    boardUI.position(game.fen());
  }

  async function init(){
    boardUI = Chessboard("board", {
      position: "start",
      draggable: true,
      onDragStart,
      onDrop,
      onSnapEnd,
    });

    await syncFromServer();

    const es = new EventSource("/events");
    es.addEventListener("open", () => {
      sseDot.classList.add("on");
    });
    es.addEventListener("error", () => {
      sseDot.classList.remove("on");
    });

    es.addEventListener("commentary", async (event) => {
      try{
        const p = JSON.parse(event.data);
        bubble(p.side, p.text, p.san);
        if(p.audio_url) await playAudioFromUrl(p.audio_url);
      }catch(e){}
    });

    document.getElementById("resetBtn").addEventListener("click", async ()=>{
      await primeAudio();
      const r = await fetch("/api/reset", {method:"POST"});
      if(r.ok){
        logEl.innerHTML = "";
        statusText.textContent = "Partie réinitialisée.";
        await syncFromServer();
      }
    });

    document.getElementById("soundBtn").addEventListener("click", async ()=>{
      audioEnabled = true;
      await primeAudio();
      alert("Audio activé.");
    });
  }

  init();
</script>
</body>
</html>
        """.strip()
    )


# ----------------------------
# HTTP: API
# ----------------------------
@app.get("/api/state")
def api_state() -> dict:
    return {
        "fen": BOARD.fen(),
        "turn": color_name(BOARD.turn),
        "moves_san": MOVE_HISTORY_SAN,
    }


@app.post("/api/reset")
def api_reset() -> dict:
    global BOARD, MOVE_HISTORY_SAN
    BOARD = chess.Board()
    MOVE_HISTORY_SAN = []
    cleanup_audio_store()
    return {"ok": True, "fen": BOARD.fen()}


@app.post("/api/move")
async def api_move(request: Request):
    body = await request.json()
    frm = (body.get("from") or "").strip()
    to = (body.get("to") or "").strip()
    promo = (body.get("promotion") or "q").strip().lower()

    if len(frm) != 2 or len(to) != 2:
        raise HTTPException(status_code=400, detail="Invalid move coordinates")

    cleanup_audio_store()

    side_to_play = color_name(BOARD.turn)

    try:
        move = chess.Move.from_uci(frm + to + (promo if promo else ""))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid move format")

    if move not in BOARD.legal_moves:
        try:
            move2 = chess.Move.from_uci(frm + to)
            if move2 in BOARD.legal_moves:
                move = move2
        except Exception:
            pass

    if move not in BOARD.legal_moves:
        raise HTTPException(status_code=400, detail="Illegal move")

    san = BOARD.san(move)
    BOARD.push(move)
    MOVE_HISTORY_SAN.append(san)

    fen_after = BOARD.fen()
    text = mistral_generate_commentary(side_to_play, san, fen_after)

    voice_id = VOICE_WHITE if side_to_play == "white" else VOICE_BLACK

    audio_id = uuid.uuid4().hex
    try:
        wav_bytes = await gradium_tts_wav(text=text, voice_id=voice_id)
        AUDIO_STORE[audio_id] = wav_bytes
        AUDIO_STORE_CREATED_AT[audio_id] = time.time()
        audio_url = f"/api/audio/{audio_id}"
    except Exception as exc:
        audio_url = None
        text = f"{text} (TTS indisponible)"
        await publish_event(
            "commentary",
            {
                "side": side_to_play,
                "san": san,
                "text": text,
                "audio_url": None,
                "error": str(exc),
            },
        )
        return {
            "fen": fen_after,
            "turn": color_name(BOARD.turn),
            "san": san,
            "text": text,
            "audio_url": None,
        }

    await publish_event(
        "commentary",
        {"side": side_to_play, "san": san, "text": text, "audio_url": audio_url},
    )

    return {
        "fen": fen_after,
        "turn": color_name(BOARD.turn),
        "san": san,
        "text": text,
        "audio_url": audio_url,
    }


@app.get("/api/audio/{audio_id}")
def api_audio(audio_id: str):
    cleanup_audio_store()
    data = AUDIO_STORE.get(audio_id)
    if not data:
        return Response("Audio not found", status_code=404)
    return Response(content=data, media_type="audio/wav")


# ----------------------------
# SSE
# ----------------------------
@app.get("/events")
async def events():
    q: asyncio.Queue = asyncio.Queue(maxsize=200)

    async with SUBSCRIBERS_LOCK:
        SUBSCRIBERS.add(q)

    async def gen():
        try:
            yield "event: ping\ndata: {}\n\n"
            while True:
                msg = await q.get()
                event = msg.get("event", "commentary")
                payload = msg.get("payload", {})
                yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            async with SUBSCRIBERS_LOCK:
                SUBSCRIBERS.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
