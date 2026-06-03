from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import fitz  # PyMuPDF
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import json
import os
import shutil
import requests
from docx import Document
from datetime import datetime

app = FastAPI()
chat_history = []
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TFIDF_DATA_PATH = "faiss_index/chunks.json"

os.makedirs("faiss_index", exist_ok=True)
os.makedirs("uploads", exist_ok=True)


# ── helpers ──────────────────────────────────────────────

def extract_text(pdf_path: str) -> list[dict]:
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text().strip()
        if text:
            pages.append({"text": text, "page": i + 1})
    return pages

def extract_txt(path):
    with open(path, "r", encoding="utf-8") as f:
        return [{"text": f.read(), "page": 1}]

def extract_docx(path):
    doc = Document(path)
    text = "\n".join(para.text for para in doc.paragraphs)
    return [{"text": text, "page": 1}]

def chunk_text(pages: list[dict], chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    chunks = []
    for page in pages:
        text = page["text"]
        words = text.split()
        start = 0
        while start < len(words):
            end = start + chunk_size
            chunk = " ".join(words[start:end])
            chunks.append({"text": chunk, "page": page["page"]})
            start += chunk_size - overlap
    return chunks

def build_index(chunks: list[dict], filename: str):
    if os.path.exists(TFIDF_DATA_PATH):
        with open(TFIDF_DATA_PATH, "r", encoding="utf-8") as f:
            existing_chunks = json.load(f)
    else:
        existing_chunks = []

    existing_chunks = [c for c in existing_chunks if c.get("source") != filename]

    for c in chunks:
        c["source"] = filename

    all_chunks = existing_chunks + chunks

    with open(TFIDF_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f)

    return len(all_chunks)

def search_documents(query: str, top_k: int = 5):
    if not os.path.exists(TFIDF_DATA_PATH):
        return []

    with open(TFIDF_DATA_PATH, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    if not chunks:
        return []

    texts = [c["text"] for c in chunks]

    vectorizer = TfidfVectorizer(stop_words="english")
    doc_matrix = vectorizer.fit_transform(texts)
    query_vector = vectorizer.transform([query])
    scores = cosine_similarity(query_vector, doc_matrix)[0]

    top_indices = scores.argsort()[::-1][:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] > 0:  # skip zero-score chunks
            results.append({
                "text": chunks[idx]["text"],
                "page": chunks[idx]["page"],
                "source": chunks[idx]["source"],
                "score": round(float(scores[idx]), 2)
            })

    return results
def get_conversation_context():

    if not chat_history:
        return ""

    history = chat_history[-6:]  # last 6 messages

    return "\n".join(
        f"{msg['role']}: {msg['content']}"
        for msg in history
    )

def generate_answer_and_questions(question, context, conversation_history):

    prompt = f"""
You are an LMS assistant.

Use BOTH:
1. Previous conversation
2. Document context

to answer the user's question.

If the user refers to something using words like:
- it
- that
- this
- they
- those

use the conversation history to understand what they mean.

Return valid JSON only.

Example:

{{
  "answer": "Attendance requirement is 75%.",
  "related_questions": [
    "What happens if attendance is below 75%?",
    "Are medical leaves exempted?",
    "How is attendance calculated?"
  ]
}}

Conversation History:
{conversation_history}

Document Context:
{context}

Current Question:
{question}
"""

    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen2.5:1.5b",
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": 120,
                "temperature": 0.1
            }
        },
        timeout=60
    )

    text = response.json()["response"].strip()

# remove markdown fences if model adds them
    text = text.replace("```json", "")
    text = text.replace("```", "")
    text = text.strip()

    try:
        result = json.loads(text)

        return {
        "answer": result.get("answer", ""),
        "related_questions": result.get("related_questions", [])
    }

    except Exception:
        return {
        "answer": text,
        "related_questions": []
    }
# ── routes ───────────────────────────────────────────────

