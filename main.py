import os

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()


class CommentaryRequest(BaseModel):
    move: str
    color: str


class CommentaryResponse(BaseModel):
    text: str


@app.post("/commentary", response_model=CommentaryResponse)
def commentary(req: CommentaryRequest):
    mistral_key = os.getenv("MISTRAL_API_KEY")
    if not mistral_key:
        return {"text": "Clé Mistral absente."}

    prompt = f"""
Tu es un coach d'échecs vocal.

Contraintes strictes :
- 1 phrase maximum
- français simple
- ton amical
- aucun emoji
- texte destiné à être lu à voix haute

Si le joueur joue :
- parle en disant "tu"

Si c’est l’adversaire :
- parle en disant "je"

Coup joué : {req.move}
Couleur : {req.color}
"""

    payload = {
        "model": "mistral-small-latest",
        "messages": [
            {"role": "system", "content": "Tu es un coach d'échecs."},
            {"role": "user", "content": prompt},
        ],
    }

    try:
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {mistral_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        return {"text": text}
    except Exception:
        return {"text": "Je réfléchis encore à ce coup."}


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Chess Demo (Tour Blanc / Noir)</title>
  <style>
    body {
      font-family: Arial, sans-serif;
      margin: 32px;
    }
    table {
      border-collapse: collapse;
    }
    td {
      width: 60px;
      height: 60px;
      text-align: center;
      vertical-align: middle;
      font-size: 40px;
      cursor: pointer;
      user-select: none;
    }
    .white { background: #f0d9b5; }
    .black { background: #b58863; }
    .selected { outline: 3px solid red; }
    #log {
      margin-top: 16px;
      padding: 12px;
      background: #f3f4f6;
      border-radius: 8px;
      font-family: monospace;
      white-space: pre-wrap;
      max-width: 500px;
    }
    #coach-text {
      padding: 10px;
      border: 1px solid #ddd;
      border-radius: 6px;
      min-height: 40px;
      font-style: italic;
      max-width: 500px;
      margin-top: 8px;
    }
    #turn {
      margin-bottom: 12px;
      font-weight: bold;
    }
  </style>
</head>
<body>

<h1>Chess Demo (local)</h1>
<div id="turn">Tour : Blancs</div>

<table id="board"></table>

<div id="log">Coups joués :</div>

<h3>Coach</h3>
<div id="coach-text"></div>

<script>
const pieces = [
  ["♜","♞","♝","♛","♚","♝","♞","♜"],
  ["♟","♟","♟","♟","♟","♟","♟","♟"],
  ["","","","","","","",""],
  ["","","","","","","",""],
  ["","","","","","","",""],
  ["","","","","","","",""],
  ["♙","♙","♙","♙","♙","♙","♙","♙"],
  ["♖","♘","♗","♕","♔","♗","♘","♖"]
];

const whitePieces = "♙♖♘♗♕♔";
const blackPieces = "♟♜♞♝♛♚";

let turn = "white";
let selected = null;

const boardEl = document.getElementById("board");
const logEl = document.getElementById("log");
const turnEl = document.getElementById("turn");
const coachEl = document.getElementById("coach-text");

function render() {
  boardEl.innerHTML = "";
  turnEl.textContent = "Tour : " + (turn === "white" ? "Blancs" : "Noirs");

  for (let r = 0; r < 8; r++) {
    const row = document.createElement("tr");
    for (let c = 0; c < 8; c++) {
      const cell = document.createElement("td");
      const piece = pieces[r][c];
      cell.textContent = piece;
      cell.className = (r + c) % 2 === 0 ? "white" : "black";

      if (selected && selected.r === r && selected.c === c) {
        cell.classList.add("selected");
      }

      cell.onclick = () => onCellClick(r, c);
      row.appendChild(cell);
    }
    boardEl.appendChild(row);
  }
}

function pieceColor(piece) {
  if (whitePieces.includes(piece)) return "white";
  if (blackPieces.includes(piece)) return "black";
  return null;
}

function onCellClick(r, c) {
  const piece = pieces[r][c];

  if (selected) {
    const movingPiece = pieces[selected.r][selected.c];
    pieces[selected.r][selected.c] = "";
    pieces[r][c] = movingPiece;

    logEl.textContent += `\\n${movingPiece} : ${coord(selected)} → ${coord({r,c})}`;
    const move = `${coord(selected)}${coord({ r, c })}`;
    const moveColor = turn;
    sendMoveToCoach(move, moveColor);

    selected = null;
    turn = turn === "white" ? "black" : "white";
    render();
    return;
  }

  if (!piece) return;

  const color = pieceColor(piece);
  if (color !== turn) return;

  selected = { r, c };
  render();
}

async function sendMoveToCoach(move, color) {
  try {
    const res = await fetch("/commentary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ move, color })
    });
    const data = await res.json();
    coachEl.textContent = data.text || "";
  } catch (error) {
    coachEl.textContent = "Je réfléchis encore à ce coup.";
  }
}

function coord(pos) {
  const files = "abcdefgh";
  return files[pos.c] + (8 - pos.r);
}

render();
</script>

</body>
</html>
"""
