from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import fitz
import json, os, shutil, requests, re, uuid, asyncio, threading, warnings
from docx import Document
from datetime import datetime
from duckduckgo_search import DDGS
import numpy as np

# ── App ───────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

os.makedirs("uploads", exist_ok=True)
os.makedirs("data",    exist_ok=True)

STORE_PATH   = "data/store.json"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:1.5b"

# ── In-memory vector store ────────────────────────────────
_store: list[dict] = []
_store_lock = threading.Lock()

def save_store():
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_store, f)

def load_store():
    global _store
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, "r", encoding="utf-8") as f:
                _store = json.load(f)
            print(f"[Store] Loaded {len(_store)} chunks from disk.")
        except Exception as e:
            print(f"[Store] Could not load store ({e}), starting fresh.")
            _store = []

load_store()

# ── Embedding model ───────────────────────────────────────
_embed_model = None
_embed_lock  = threading.Lock()

def get_embed_model():
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    with _embed_lock:
        if _embed_model is None:
            warnings.filterwarnings("ignore", category=FutureWarning)
            from sentence_transformers import SentenceTransformer
            _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
            print("[Embeddings] Model loaded.")
    return _embed_model

def embed_texts(texts: list[str]) -> list[list[float]]:
    vecs = get_embed_model().encode(texts, show_progress_bar=False, convert_to_numpy=True)
    return vecs.tolist()

def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]

# ── Startup warmup ────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    async def _warm():
        try:
            await asyncio.to_thread(get_embed_model)
            print("[Startup] Ready.")
        except Exception as e:
            print(f"[Startup] Error: {e}")
    asyncio.create_task(_warm())

# ── Text extraction ───────────────────────────────────────
def extract_pdf(path):
    doc = fitz.open(path)
    return [{"text": p.get_text().strip(), "page": i+1}
            for i, p in enumerate(doc) if p.get_text().strip()]

def extract_txt(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return [{"text": f.read(), "page": 1}]

def extract_docx(path):
    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return [{"text": text, "page": 1}]

# ── Chunking ──────────────────────────────────────────────
def chunk_text(pages, chunk_size=400, overlap=80):
    chunks = []
    for page in pages:
        sentences = re.split(r'(?<=[.!?])\s+', page["text"])
        current, cur_len = [], 0
        for sent in sentences:
            words = sent.split()
            if cur_len + len(words) > chunk_size and current:
                chunks.append({"text": " ".join(current), "page": page["page"]})
                current = (current[-overlap:] if len(current) > overlap else current[:]) + words
                cur_len = len(current)
            else:
                current.extend(words)
                cur_len += len(words)
        if current:
            chunks.append({"text": " ".join(current), "page": page["page"]})
    return chunks

# ── Vector store ops ──────────────────────────────────────
def cosine_similarity(a, b):
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0

def build_index(chunks, filename):
    print(f"[Index] Embedding {len(chunks)} chunks…")
    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(texts)
    print(f"[Index] Embeddings done. Saving to store…")
    with _store_lock:
        global _store
        _store = [e for e in _store if e["source"] != filename]
        for chunk, emb in zip(chunks, embeddings):
            _store.append({
                "id":        str(uuid.uuid4()),
                "text":      chunk["text"],
                "page":      chunk["page"],
                "source":    filename,
                "embedding": emb
            })
        save_store()
    print(f"[Index] Done. Total chunks: {len(_store)}")
    return len(_store)

def search_store(query, top_k=5):
    if not _store:
        return []
    q_emb = embed_query(query)
    scored = [(cosine_similarity(q_emb, e["embedding"]), e) for e in _store]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"text": e["text"], "page": e["page"], "source": e["source"], "score": round(s, 3)}
        for s, e in scored[:top_k] if s > 0.25
    ]

def get_docs():
    with _store_lock:
        return sorted({e["source"] for e in _store})

# ── Web / Wiki search ─────────────────────────────────────
def web_search(query):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        return "\n\n".join(f"[{r['title']}]\n{r['body']}" for r in results)
    except Exception as e:
        print("Web search error:", e)
        return ""

def wiki_search(query):
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action":"query","list":"search","srsearch":query,"format":"json"},
            headers={"User-Agent":"LMSChatBot/2.0"}, timeout=8)
        title = resp.json()["query"]["search"][0]["title"]
        return requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ','_')}",
            headers={"User-Agent":"LMSChatBot/2.0"}, timeout=8
        ).json().get("extract","")
    except Exception:
        return ""

