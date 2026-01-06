<body>
  <h1>Gradium TTS Test</h1>

  <button id="go">Go</button>
  <div id="status"></div>

  <!-- ✅ PLAYER AUDIO MANQUANT -->
  <audio id="player"></audio>

  <script>
    const btn = document.getElementById("go");
    const statusEl = document.getElementById("status");
    const player = document.getElementById("player");

    btn.addEventListener("click", async () => {
      statusEl.textContent = "Génération audio…";
      btn.disabled = true;

      try {
        const res = await fetch("/tts", { method: "POST" });

        if (!res.ok) {
          const msg = await res.text().catch(() => "");
          alert("Erreur TTS : " + (msg || ("HTTP " + res.status)));
          statusEl.textContent = "";
          return;
        }

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);

        player.src = url;
        player.load();
        await player.play();

        statusEl.textContent = "Lecture OK";
      } catch (e) {
        alert("Erreur TTS : " + e);
        statusEl.textContent = "";
      } finally {
        btn.disabled = false;
      }
    });
  </script>
</body>
