// ── Theme Toggle ─────────────────────────────────────────

const THEME_KEY = "lms_theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(THEME_KEY, theme);
  const btn = document.getElementById("themeToggle");
  if (btn) {
    const icon  = btn.querySelector(".toggle-icon");
    const label = btn.querySelector(".toggle-label");
    if (icon)  icon.textContent  = theme === "dark" ? "☀️" : "🌙";
    if (label) label.textContent = theme === "dark" ? "Light" : "Dark";
  }
}

function initTheme() {
  const saved     = localStorage.getItem(THEME_KEY);
  const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  applyTheme(saved || preferred);
}

initTheme();

document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("themeToggle");
  if (btn) {
    btn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "light";
      applyTheme(current === "dark" ? "light" : "dark");
    });
  }
});

// ── Toast notification ────────────────────────────────────

function showToast(msg) {
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2600);
}

// ── Simple Markdown Renderer ─────────────────────────────

function renderMarkdown(text) {
  if (!text) return "";

  let html = "";
  const lines = text.split("\n");
  let i = 0;
  let inCode = false;
  let codeLang = "";
  let codeLines = [];
  let paraLines = [];

  function flushPara() {
    if (!paraLines.length) return;
    let block = paraLines.join("\n")
      .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.*?)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/^\d+\.\s+(.+)$/gm, "<li>$1</li>")
      .replace(/^[-•]\s+(.+)$/gm, "<li>$1</li>")
      .replace(/(<li>.*<\/li>)/gs, m => `<ul style="margin:6px 0 6px 18px;line-height:1.7">${m}</ul>`)
      .replace(/\n{2,}/g, "</p><p style='margin-top:8px'>")
      .replace(/\n/g, "<br>");
    html += `<p style="margin:0 0 6px">${block}</p>`;
    paraLines = [];
  }

  while (i < lines.length) {
    const line = lines[i];

    if (!inCode && /^```/.test(line)) {
      flushPara();
      codeLang = line.replace(/^```/, "").trim() || "plaintext";
      codeLines = [];
      inCode = true;
      i++;
      continue;
    }

    if (inCode) {
      if (/^```/.test(line)) {
        const langLabel = codeLang !== "plaintext"
          ? `<span style="font-size:0.75em;color:var(--text-muted);font-family:var(--font-mono)">${escapeHtml(codeLang)}</span>`
          : "";
        html += `
          <div style="margin:8px 0;background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden">
            ${langLabel ? `<div style="padding:4px 12px;border-bottom:1px solid var(--border)">${langLabel}</div>` : ""}
            <pre style="margin:0;padding:10px 14px;overflow-x:auto;font-family:monospace;font-size:0.83rem;line-height:1.6"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>
          </div>`;
        inCode = false;
        codeLines = [];
      } else {
        codeLines.push(line);
      }
      i++;
      continue;
    }

    paraLines.push(line);
    i++;
  }

  // flush any unclosed code block (streaming mid-block)
  if (inCode && codeLines.length) {
    html += `<pre style="margin:8px 0;padding:10px 14px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow-x:auto;font-family:monospace;font-size:0.83rem;line-height:1.6"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`;
  }

  flushPara();
  return html;
}
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Train Page ────────────────────────────────────────────

const dropZone      = document.getElementById("dropZone");
const fileInput     = document.getElementById("fileInput");
const selectedFile  = document.getElementById("selectedFile");
const fileName      = document.getElementById("fileName");
const trainBtn      = document.getElementById("trainBtn");
const trainBtnText  = document.getElementById("trainBtnText");
const progressWrap  = document.getElementById("progressWrap");
const progressFill  = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");
const resultBox     = document.getElementById("resultBox");
const resetBtn      = document.getElementById("resetBtn");

let selectedFileObj = null;

if (dropZone) {
  dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));

  dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file && isAllowed(file.name)) setFile(file);
    else showResult("❌ Supported files: PDF, DOCX, TXT, MD", "error");
  });

  dropZone.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (!file) return;
    isAllowed(file.name) ? setFile(file) : showResult("❌ Supported files: PDF, DOCX, TXT, MD", "error");
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
    trainBtnText.textContent = "Training…";
    progressWrap.style.display = "block";
    resultBox.style.display = "none";

    let progress = 0;
    const labels = ["Reading document…", "Chunking text…", "Generating embeddings (first run may take 1–2 min)…", "Building knowledge base…"];
    const interval = setInterval(() => {
      progress = Math.min(progress + Math.random() * 12, 90);
      progressFill.style.width = progress + "%";
      progressLabel.textContent = labels[Math.floor(progress / 25)] || labels[3];
    }, 350);

    const formData = new FormData();
    formData.append("file", selectedFileObj);

    try {
      const controller = new AbortController();
      const timeoutId  = setTimeout(() => controller.abort(), 180000);

      const res  = await fetch("/upload", { method: "POST", body: formData, signal: controller.signal });
      clearTimeout(timeoutId);
      const data = await res.json();

      clearInterval(interval);
      progressFill.style.width = "100%";
      progressLabel.textContent = "Done!";

      setTimeout(() => {
        progressWrap.style.display = "none";
        progressFill.style.width   = "0%";
        trainBtn.disabled          = false;
        trainBtnText.textContent   = "Train Document";

        if (data.error) showResult("❌ " + data.error, "error");
        else {
          showResult(
            `✅ <strong>${data.filename}</strong> indexed — <strong>${data.chunks}</strong> chunks · Total: <strong>${data.total_chunks}</strong>`,
            "success"
          );
          setTimeout(() => location.reload(), 1600);
        }
      }, 500);
    } catch (err) {
      clearInterval(interval);
      progressWrap.style.display = "none";
      trainBtn.disabled          = false;
      trainBtnText.textContent   = "Train Document";
      if (err.name === "AbortError") {
        showResult("⏱ Training timed out. The server may still be processing — try reloading in 30s.", "error");
      } else {
        showResult("❌ Could not connect to server. Make sure the backend is running.", "error");
      }
    }
  });

  function showResult(msg, type) {
    resultBox.innerHTML   = msg;
    resultBox.className   = "result-box " + type;
    resultBox.style.display = "block";
  }

  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      if (!confirm("Clear ALL trained documents and chat history?")) return;
      try {
        const res  = await fetch("/reset", { method: "DELETE" });
        const data = await res.json();
        showResult("🗑 " + data.message, "success");
        setTimeout(() => location.reload(), 1100);
      } catch {
        showResult("❌ Could not connect to server.", "error");
      }
    });
  }
}

// ── Per-document delete ───────────────────────────────────

document.querySelectorAll(".btn-doc-delete").forEach(btn => {
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const li = btn.closest(".doc-item");
    li.classList.add("confirming");
  });
});

document.querySelectorAll(".btn-confirm-no").forEach(btn => {
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const li = btn.closest(".doc-item");
    li.classList.remove("confirming");
  });
});

document.querySelectorAll(".btn-confirm-yes").forEach(btn => {
  btn.addEventListener("click", async e => {
    e.stopPropagation();
    const li   = btn.closest(".doc-item");
    const name = li.dataset.docName;
    if (!name) return;

    li.style.opacity = "0.5";
    li.style.pointerEvents = "none";

    try {
      const res  = await fetch("/delete-doc", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: name })
      });
      const data = await res.json();
      if (res.ok) {
        showToast(`🗑 "${name}" removed`);
        li.style.transition = "all 0.3s ease";
        li.style.maxHeight  = li.offsetHeight + "px";
        requestAnimationFrame(() => {
          li.style.maxHeight = "0";
          li.style.opacity   = "0";
          li.style.padding   = "0";
          li.style.margin    = "0";
        });
        setTimeout(() => {
          li.remove();
          // show empty state if no docs left
          const list = document.querySelector(".doc-list");
          if (list && list.children.length === 0) {
            list.innerHTML = "";
            const empty = document.createElement("div");
            empty.className = "empty-state";
            empty.innerHTML = "<p>📭 No documents trained yet.</p><p>Upload a file or paste text to get started.</p>";
            list.parentNode.insertBefore(empty, list);
            list.remove();
          }
          // update doc badge
          const badge = document.querySelector(".doc-badge");
          if (badge) {
            const remaining = document.querySelectorAll(".doc-item").length;
            badge.textContent = remaining + " doc" + (remaining !== 1 ? "s" : "");
          }
        }, 310);
      } else {
        showToast("❌ " + (data.error || "Delete failed"));
        li.style.opacity = "1";
        li.style.pointerEvents = "";
        li.classList.remove("confirming");
      }
    } catch {
      showToast("❌ Could not connect to server.");
      li.style.opacity = "1";
      li.style.pointerEvents = "";
      li.classList.remove("confirming");
    }
  });
});

// ── Train from Text ───────────────────────────────────────

const trainTextBtn    = document.getElementById("trainTextBtn");
const trainTextResult = document.getElementById("trainTextResult");
const manualTextArea  = document.getElementById("manualText");

function showTextResult(msg, type) {
  if (!trainTextResult) return;
  trainTextResult.innerHTML     = msg;
  trainTextResult.className     = "result-box " + type;
  trainTextResult.style.display = "block";
}

// Character counter for textarea
if (manualTextArea) {
  const maxChars = 50000;
  const wrapper  = manualTextArea.parentElement;
  if (wrapper) {
    const counter = document.createElement("span");
    counter.className = "char-count";
    counter.textContent = "0 / " + maxChars.toLocaleString();
    wrapper.style.position = "relative";
    wrapper.appendChild(counter);

    manualTextArea.addEventListener("input", () => {
      const len = manualTextArea.value.length;
      counter.textContent = len.toLocaleString() + " / " + maxChars.toLocaleString();
      counter.className = "char-count" + (len > maxChars ? " over" : len > maxChars * 0.8 ? " warn" : "");
    });
  }
}

if (trainTextBtn) {
  trainTextBtn.addEventListener("click", async () => {
    const text = manualTextArea ? manualTextArea.value.trim() : "";
    if (!text) { showTextResult("❌ Please enter some text first.", "error"); return; }

    trainTextBtn.disabled    = true;
    trainTextBtn.textContent = "Training…";
    if (trainTextResult) trainTextResult.style.display = "none";

    try {
      const res  = await fetch("/train-text", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ text })
      });
      const data = await res.json();

      if (res.ok) {
        showTextResult(
          `✅ Text trained — <strong>${data.chunks}</strong> chunk(s) added · Total: <strong>${data.total_chunks}</strong>`,
          "success"
        );
        if (manualTextArea) manualTextArea.value = "";
        setTimeout(() => location.reload(), 1600);
      } else {
        showTextResult("❌ " + (data.error || "Unknown error."), "error");
      }
    } catch {
      showTextResult("❌ Could not connect to server.", "error");
    } finally {
      trainTextBtn.disabled    = false;
      trainTextBtn.textContent = "Train Text";
    }
  });
}

// ── Chat Page ─────────────────────────────────────────────

const chatWindow    = document.getElementById("chatWindow");
const questionInput = document.getElementById("questionInput");
const sendBtn       = document.getElementById("sendBtn");

let scrollBtn = null;

// Starter suggestion chips shown at first load
const STARTER_SUGGESTIONS = [
  { icon: "📖", text: "Summarize the uploaded documents" },
  { icon: "🔍", text: "What are the key topics covered?" },
  { icon: "💡", text: "Give me 5 important facts from the docs" },
  { icon: "❓", text: "What questions can you answer?" },
];

if (chatWindow) {

  // ── Create scroll-to-bottom button ──────────────────────
  const chatWrapEl = document.querySelector(".chat-wrapper");
  if (chatWrapEl) {
    scrollBtn = document.createElement("button");
    scrollBtn.className = "scroll-btn";
    scrollBtn.innerHTML = "↓";
    scrollBtn.title = "Scroll to bottom";
    chatWrapEl.style.position = "relative";
    chatWrapEl.appendChild(scrollBtn);
    scrollBtn.addEventListener("click", () => scrollToBottom(true));
  }

  chatWindow.addEventListener("scroll", () => {
    if (!scrollBtn) return;
    const distFromBottom = chatWindow.scrollHeight - chatWindow.scrollTop - chatWindow.clientHeight;
    scrollBtn.classList.toggle("visible", distFromBottom > 120);
  });

  // ── Insert starter suggestions ───────────────────────────
  function insertStarterSuggestions() {
    const row = document.createElement("div");
    row.className = "suggestions-row";
    row.id = "starterSuggestions";

    STARTER_SUGGESTIONS.forEach((s, i) => {
      const btn = document.createElement("button");
      btn.className = "suggestion-chip";
      btn.style.animationDelay = (i * 0.07) + "s";
      btn.innerHTML = `<span class="chip-icon">${s.icon}</span>${escapeHtml(s.text)}`;
      btn.addEventListener("click", () => {
        row.remove();
        questionInput.value = s.text;
        sendQuestion();
      });
      row.appendChild(btn);
    });

    chatWindow.appendChild(row);
    scrollToBottom();
  }

  insertStarterSuggestions();

  // ── Send handler ──────────────────────────────────────────

  async function sendQuestion() {
    const question = questionInput.value.trim();
    if (!question) return;

    // Remove starter suggestions on first send
    const starterRow = document.getElementById("starterSuggestions");
    if (starterRow) starterRow.remove();

    appendUserMessage(question);
    questionInput.value  = "";
    sendBtn.disabled     = true;
    questionInput.disabled = true;

    const typingId = showTyping();

    try {
      await streamAnswer(question, typingId);
    } catch (err) {
      // Fallback to regular fetch
      try {
        removeTyping(typingId);
        const typingId2 = showTyping();
        const res  = await fetch("/ask", {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify({ question })
        });
        const data = await res.json();
        removeTyping(typingId2);

        if (data.error)    appendBotError(data.error);
        else if (data.fallback) appendFallback(data.message);
        else appendAnswer(data.answer, data.source, data.page, data.related_questions || [], data.used_web);
      } catch (err2) {
        removeTyping(typingId);
        appendBotError("Could not connect to server. Make sure backend is running.");
      }
    }

    sendBtn.disabled       = false;
    questionInput.disabled = false;
    questionInput.focus();
    scrollToBottom();
  }

  // ── Streaming answer ──────────────────────────────────────

  async function streamAnswer(question, typingId) {
    const eventSource = new EventSource(`/ask-stream?question=${encodeURIComponent(question)}`);

    return new Promise((resolve, reject) => {
      let answered = false;
      let fullText = "";
      let cardEl   = null;
      let textEl   = null;
      let wrapEl   = null;

      const timeout = setTimeout(() => {
        eventSource.close();
        if (!answered) reject(new Error("Stream timeout"));
      }, 90000);

      eventSource.onmessage = (e) => {
        const data = JSON.parse(e.data);

        if (data.done) {
          clearTimeout(timeout);
          eventSource.close();
          if (textEl) textEl.classList.remove("stream-cursor");

          // Source metadata
          if (cardEl) {
            const meta = cardEl.querySelector(".stream-meta");
            if (meta) {
              const webBadge = data.used_web ? ` <span class="web-badge">🌐 Web</span>` : "";
              const src = data.source && data.source !== "-" ? `📄 <strong>${escapeHtml(data.source)}</strong> · Page ${data.page}` : "🌐 Web + Wikipedia";
              meta.innerHTML = `<hr><small>${src}${webBadge}</small>`;
            }
          }

          // Related questions (exactly 3)
          if (wrapEl) {
            const contentEl = wrapEl.querySelector(".message-content");
            if (contentEl) {
              const related = data.related_questions || [];
              if (related.length > 0) {
                contentEl.appendChild(buildRelatedSection(related));
              }
              contentEl.appendChild(buildActionBar(fullText));
            }
          }

          answered = true;
          resolve();
          return;
        }

        if (data.token !== undefined) {
          removeTyping(typingId);

          if (!cardEl) {
            wrapEl = document.createElement("div");
            wrapEl.className = "message bot-message";
            wrapEl.innerHTML = `
              <div class="message-avatar">🤖</div>
              <div class="message-content">
                <div class="chunk-card">
                  <div class="stream-text stream-cursor"></div>
                  <div class="stream-meta"></div>
                </div>
              </div>`;
            chatWindow.appendChild(wrapEl);
            cardEl = wrapEl.querySelector(".chunk-card");
            textEl = wrapEl.querySelector(".stream-text");
            scrollToBottom();
          }

          fullText += data.token;
          textEl.innerHTML = renderMarkdown(fullText);
          scrollToBottom();
        }
      };

      eventSource.onerror = () => {
        clearTimeout(timeout);
        eventSource.close();
        if (!answered) reject(new Error("SSE error"));
      };
    });
  }

  sendBtn.addEventListener("click", sendQuestion);

  questionInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
  });

  // ── Message renderers ─────────────────────────────────────

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

  function buildRelatedSection(relatedQuestions) {
    if (!relatedQuestions || relatedQuestions.length === 0) return document.createDocumentFragment();
    const section = document.createElement("div");
    section.className = "related-section";
    const chips = relatedQuestions.slice(0, 3)
      .map(q => `<button class="related-btn" onclick="askSuggestedQuestion('${escapeHtml(q)}')">${escapeHtml(q)}</button>`)
      .join("");
    section.innerHTML = `
      <div class="related-label">💬 Ask a follow-up</div>
      <div class="related-chips">${chips}</div>`;
    return section;
  }

  function buildActionBar(answerText) {
    const bar = document.createElement("div");
    bar.className = "message-actions";
    bar.innerHTML = `
      <button class="action-btn copy-btn" title="Copy answer">📋 Copy</button>
      <button class="action-btn" title="Ask again" onclick="this.closest('.message').remove()">🔄 Retry</button>
    `;
    bar.querySelector(".copy-btn").addEventListener("click", function() {
      navigator.clipboard.writeText(answerText).then(() => {
        this.textContent = "✅ Copied!";
        this.classList.add("copied");
        setTimeout(() => { this.textContent = "📋 Copy"; this.classList.remove("copied"); }, 2000);
      });
    });
    return bar;
  }

  function appendAnswer(answer, source, page, relatedQuestions = [], usedWeb = false) {
    const webBadge = usedWeb ? `<span class="web-badge">🌐 Web</span>` : "";

    const wrap = document.createElement("div");
    wrap.className = "message bot-message";
    wrap.innerHTML = `
      <div class="message-avatar">🤖</div>
      <div class="message-content">
        <div class="chunk-card">
          ${renderMarkdown(answer)}
          <hr>
          <small>📄 <strong>${escapeHtml(source)}</strong> · Page ${page} ${webBadge}</small>
        </div>
      </div>`;

    const contentEl = wrap.querySelector(".message-content");
    if (relatedQuestions.length) contentEl.appendChild(buildRelatedSection(relatedQuestions));
    contentEl.appendChild(buildActionBar(answer));

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
        <div class="chunk-card" style="border-color:var(--danger);background:var(--danger-lt);color:var(--danger);">
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

  function scrollToBottom(force = false) {
    const distFromBottom = chatWindow.scrollHeight - chatWindow.scrollTop - chatWindow.clientHeight;
    if (force || distFromBottom < 200) {
      chatWindow.scrollTo({ top: chatWindow.scrollHeight, behavior: "smooth" });
    }
  }

  // ── Health check (status dot) ─────────────────────────────

  async function checkHealth() {
    try {
      const res  = await fetch("/health");
      const data = await res.json();
      const dot  = document.getElementById("statusDot");
      if (dot) dot.className = `status-dot ${data.ollama ? "online" : "offline"}`;
      const lbl = document.getElementById("statusLabel");
      if (lbl) lbl.textContent = data.ollama ? `${data.model} ready` : "Ollama offline";
    } catch {
      const dot = document.getElementById("statusDot");
      if (dot) dot.className = "status-dot offline";
    }
  }

  checkHealth();
  setInterval(checkHealth, 30000);
}

// ── Global: suggested question click ─────────────────────

window.askSuggestedQuestion = function(question) {
  if (!questionInput) return;
  const starterRow = document.getElementById("starterSuggestions");
  if (starterRow) starterRow.remove();
  questionInput.value = question;
  sendBtn.click();
};