# ── Detect if question needs current/real-time info ──────
REALTIME_KEYWORDS = [
    "current", "now", "today", "latest", "recent", "right now",
    "who is", "what is the", "cm of", "chief minister", "prime minister",
    "president of", "ceo of", "winner", "score", "match", "election",
    "2024", "2025", "this year", "news", "update", "live",
]

def needs_web(question: str, doc_score: float) -> bool:
    """Always fetch web for current-affairs questions; also when doc score is weak."""
    q_lower = question.lower()
    is_current = any(kw in q_lower for kw in REALTIME_KEYWORDS)
    return is_current or doc_score < 0.60

# ── Summarization intent detection ───────────────────────
SUMMARIZE_KEYWORDS = [
    "summarize", "summary", "summarise", "give me a summary",
    "summarize the", "summarize all", "give an overview", "overview of",
    "what is the document about", "what does the document say",
    "what is covered", "what are the main points", "main points",
    "key points", "key takeaways", "briefly explain", "brief summary",
    "explain the document", "explain this document", "tldr", "tl;dr",
]

TOPIC_SUMMARIZE_PATTERNS = [
    r"summarize\s+(?:the\s+)?(?:section\s+on\s+|part\s+on\s+|topic\s+of\s+)?(.+)",
    r"summary\s+(?:of|on|about)\s+(.+)",
    r"summarise\s+(?:the\s+)?(.+)",
    r"what\s+does\s+(?:the\s+)?doc(?:ument)?\s+say\s+about\s+(.+)",
    r"tell\s+me\s+about\s+(.+?)\s+(?:in|from)\s+the\s+doc",
    r"explain\s+(.+?)\s+(?:in|from)\s+the\s+doc",
]

def detect_summarize_intent(question: str):
    """Returns (is_summary, topic_or_None, specific_doc_or_None)."""
    q = question.lower().strip()

    # Check for specific doc: "summarize report.pdf"
    doc_match = re.search(r'summarize\s+([\w\-\.]+\.(pdf|docx|txt|md))', q)
    if doc_match:
        return True, None, doc_match.group(1)

    # Check for topic-specific: "summarize neural networks", "summary of chapter 2"
    for pat in TOPIC_SUMMARIZE_PATTERNS:
        m = re.search(pat, q)
        if m:
            topic = m.group(1).strip()
            topic = re.sub(r'\b(the|a|an|this|that|these|those|my|our|all|documents?|doc)\b', '', topic).strip()
            if topic and len(topic) > 2:
                return True, topic, None

    # General summarize keyword
    if any(kw in q for kw in SUMMARIZE_KEYWORDS):
        return True, None, None

    return False, None, None

def get_summary_chunks(topic=None, specific_doc=None, top_k=30):
    """Pull chunks for summarization — all content, filtered/ranked as needed."""
    with _store_lock:
        pool = list(_store)

    if not pool:
        return []

    if specific_doc:
        pool = [e for e in pool if specific_doc.lower() in e["source"].lower()]

    if not pool:
        return []

    if topic:
        # Re-rank by semantic similarity to the topic
        q_emb = embed_query(topic)
        scored = [(cosine_similarity(q_emb, e["embedding"]), e) for e in pool]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    # No topic — evenly sample to cover whole document breadth
    if len(pool) <= top_k:
        return pool
    step = len(pool) / top_k
    return [pool[int(i * step)] for i in range(top_k)]

def build_summary_prompt(question, chunks, topic=None, specific_doc=None):
    today = datetime.now().strftime("%A, %d %B %Y")
    sources = sorted({c["source"] for c in chunks})
    source_label = specific_doc or (", ".join(sources) if sources else "all documents")

    content = "\n\n---\n\n".join(c["text"] for c in chunks)
    words = content.split()
    if len(words) > 3000:
        content = " ".join(words[:3000]) + " [content continues...]"

    if topic:
        task = f'Summarize everything the document says about: "{topic}"'
        structure = f"""Include:
- What the document covers regarding "{topic}"
- Key facts, definitions, or data points
- Any conclusions or implications"""
    else:
        task = f"Provide a comprehensive summary of the document(s): {source_label}"
        structure = """Include:
- A clear overview of what the document is about (2-3 sentences)
- The main topics and themes covered
- Key facts, findings, or important details (at least 5 points)
- Any conclusions, recommendations, or notable takeaways"""

    return f"""You are an expert document analyst. Today is {today}.

TASK: {task}

{structure}

Write in clear, well-organized prose. Be thorough. Do not cut short.

DOCUMENT CONTENT:
{content}

SUMMARY:"""

