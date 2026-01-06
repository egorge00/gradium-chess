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
  <title>Chess Demo – Simple</title>

  <!-- Chessboard Web Component -->
  <script type="module" src="https://unpkg.com/chessboard-element@1.6.4/dist/chessboard-element.js"></script>

  <!-- Chess.js -->
  <script src="https://unpkg.com/chess.js@1.0.0/chess.min.js"></script>

  <style>
    body {
      font-family: Arial, sans-serif;
      margin: 32px;
    }
    chess-board {
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
  <p>Joue les blancs et les noirs librement.</p>

  <chess-board id="board" draggable></chess-board>

  <div id="log">Coups joués :</div>

  <script>
    const game = new Chess();
    const board = document.getElementById("board");
    const logEl = document.getElementById("log");

    board.addEventListener("drop", (event) => {
      const { source, target } = event.detail;

      const move = game.move({
        from: source,
        to: target,
        promotion: "q"
      });

      if (!move) {
        event.preventDefault();
        return;
      }

      logEl.textContent += "\\n" + move.color.toUpperCase() + ": " + move.san;
      board.position = game.fen();
    });
  </script>

</body>
</html>
"""
