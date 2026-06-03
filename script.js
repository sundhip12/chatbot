// ── Train Page ────────────────────────────────────────────

const dropZone     = document.getElementById("dropZone");
const fileInput    = document.getElementById("fileInput");
const selectedFile = document.getElementById("selectedFile");
const fileName     = document.getElementById("fileName");
const trainBtn     = document.getElementById("trainBtn");
const trainBtnText = document.getElementById("trainBtnText");
const progressWrap = document.getElementById("progressWrap");
const progressFill = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");
const resultBox    = document.getElementById("resultBox");
const resetBtn     = document.getElementById("resetBtn");

let selectedFileObj = null;

if (dropZone) {

  dropZone.addEventListener("dragover", e => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("drag-over");
  });

  dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file && isAllowed(file.name)) {
      setFile(file);
    } else {
      showResult("❌ Supported files: PDF, DOCX, TXT, MD", "error");
    }
  });

  dropZone.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (!file) return;
    if (isAllowed(file.name)) {
      setFile(file);
    } else {
      showResult("❌ Supported files: PDF, DOCX, TXT, MD", "error");
    }
  });

  function isAllowed(name) {
    return [".pdf", ".docx", ".txt", ".md"].some(ext => name.toLowerCase().endsWith(ext));
  }

  function setFile(file) {
    selectedFileObj = file;
    fileName.textContent = file.name;
    selectedFile.style.display = "flex";
    trainBtn.disabled = false;
    resultBox.style.display = "none";
  }

  trainBtn.addEventListener("click", async () => {
    if (!selectedFileObj) return;

    trainBtn.disabled = true;
    trainBtnText.textContent = "Training...";
    progressWrap.style.display = "block";
    resultBox.style.display = "none";

    let progress = 0;
    const interval = setInterval(() => {
      progress = Math.min(progress + Math.random() * 15, 90);
      progressFill.style.width = progress + "%";
      if (progress < 30) progressLabel.textContent = "Reading document...";
      else if (progress < 60) progressLabel.textContent = "Chunking text...";
      else progressLabel.textContent = "Building knowledge base...";
    }, 300);

    const formData = new FormData();
    formData.append("file", selectedFileObj);

    try {
      const res = await fetch("/upload", { method: "POST", body: formData });
      const data = await res.json();

      clearInterval(interval);
      progressFill.style.width = "100%";
      progressLabel.textContent = "Done!";

      setTimeout(() => {
        progressWrap.style.display = "none";
        progressFill.style.width = "0%";
        trainBtn.disabled = false;
        trainBtnText.textContent = "Train Document";

        if (data.error) {
          showResult("❌ " + data.error, "error");
        } else {
          showResult(
            `✅ Training complete — <strong>${data.filename}</strong> indexed with <strong>${data.chunks}</strong> chunks. Total: <strong>${data.total_chunks}</strong>`,
            "success"
          );
          setTimeout(() => location.reload(), 1500);
        }
      }, 500);

    } catch (err) {
      clearInterval(interval);
      progressWrap.style.display = "none";
      trainBtn.disabled = false;
      trainBtnText.textContent = "Train Document";
      showResult("❌ Could not connect to server. Make sure the backend is running.", "error");
    }
  });

  function showResult(msg, type) {
    resultBox.innerHTML = msg;
    resultBox.className = "result-box " + type;
    resultBox.style.display = "block";
  }

  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      if (!confirm("Are you sure you want to clear all trained documents?")) return;
      try {
        const res = await fetch("/reset", { method: "DELETE" });
        const data = await res.json();
        showResult("🗑 " + data.message, "success");
        setTimeout(() => location.reload(), 1000);
      } catch (err) {
        showResult("❌ Could not connect to server.", "error");
      }
    });
  }
}

// ── Train from Text ───────────────────────────────────────

const trainTextBtn = document.getElementById("trainTextBtn");
const trainTextResult = document.getElementById("trainTextResult");

function showTextResult(msg, type) {
  if (!trainTextResult) return;
  trainTextResult.innerHTML = msg;
  trainTextResult.className = "result-box " + type;
  trainTextResult.style.display = "block";
}