def get_docs():
    if os.path.exists(TFIDF_DATA_PATH):
        with open(TFIDF_DATA_PATH, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        return list({c.get("source", "unknown") for c in chunks})
    return []

@app.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
    return templates.TemplateResponse(request=request, name="train.html", context={"docs": get_docs()})

@app.get("/train", response_class=HTMLResponse)
async def train_page(request: Request):
    return templates.TemplateResponse(request=request, name="train.html", context={"docs": get_docs()})

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    docs = get_docs()
    return templates.TemplateResponse(request=request, name="chat.html", context={"doc_count": len(docs)})

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    allowed = [".pdf", ".txt", ".docx", ".md"]
    if not any(file.filename.lower().endswith(ext) for ext in allowed):
        return JSONResponse({"error": "Supported files: PDF, DOCX, TXT, MD"}, status_code=400)

    save_path = f"uploads/{file.filename}"
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        filename = file.filename.lower()
        if filename.endswith(".pdf"):
            pages = extract_text(save_path)
        elif filename.endswith(".txt") or filename.endswith(".md"):
            pages = extract_txt(save_path)
        elif filename.endswith(".docx"):
            pages = extract_docx(save_path)
        else:
            return JSONResponse({"error": "Unsupported file type"}, status_code=400)

        if not pages:
            return JSONResponse({"error": "Could not extract text from document."}, status_code=400)

        chunks = chunk_text(pages)
        total = build_index(chunks, file.filename)

        return JSONResponse({
            "message": "Training complete",
            "filename": file.filename,
            "chunks": len(chunks),
            "total_chunks": total
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/train-text")
async def train_text(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()

    if not text:
        return JSONResponse({"error": "Text is empty"}, status_code=400)

    pages = [{"text": text, "page": 1}]
    chunks = chunk_text(pages)
    source_name = f"Manual Text {datetime.now().strftime('%Y%m%d%H%M%S')}"
    total = build_index(chunks, source_name)

    return JSONResponse({
        "message": "Text trained successfully",
        "source": source_name,
        "chunks": len(chunks),
        "total_chunks": total
    })

@app.post("/ask")
async def ask_question(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()
    if not question:
        return JSONResponse({"error": "Please enter a question."}, status_code=400)
    chat_history.append({
    "role": "user",
    "content": question
})
    chat_history[:] = chat_history[-10:]
    if not os.path.exists(TFIDF_DATA_PATH):
        return JSONResponse({"error": "No documents trained yet. Please train some content first."}, status_code=400)

    results = search_documents(question, top_k=5)

    if not results:
        return JSONResponse({
            "fallback": True,
            "message": "No relevant information found in the trained documents."
        })

    top = results[0]
    context = "\n\n".join([r["text"] for r in results])

    # If score is strong enough, return chunk directly (avoids weak LLM ignoring context)
    if top["score"] >= 0.5:

        chat_history.append({
        "role": "assistant",
        "content": top["text"][:300]
    })

        chat_history[:] = chat_history[-10:]

        return JSONResponse({
        "fallback": False,
        "answer": top["text"],
        "source": top["source"],
        "page": top["page"]
    })

    # Otherwise try LLM
    try:
        result = generate_answer_and_questions(
        question,
        context,get_conversation_context()
    )

        answer = result["answer"]
        chat_history.append({
        "role": "assistant",
        "content": answer[:300]
        })

        chat_history[:] = chat_history[-10:]
  
        related_questions = result.get(
        "related_questions",
        []
    )

    except Exception:
        answer = top["text"]
        related_questions = []

    return JSONResponse({
    "fallback": False,
    "answer": answer,
    "source": results[0]["source"],
    "page": results[0]["page"],
    "related_questions": related_questions
   })

@app.delete("/reset")
async def reset():
    if os.path.exists(TFIDF_DATA_PATH):
        os.remove(TFIDF_DATA_PATH)
    if os.path.exists("uploads"):
        shutil.rmtree("uploads")
        os.makedirs("uploads", exist_ok=True)
    return JSONResponse({"message": "All documents cleared."})