# ── Chat history ──────────────────────────────────────────
chat_history = []

def get_conv_context():
    return "\n".join(f"{m['role'].upper()}: {m['content']}" for m in chat_history[-6:])

# ── Ollama ────────────────────────────────────────────────
def call_ollama(prompt, max_tokens=500):
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0.15,
                        "top_p": 0.9, "repeat_penalty": 1.1}
        }, timeout=90)
        return resp.json().get("response", "").strip()
    except Exception as e:
        print("Ollama error:", e)
        return ""

def call_ollama_stream(prompt):
    try:
        with requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": True,
            "options": {"num_predict": 700, "temperature": 0.15}
        }, stream=True, timeout=90) as resp:
            for line in resp.iter_lines():
                if line:
                    chunk = json.loads(line)
                    yield chunk.get("response", "")
                    if chunk.get("done", False):
                        break
    except Exception as e:
        yield f"[Error: {e}]"

def build_prompt(question, context, conv_history):
    today = datetime.now().strftime("%A, %d %B %Y")
    return f"""You are an intelligent assistant. Today's date is {today}.

STRICT RULES:
1. Use the KNOWLEDGE section below as your PRIMARY source of truth.
2. Web Search and Wikipedia results are LIVE and UP-TO-DATE — always prefer them for current facts (people, positions, scores, news).
3. NEVER answer from your own training memory for questions about people, positions, or current events — the training data is outdated.
4. Be factual, concise, and direct.
5. Return ONLY valid JSON — no extra text, no markdown fences.

JSON FORMAT (exactly this structure):
{{
  "answer": "your detailed answer here",
  "related_questions": ["question 1?", "question 2?", "question 3?"]
}}

CONVERSATION HISTORY:
{conv_history}

KNOWLEDGE:
{context}

QUESTION: {question}

JSON:"""

def generate_answer(question, context, conv_history):
    prompt = build_prompt(question, context, conv_history)
    raw    = call_ollama(prompt, max_tokens=500)
    raw    = re.sub(r"```json|```", "", raw).strip()

    # Try to extract JSON object
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            answer   = result.get("answer", "").strip()
            related  = result.get("related_questions", [])
            if isinstance(related, list):
                related = [q for q in related if isinstance(q, str) and q.strip()][:3]
            if answer:
                return {"answer": answer, "related_questions": related}
        except Exception:
            pass

    # Fallback: if LLM returned plain text instead of JSON
    return {"answer": raw or "I couldn't generate an answer.", "related_questions": []}

def build_stream_prompt(question, context):
    today = datetime.now().strftime("%A, %d %B %Y")
    return f"""You are an intelligent assistant. Today's date is {today}.

INSTRUCTIONS:
- Use the KNOWLEDGE below as your primary source. Web Search results are live and current — prefer them for recent facts.
- NEVER answer from your own old training data for current events, people's positions, or scores.
- Be direct, factual and clear. Give a complete answer — use as much detail as needed.

KNOWLEDGE:
{context}

QUESTION: {question}

ANSWER:"""

def generate_related_questions(question, answer):
    """Ask the LLM to produce exactly 3 follow-up questions after the answer is ready."""
    prompt = f"""Given this Q&A, suggest exactly 3 short follow-up questions a user might ask next.
Return ONLY a JSON array of 3 strings, no extra text.
Example: ["What is X?", "How does Y work?", "When did Z happen?"]

QUESTION: {question}
ANSWER: {answer[:300]}

JSON array:"""
    raw = call_ollama(prompt, max_tokens=150)
    raw = re.sub(r"```json|```", "", raw).strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if match:
        try:
            qs = json.loads(match.group())
            if isinstance(qs, list):
                return [q for q in qs if isinstance(q, str) and q.strip()][:3]
        except Exception:
            pass
    return []

# ── Routes ────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="train.html", context={"docs": get_docs()})

