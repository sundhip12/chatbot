from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import fitz
import json, os, shutil, requests, re, uuid, asyncio, threading, warnings, hashlib, math
from docx import Document
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import numpy as np

# ── App ───────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

os.makedirs("uploads", exist_ok=True)
os.makedirs("data",    exist_ok=True)

STORE_PATH   = "data/store.json"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:latest"

# ── SearXNG config ────────────────────────────────────────
# Set SEARXNG_URL env var to your instance, e.g. http://localhost:8888
# Falls back to the public instance (rate-limited) if not set.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "https://searx.be")

# ── Answer cache ──────────────────────────────────────────
_answer_cache: dict[str, dict] = {}   # sha256(question) -> {answer, related, source, page, used_web, ts}
CACHE_MAX_AGE = 300   # seconds — web answers expire after 5 min; doc answers stay longer

def _cache_key(question: str) -> str:
    return hashlib.sha256(question.strip().lower().encode()).hexdigest()

def cache_get(question: str) -> dict | None:
    key = _cache_key(question)
    entry = _answer_cache.get(key)
    if not entry:
        return None
    age = (datetime.now() - entry["ts"]).total_seconds()
    max_age = CACHE_MAX_AGE if entry.get("used_web") else 3600
    if age > max_age:
        del _answer_cache[key]
        return None
    return entry

def cache_set(question: str, data: dict):
    key = _cache_key(question)
    _answer_cache[key] = {**data, "ts": datetime.now()}

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

# ── BM25 helpers ──────────────────────────────────────────
def _tokenize(text: str) -> list[str]:
    return re.findall(r'\w+', text.lower())

def bm25_scores(query: str, corpus: list[str], k1=1.5, b=0.75) -> list[float]:
    """Return BM25 score for each doc in corpus against query."""
    tokenized = [_tokenize(d) for d in corpus]
    N = len(tokenized)
    avgdl = sum(len(d) for d in tokenized) / max(N, 1)
    query_terms = _tokenize(query)

    # IDF for each query term
    idf: dict[str, float] = {}
    for term in set(query_terms):
        df = sum(1 for d in tokenized if term in d)
        idf[term] = math.log((N - df + 0.5) / (df + 0.5) + 1)

    scores = []
    for doc_tokens in tokenized:
        tf_map = Counter(doc_tokens)
        dl = len(doc_tokens)
        score = 0.0
        for term in query_terms:
            tf = tf_map.get(term, 0)
            num = tf * (k1 + 1)
            den = tf + k1 * (1 - b + b * dl / max(avgdl, 1))
            score += idf.get(term, 0) * num / max(den, 1e-9)
        scores.append(score)
    return scores

# ── Hybrid search (BM25 + semantic) ──────────────────────
def search_store(query: str, top_k: int = 6) -> list[dict]:
    """Hybrid retrieval: 50% semantic + 50% BM25, re-ranked by combined score."""
    if not _store:
        return []

    texts  = [e["text"] for e in _store]
    q_emb  = embed_query(query)

    # Semantic scores
    sem_scores = [cosine_similarity(q_emb, e["embedding"]) for e in _store]

    # BM25 scores (normalised 0–1)
    bm25_raw = bm25_scores(query, texts)
    bm25_max = max(bm25_raw) if bm25_raw else 1.0
    bm25_norm = [s / max(bm25_max, 1e-9) for s in bm25_raw]

    # Combined
    combined = [0.5 * s + 0.5 * b for s, b in zip(sem_scores, bm25_norm)]

    scored = sorted(zip(combined, sem_scores, _store), key=lambda x: x[0], reverse=True)

    return [
        {
            "text":   e["text"],
            "page":   e["page"],
            "source": e["source"],
            "score":  round(s, 3),
            "sem":    round(sem, 3),
        }
        for s, sem, e in scored[:top_k]
        if s > 0.15   # lower threshold — BM25 boosts sparse matches
    ]

