from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <title>Chess Demo – Local Board</title>

  <!-- Chessboard.js CSS -->
  <link
    rel="stylesheet"
    href="https://unpkg.com/chessboardjs@1.0.0/www/css/chessboard.css"
  />

  <style>
    body {
      font-family: Arial, sans-serif;
      margin: 32px;
    }
    #board {
      width: 400px;
      margin-bottom: 16px;
    }
    #log {
      margin-top: 16px;
      padding: 12px;
      background: #f3f4f6;
      border-radius: 8px;
      font-family: monospace;
      white-space: pre-wrap;
      max-width: 400px;
    }
  </style>
</head>
<body>

  <h1>Chess Demo (local)</h1>
  <p>Joue les blancs et les noirs, librement.</p>

  <div id="board"></div>

  <div id="log">Coups joués :</div>

  <!-- Libraries -->
  <script src="https://unpkg.com/chess.js@1.0.0/chess.min.js"></script>
  <script src="https://unpkg.com/chessboardjs@1.0.0/www/js/chessboard.js"></script>

  <script>
    const game = new Chess();
    const logEl = document.getElementById("log");

    function logMove(move) {
      logEl.textContent =
        logEl.textContent + "\\n" +
        move.color.toUpperCase() + ": " + move.san;
    }

    const board = Chessboard("board", {
      draggable: true,
      position: "start",
      onDrop: function (source, target) {
        const move = game.move({
          from: source,
          to: target,
          promotion: "q"
        });

        if (move === null) {
          return "snapback";
        }

        logMove(move);
      }
    });
  </script>

</body>
</html>
"""