@app.get("/train", response_class=HTMLResponse)
async def train_page(request: Request):
    return templates.TemplateResponse(request=request, name="train.html", context={"docs": get_docs()})

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request=request, name="chat.html", context={"doc_count": len(get_docs())})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    allowed = [".pdf", ".docx", ".txt", ".md"]
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in allowed:
        return JSONResponse({"error": "Supported: PDF, DOCX, TXT, MD"}, status_code=400)

    save_path = f"uploads/{file.filename}"
    raw = await file.read()
    with open(save_path, "wb") as f:
        f.write(raw)

    def process():
        print(f"[Upload] Extracting '{file.filename}'…")
        if ext == ".pdf":           pages = extract_pdf(save_path)
        elif ext in (".txt",".md"): pages = extract_txt(save_path)
        elif ext == ".docx":        pages = extract_docx(save_path)
        else: raise ValueError("Unsupported")
        if not pages:
            raise ValueError("No text extracted.")
        chunks = chunk_text(pages)
        total  = build_index(chunks, file.filename)
        return len(chunks), total

    try:
        num_chunks, total = await asyncio.to_thread(process)
        return JSONResponse({"message": "Training complete", "filename": file.filename,
                             "chunks": num_chunks, "total_chunks": total})
    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/train-text")
async def train_text(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "Text is empty"}, status_code=400)

    def process():
        pages  = [{"text": text, "page": 1}]
        chunks = chunk_text(pages)
        source = f"Manual-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        total  = build_index(chunks, source)
        return chunks, source, total

    chunks, source, total = await asyncio.to_thread(process)
    return JSONResponse({"message": "Trained successfully", "source": source,
                         "chunks": len(chunks), "total_chunks": total})

@app.post("/ask")
async def ask(request: Request):
    body     = await request.json()
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse({"error": "Empty question."}, status_code=400)

    chat_history.append({"role": "user", "content": question})
    chat_history[:] = chat_history[-12:]

    # ── Summarization fast-path ───────────────────────────
    is_summary, topic, specific_doc = detect_summarize_intent(question)
    if is_summary:
        chunks = await asyncio.to_thread(get_summary_chunks, topic, specific_doc, 30)
        if not chunks:
            return JSONResponse({"fallback": True,
                                 "message": "No documents found. Please train documents first."})
        prompt  = build_summary_prompt(question, chunks, topic, specific_doc)
        sources = sorted({c["source"] for c in chunks})

        def _run():
            return call_ollama(prompt, max_tokens=1200)
        summary = await asyncio.to_thread(_run)
        related = await asyncio.to_thread(generate_related_questions, question, summary)
        chat_history.append({"role": "assistant", "content": summary[:600]})
        return JSONResponse({
            "fallback": False, "answer": summary,
            "source": sources[0] if sources else "-",
            "page": chunks[0]["page"] if chunks else "-",
            "used_web": False, "doc_score": 1.0,
            "related_questions": related[:3]
        })
    # ── End summarization fast-path ───────────────────────

    def get_results():
        doc_results = search_store(question, top_k=5)
        top_score   = doc_results[0]["score"] if doc_results else 0
        fetch_web   = needs_web(question, top_score)
        wiki_ctx    = wiki_search(question) if fetch_web else ""
        web_ctx     = web_search(question)  if fetch_web else ""
        return doc_results, wiki_ctx, web_ctx, fetch_web

    doc_results, wiki_ctx, web_ctx, fetch_web = await asyncio.to_thread(get_results)

    # Build context — web first so LLM sees fresh info early
    context = ""
    if web_ctx:  context += f"[WEB SEARCH — CURRENT & LIVE]\n{web_ctx}\n\n"
    if wiki_ctx: context += f"[WIKIPEDIA]\n{wiki_ctx}\n\n"
    doc_ctx = "\n\n---\n\n".join(r["text"] for r in doc_results)
    if doc_ctx:  context += f"[YOUR DOCUMENTS]\n{doc_ctx}\n\n"

    if not context.strip():
        return JSONResponse({"fallback": True,
                             "message": "No relevant info found. Train more documents or rephrase."})

    result  = await asyncio.to_thread(generate_answer, question, context, get_conv_context())
    answer  = result["answer"]
    related = result.get("related_questions", [])

    # If LLM didn't return related questions, generate separately
    if len(related) < 3:
        related = await asyncio.to_thread(generate_related_questions, question, answer)

    source = doc_results[0]["source"] if doc_results else "Web + Wikipedia"
    page   = doc_results[0]["page"]   if doc_results else "-"

    chat_history.append({"role": "assistant", "content": answer[:400]})
    chat_history[:] = chat_history[-12:]

    return JSONResponse({
        "fallback": False, "answer": answer, "source": source,
        "page": page, "used_web": bool(wiki_ctx or web_ctx),
        "doc_score": doc_results[0]["score"] if doc_results else 0,
        "related_questions": related[:3]
    })