def get_docs():
    with _store_lock:
        return sorted({e["source"] for e in _store})

# ── SearXNG web search ────────────────────────────────────
def searxng_search(query: str, num: int = 6) -> list[dict]:
    """
    Query SearXNG JSON API.
    Returns list of {title, url, content} dicts.
    """
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={
                "q":       query,
                "format":  "json",
                "engines": "google,bing,duckduckgo",
                "language":"en",
                "num":     num,
            },
            headers={"User-Agent": "LMSChatBot/3.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:num]
        return [
            {
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "content": r.get("content", "") or r.get("body", ""),
            }
            for r in results
            if r.get("content") or r.get("body")
        ]
    except Exception as e:
        print(f"[SearXNG] Error: {e}")
        return []

def format_web_results(results: list[dict]) -> str:
    """Convert SearXNG results to a rich context string with sources."""
    if not results:
        return ""
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"[Source {i}: {r['title']}]\n"
            f"URL: {r['url']}\n"
            f"{r['content']}"
        )
    return "\n\n".join(parts)

def wiki_search(query: str) -> str:
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query, "format": "json"},
            headers={"User-Agent": "LMSChatBot/3.0"}, timeout=8,
        )
        title = resp.json()["query"]["search"][0]["title"]
        return requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
            headers={"User-Agent": "LMSChatBot/3.0"}, timeout=8,
        ).json().get("extract", "")
    except Exception:
        return ""

# ── Parallel web + wiki fetch ─────────────────────────────
def fetch_web_context(query: str) -> tuple[list[dict], str]:
    """Run SearXNG + Wikipedia in parallel and return (web_results, wiki_text)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        web_fut  = ex.submit(searxng_search, query)
        wiki_fut = ex.submit(wiki_search, query)
    return web_fut.result(), wiki_fut.result()

# ── Detect if question needs web ──────────────────────────
REALTIME_KEYWORDS = [
    "current", "now", "today", "latest", "recent", "right now",
    "who is", "what is the", "cm of", "chief minister", "prime minister",
    "president of", "ceo of", "winner", "score", "match", "election",
    "2024", "2025", "2026", "this year", "news", "update", "live",
    "price of", "how much does", "where is", "when is",
]

def needs_web(question: str, doc_score: float) -> bool:
    q_lower = question.lower()
    is_current = any(kw in q_lower for kw in REALTIME_KEYWORDS)
    return is_current or doc_score < 0.55

# ── Chat history ──────────────────────────────────────────
chat_history = []

def get_conv_context() -> str:
    return "\n".join(f"{m['role'].upper()}: {m['content']}" for m in chat_history[-6:])

# ── Ollama calls ──────────────────────────────────────────
def call_ollama(prompt: str, max_tokens: int = 500) -> str:
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

def call_ollama_stream(prompt: str):
    try:
        with requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": True,
            "options": {"num_predict": 600, "temperature": 0.15}
        }, stream=True, timeout=90) as resp:
            for line in resp.iter_lines():
                if line:
                    chunk = json.loads(line)
                    yield chunk.get("response", "")
                    if chunk.get("done", False):
                        break
    except Exception as e:
        yield f"[Error: {e}]"

# ── Prompt builders ───────────────────────────────────────
def build_prompt(question: str, context: str, conv_history: str) -> str:
    today = datetime.now().strftime("%A, %d %B %Y")
    return f"""You are an intelligent assistant. Today's date is {today}.

STRICT RULES:
1. Use the KNOWLEDGE section below as your PRIMARY source of truth.
2. [WEB SEARCH] results are live and current — ALWAYS prefer them for facts about people, prices, events, or anything time-sensitive.
3. NEVER answer from your own training memory for current events, positions, or scores — your training data is stale.
4. If the answer comes from a web source, cite it naturally (e.g. "According to [title]…").
5. Be factual, clear, and concise. Format with Markdown (headers, bullet lists, bold) when it aids readability.
6. Return ONLY valid JSON — no extra text, no markdown fences.

