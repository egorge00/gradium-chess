from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()


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

function coord(pos) {
  const files = "abcdefgh";
  return files[pos.c] + (8 - pos.r);
}

render();
</script>

</body>
</html>
"""