@app.get("/ask-stream")
async def ask_stream(request: Request, question: str = ""):
    if not question:
        return JSONResponse({"error": "Empty"}, status_code=400)

    # ── Summarization fast-path ───────────────────────────
    is_summary, topic, specific_doc = detect_summarize_intent(question)
    if is_summary:
        chunks = await asyncio.to_thread(get_summary_chunks, topic, specific_doc, 30)
        if not chunks:
            async def no_docs_stream():
                msg = "No documents have been trained yet. Please upload documents on the Train page first."
                yield f"data: {json.dumps({'token': msg})}\n\n"
                yield f"data: {json.dumps({'done': True, 'source': '-', 'page': '-', 'related_questions': [], 'used_web': False})}\n\n"
            return StreamingResponse(no_docs_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        prompt  = build_summary_prompt(question, chunks, topic, specific_doc)
        sources = sorted({c["source"] for c in chunks})
        source  = sources[0] if sources else "-"
        page    = chunks[0]["page"] if chunks else "-"

        async def summary_stream():
            full = ""
            queue = asyncio.Queue()
            loop  = asyncio.get_event_loop()
            stop  = asyncio.Event()

            def run():
                try:
                    # Higher token limit for summaries
                    with requests.post(OLLAMA_URL, json={
                        "model": OLLAMA_MODEL, "prompt": prompt, "stream": True,
                        "options": {"num_predict": 1200, "temperature": 0.2,
                                    "top_p": 0.9, "repeat_penalty": 1.1}
                    }, stream=True, timeout=180) as resp:
                        for line in resp.iter_lines():
                            if line and not stop.is_set():
                                c = json.loads(line)
                                loop.call_soon_threadsafe(queue.put_nowait, ("token", c.get("response", "")))
                                if c.get("done", False):
                                    break
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
                except Exception as e:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

            threading.Thread(target=run, daemon=True).start()

            while True:
                if await request.is_disconnected():
                    stop.set(); break
                try:
                    kind, val = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if kind == "token":
                    full += val
                    yield f"data: {json.dumps({'token': val})}\n\n"
                elif kind == "done":
                    break
                elif kind == "error":
                    yield f"data: {json.dumps({'token': f'[Error: {val}]'})}\n\n"
                    break

            related = await asyncio.to_thread(generate_related_questions, question, full)
            yield f"data: {json.dumps({'done': True, 'source': source, 'page': page, 'related_questions': related[:3], 'used_web': False})}\n\n"
            chat_history.append({"role": "assistant", "content": full[:600]})

        return StreamingResponse(summary_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # ── End summarization fast-path ───────────────────────

    def get_results():
        doc_results = search_store(question, top_k=5)
        top_score   = doc_results[0]["score"] if doc_results else 0
        fetch_web   = needs_web(question, top_score)
        wiki_ctx    = wiki_search(question) if fetch_web else ""
        web_ctx     = web_search(question)  if fetch_web else ""
        return doc_results, wiki_ctx, web_ctx, fetch_web

    doc_results, wiki_ctx, web_ctx, fetch_web = await asyncio.to_thread(get_results)

    # Build context — web first
    context = ""
    if web_ctx:  context += f"[WEB SEARCH — CURRENT & LIVE]\n{web_ctx}\n\n"
    if wiki_ctx: context += f"[WIKIPEDIA]\n{wiki_ctx}\n\n"
    doc_ctx = "\n\n---\n\n".join(r["text"] for r in doc_results)
    if doc_ctx:  context += f"[YOUR DOCUMENTS]\n{doc_ctx}\n\n"

    # For meta questions about what the bot can do, always answer helpfully
    q_lower = question.lower()
    is_meta = any(kw in q_lower for kw in [
        "what can you", "what questions", "key topics", "important facts",
        "summarize", "summary", "topics covered", "what do you know"
    ])

    if not context.strip() and not is_meta:
        async def empty_stream():
            yield f"data: {json.dumps({'token': 'No relevant information found. Please train more documents or rephrase.'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'source': '-', 'page': '-', 'related_questions': []})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    if not context.strip() and is_meta:
        context = "[NO DOCUMENTS TRAINED YET]\nThe user has not uploaded any documents yet.\n\n"

    prompt = build_stream_prompt(question, context)
    source = doc_results[0]["source"] if doc_results else "Web + Wikipedia"
    page   = doc_results[0]["page"]   if doc_results else "-"

    async def stream_gen():
        full = ""
        stop_event = asyncio.Event()

        def _blocking_stream():
            nonlocal full
            for token in call_ollama_stream(prompt):
                if stop_event.is_set():
                    break
                full += token
                yield token

        loop = asyncio.get_event_loop()

        # Use a queue to bridge the sync generator and async generator
        queue = asyncio.Queue()

        def run_in_thread():
            try:
                for token in call_ollama_stream(prompt):
                    if stop_event.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("token", token))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()

        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    stop_event.set()
                    break

                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if kind == "token":
                    full += value
                    yield f"data: {json.dumps({'token': value})}\n\n"
                elif kind == "done":
                    break
                elif kind == "error":
                    yield f"data: {json.dumps({'token': f'[Error: {value}]'})}\n\n"
                    break
        except asyncio.CancelledError:
            stop_event.set()
            return

        if not stop_event.is_set() and full:
            related = await asyncio.to_thread(generate_related_questions, question, full)
            yield f"data: {json.dumps({'done': True, 'source': source, 'page': page, 'related_questions': related[:3], 'used_web': bool(wiki_ctx or web_ctx)})}\n\n"
            chat_history.append({"role": "assistant", "content": full[:400]})
        else:
            yield f"data: {json.dumps({'done': True, 'stopped': True, 'source': source, 'page': page, 'related_questions': [], 'used_web': False})}\n\n"

    return StreamingResponse(stream_gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.post("/summarize")
async def summarize_doc(request: Request):
    body     = await request.json()
    filename = body.get("filename", "").strip()

    with _store_lock:
        if filename:
            chunks = [e["text"] for e in _store if e["source"] == filename]
            label  = filename
        else:
            chunks = [e["text"] for e in _store]
            label  = "all documents"

    if not chunks:
        return JSONResponse({"error": "No content found to summarize."}, status_code=404)

    # Take up to ~2000 words of content for summarization
    combined = "\n\n".join(chunks)
    words    = combined.split()
    if len(words) > 2000:
        combined = " ".join(words[:2000]) + "..."

    prompt = f"""You are an expert summarizer. Summarize the following document content clearly and concisely.

Include:
1. A 2-3 sentence overview
2. Key topics and themes (as a bullet list)
3. Important facts or findings (up to 5)
4. Any notable conclusions

DOCUMENT: {label}

CONTENT:
{combined}

SUMMARY:"""

    def _run():
        return call_ollama(prompt, max_tokens=600)

    summary = await asyncio.to_thread(_run)
    if not summary:
        return JSONResponse({"error": "Could not generate summary."}, status_code=500)

    return JSONResponse({"summary": summary, "label": label, "chunk_count": len(chunks)})

@app.delete("/reset")
async def reset():
    def _do():
        with _store_lock:
            global _store
            _store = []
            if os.path.exists(STORE_PATH):
                os.remove(STORE_PATH)
        if os.path.exists("uploads"):
            shutil.rmtree("uploads")
        os.makedirs("uploads", exist_ok=True)
        chat_history.clear()
    await asyncio.to_thread(_do)
    return JSONResponse({"message": "All documents and chat history cleared."})

@app.delete("/delete-doc")
async def delete_doc(request: Request):
    body     = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        return JSONResponse({"error": "No filename provided."}, status_code=400)
    with _store_lock:
        global _store
        before = len(_store)
        _store = [e for e in _store if e["source"] != filename]
        removed = before - len(_store)
        save_store()
    # Remove uploaded file if present
    upload_path = f"uploads/{filename}"
    if os.path.exists(upload_path):
        os.remove(upload_path)
    return JSONResponse({"message": f"Removed {removed} chunks for '{filename}'.", "removed": removed})

@app.get("/health")
async def health():
    try:
        requests.get("http://localhost:11434", timeout=3)
        ollama_ok = True
    except Exception:
        ollama_ok = False
    return JSONResponse({"chunks": len(_store), "ollama": ollama_ok,
                         "model": OLLAMA_MODEL, "docs": get_docs()})