JSON FORMAT:
{{
  "answer": "your detailed markdown-formatted answer",
  "related_questions": ["question 1?", "question 2?", "question 3?"],
  "web_sources": ["url1", "url2"]
}}

CONVERSATION HISTORY:
{conv_history}

KNOWLEDGE:
{context}

QUESTION: {question}

JSON:"""

def build_stream_prompt(question: str, context: str) -> str:
    today = datetime.now().strftime("%A, %d %B %Y")
    return f"""You are an intelligent assistant. Today's date is {today}.

INSTRUCTIONS:
- Use the KNOWLEDGE below as your primary source.
- [WEB SEARCH] results are LIVE — always prefer them for current facts (people, news, scores, prices).
- NEVER use your own outdated training data for current events.
- If citing a web result, mention the source title naturally.
- Format your answer with Markdown (use **bold**, bullet lists, and code blocks where helpful).
- Answer thoroughly but concisely.

KNOWLEDGE:
{context}

QUESTION: {question}

ANSWER:"""

def generate_answer(question: str, context: str, conv_history: str) -> dict:
    prompt = build_prompt(question, context, conv_history)
    raw    = call_ollama(prompt, max_tokens=600)
    raw    = re.sub(r"```json|```", "", raw).strip()

    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            answer  = result.get("answer", "").strip()
            related = result.get("related_questions", [])
            sources = result.get("web_sources", [])
            if isinstance(related, list):
                related = [q for q in related if isinstance(q, str) and q.strip()][:3]
            if answer:
                return {"answer": answer, "related_questions": related, "web_sources": sources}
        except Exception:
            pass

    return {"answer": raw or "I couldn't generate an answer.", "related_questions": [], "web_sources": []}

def generate_related_questions(question: str, answer: str) -> list[str]:
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

# ── Context builder ───────────────────────────────────────
def build_context(web_results: list[dict], wiki_ctx: str, doc_results: list[dict]) -> str:
    context = ""
    if web_results:
        context += f"[WEB SEARCH — LIVE & CURRENT]\n{format_web_results(web_results)}\n\n"
    if wiki_ctx:
        context += f"[WIKIPEDIA]\n{wiki_ctx}\n\n"
    doc_ctx = "\n\n---\n\n".join(r["text"] for r in doc_results)
    if doc_ctx:
        context += f"[YOUR DOCUMENTS]\n{doc_ctx}\n\n"
    return context

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

    # Check cache
    cached = cache_get(question)
    if cached:
        return JSONResponse({**cached, "cached": True})

    chat_history.append({"role": "user", "content": question})
    chat_history[:] = chat_history[-12:]

    def get_results():
        doc_results = search_store(question, top_k=6)
        top_score   = doc_results[0]["score"] if doc_results else 0
        fetch_web   = needs_web(question, top_score)
        web_results, wiki_ctx = fetch_web_context(question) if fetch_web else ([], "")
        return doc_results, web_results, wiki_ctx, fetch_web

    doc_results, web_results, wiki_ctx, fetch_web = await asyncio.to_thread(get_results)

    context = build_context(web_results, wiki_ctx, doc_results)

    if not context.strip():
        return JSONResponse({"fallback": True,
                             "message": "No relevant info found. Train more documents or rephrase."})

    result  = await asyncio.to_thread(generate_answer, question, context, get_conv_context())
    answer  = result["answer"]
    related = result.get("related_questions", [])
    web_sources = result.get("web_sources", []) or [r["url"] for r in web_results[:3]]

    if len(related) < 3:
        related = await asyncio.to_thread(generate_related_questions, question, answer)

    source = doc_results[0]["source"] if doc_results else "Web"
    page   = doc_results[0]["page"]   if doc_results else "-"
    used_web = bool(web_results or wiki_ctx)

    chat_history.append({"role": "assistant", "content": answer[:400]})
    chat_history[:] = chat_history[-12:]

    response_data = {
        "fallback": False, "answer": answer, "source": source,
        "page": page, "used_web": used_web,
        "doc_score": doc_results[0]["score"] if doc_results else 0,
        "related_questions": related[:3],
        "web_sources": web_sources[:3],
        "web_titles": [r["title"] for r in web_results[:3]],
    }
    cache_set(question, response_data)
    return JSONResponse(response_data)

@app.get("/ask-stream")
async def ask_stream(request: Request, question: str = ""):
    if not question:
        return JSONResponse({"error": "Empty"}, status_code=400)

    def get_results():
        doc_results = search_store(question, top_k=6)
        top_score   = doc_results[0]["score"] if doc_results else 0
        fetch_web   = needs_web(question, top_score)
        web_results, wiki_ctx = fetch_web_context(question) if fetch_web else ([], "")
        return doc_results, web_results, wiki_ctx

    doc_results, web_results, wiki_ctx = await asyncio.to_thread(get_results)

    context = build_context(web_results, wiki_ctx, doc_results)

    if not context.strip():
        async def empty_stream():
            yield f"data: {json.dumps({'token': 'No relevant information found. Please train more documents or rephrase.'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'source': '-', 'page': '-', 'related_questions': [], 'web_sources': [], 'web_titles': []})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    prompt = build_stream_prompt(question, context)
    source = doc_results[0]["source"] if doc_results else "Web"
    page   = doc_results[0]["page"]   if doc_results else "-"
    used_web = bool(web_results or wiki_ctx)
    web_sources = [r["url"] for r in web_results[:3]]
    web_titles  = [r["title"] for r in web_results[:3]]

    def stream_gen():
        full = ""
        for token in call_ollama_stream(prompt):
            full += token
            yield f"data: {json.dumps({'token': token})}\n\n"

        related = generate_related_questions(question, full)

        yield f"data: {json.dumps({'done': True, 'source': source, 'page': page, 'related_questions': related[:3], 'used_web': used_web, 'web_sources': web_sources, 'web_titles': web_titles})}\n\n"
        chat_history.append({"role": "assistant", "content": full[:400]})

        # Cache the streamed answer too
        cache_set(question, {
            "fallback": False, "answer": full, "source": source,
            "page": page, "used_web": used_web,
            "doc_score": doc_results[0]["score"] if doc_results else 0,
            "related_questions": related[:3],
            "web_sources": web_sources,
            "web_titles": web_titles,
        })

    return StreamingResponse(stream_gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.delete("/reset")
async def reset():
    def _do():
        global _store
        with _store_lock:
            _store = []
            if os.path.exists(STORE_PATH):
                os.remove(STORE_PATH)
        if os.path.exists("uploads"):
            shutil.rmtree("uploads")
        os.makedirs("uploads", exist_ok=True)
        chat_history.clear()
        _answer_cache.clear()
    await asyncio.to_thread(_do)
    return JSONResponse({"message": "All documents and chat history cleared."})

@app.delete("/delete-doc")
async def delete_doc(request: Request):
    body     = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        return JSONResponse({"error": "No filename provided."}, status_code=400)
    global _store
    with _store_lock:
        before = len(_store)
        _store = [e for e in _store if e["source"] != filename]
        removed = before - len(_store)
        save_store()
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
    # Quick SearXNG ping
    try:
        requests.get(f"{SEARXNG_URL}/", timeout=3)
        searxng_ok = True
    except Exception:
        searxng_ok = False
    return JSONResponse({
        "chunks": len(_store), "ollama": ollama_ok,
        "model": OLLAMA_MODEL, "docs": get_docs(),
        "searxng": searxng_ok, "searxng_url": SEARXNG_URL,
        "cache_entries": len(_answer_cache),
    })