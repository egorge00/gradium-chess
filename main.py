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
  <title>Chess Demo (Ultra Simple)</title>
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
  </style>
</head>
<body>

<h1>Chess Demo (local)</h1>
<p>Joue les blancs et les noirs librement.</p>

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

const boardEl = document.getElementById("board");
const logEl = document.getElementById("log");

let selected = null;

function render() {
  boardEl.innerHTML = "";
  for (let r = 0; r < 8; r++) {
    const row = document.createElement("tr");
    for (let c = 0; c < 8; c++) {
      const cell = document.createElement("td");
      cell.textContent = pieces[r][c];
      cell.dataset.r = r;
      cell.dataset.c = c;
      cell.className = (r + c) % 2 === 0 ? "white" : "black";
      if (selected && selected.r == r && selected.c == c) {
        cell.classList.add("selected");
      }
      cell.onclick = () => onCellClick(r, c);
      row.appendChild(cell);
    }
    boardEl.appendChild(row);
  }
}

function onCellClick(r, c) {
  if (selected) {
    const piece = pieces[selected.r][selected.c];
    pieces[selected.r][selected.c] = "";
    pieces[r][c] = piece;

    logEl.textContent += `\\n${piece} : ${coord(selected)} → ${coord({r,c})}`;

    selected = null;
    render();
  } else if (pieces[r][c] !== "") {
    selected = { r, c };
    render();
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