if (trainTextBtn) {
  trainTextBtn.addEventListener("click", async () => {
    const text = document.getElementById("manualText").value.trim();

    if (!text) {
      showTextResult("❌ Please enter some text before training.", "error");
      return;
    }

    trainTextBtn.disabled = true;
    trainTextBtn.textContent = "Training...";
    if (trainTextResult) trainTextResult.style.display = "none";

    try {
      const response = await fetch("/train-text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });

      const data = await response.json();

      if (response.ok) {
        showTextResult(
          `✅ Text trained successfully — <strong>${data.chunks}</strong> chunk(s) added. Total: <strong>${data.total_chunks}</strong>`,
          "success"
        );
        document.getElementById("manualText").value = "";
        setTimeout(() => location.reload(), 1500);
      } else {
        showTextResult("❌ " + (data.error || "Unknown error occurred."), "error");
      }
    } catch (err) {
      showTextResult("❌ Could not connect to server. Make sure the backend is running.", "error");
    } finally {
      trainTextBtn.disabled = false;
      trainTextBtn.textContent = "Train Text";
    }
  });
}

// ── Chat Page ─────────────────────────────────────────────

const chatWindow    = document.getElementById("chatWindow");
const questionInput = document.getElementById("questionInput");
const sendBtn       = document.getElementById("sendBtn");

if (chatWindow) {

  async function sendQuestion() {
    const question = questionInput.value.trim();
    if (!question) return;

    appendUserMessage(question);
    questionInput.value = "";
    sendBtn.disabled = true;

    const typingId = showTyping();

    try {
      const res = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question })
      });
      const data = await res.json();

      removeTyping(typingId);

      if (data.error) {
        appendBotError(data.error);
      } else if (data.fallback) {
        appendFallback(data.message);
      } else {
        appendAnswer(
    data.answer,
    data.source,
    data.page,
    data.related_questions
);;
      }

    } catch (err) {
      removeTyping(typingId);
      appendBotError("Could not connect to server. Make sure the backend is running.");
    }

    sendBtn.disabled = false;
    scrollToBottom();
  }

  sendBtn.addEventListener("click", sendQuestion);

  questionInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendQuestion();
    }
  });

  function appendUserMessage(text) {
    const msg = document.createElement("div");
    msg.className = "message user-message";
    msg.innerHTML = `
      <div class="message-avatar">👤</div>
      <div class="message-content">
        <div class="message-bubble">${escapeHtml(text)}</div>
      </div>`;
    chatWindow.appendChild(msg);
    scrollToBottom();
  }

  function appendAnswer(answer, source, page, relatedQuestions = []) {
    const wrap = document.createElement("div");
    wrap.className = "message bot-message";
    const relatedHtml = relatedQuestions
  .map(q =>
    `<button class="related-btn"
      onclick="askSuggestedQuestion('${q}')">
      ${q}
    </button>`
  )
  .join("");
    wrap.innerHTML = `
      <div class="message-avatar">🤖</div>
      <div class="message-content">
        <div class="chunk-card">
          ${escapeHtml(answer)}
          <hr>
          <small>📄 Source: <strong>${escapeHtml(source)}</strong> (Page ${page})</small>
          <div class="related-section">
          ${relatedHtml}
          </div>
        </div>
      </div>`;
    chatWindow.appendChild(wrap);
    scrollToBottom();
  }

  function appendFallback(message) {
    const wrap = document.createElement("div");
    wrap.className = "message bot-message";
    wrap.innerHTML = `
      <div class="message-avatar">🤖</div>
      <div class="message-content">
        <div class="fallback-card">
          <span>⚠️</span>
          <span>${escapeHtml(message)}</span>
        </div>
      </div>`;
    chatWindow.appendChild(wrap);
    scrollToBottom();
  }

  function appendBotError(msg) {
    const wrap = document.createElement("div");
    wrap.className = "message bot-message";
    
    wrap.innerHTML = `
      <div class="message-avatar">🤖</div>
      <div class="message-content">
        <div class="chunk-card" style="border-color:#f5c6cb;background:#fdecea;color:#c0392b;">
          ❌ ${escapeHtml(msg)}
        </div>
      </div>`;
    chatWindow.appendChild(wrap);
    scrollToBottom();
  }

  function showTyping() {
    const id = "typing-" + Date.now();
    const wrap = document.createElement("div");
    wrap.className = "message bot-message";
    wrap.id = id;
    wrap.innerHTML = `
      <div class="message-avatar">🤖</div>
      <div class="message-content">
        <div class="typing-indicator">
          <div class="typing-dot"></div>
          <div class="typing-dot"></div>
          <div class="typing-dot"></div>
        </div>
      </div>`;
    chatWindow.appendChild(wrap);
    scrollToBottom();
    return id;
  }

  function removeTyping(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
  }

  function scrollToBottom() {
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}
window.askSuggestedQuestion = function(question) {

    questionInput.value = question;

    sendBtn.click();
}