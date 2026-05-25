"""
Karri Prasad – Voice AI Agent Backend
FastAPI + LangChain + Groq + FAISS RAG + SQLite History
"""

import os
import io
import base64
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import List
from gtts import gTTS

# LangChain
# pyrefly: ignore [missing-import]
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

# SQLite
import sqlite3

# ─── DB Setup ───────────────────────────────────────────────────────────────
DB_PATH = "sessions.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_ip TEXT,
            user_message TEXT,
            ai_response TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def log_chat(user_ip, user_message, ai_response):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO chat_logs (user_ip, user_message, ai_response) VALUES (?, ?, ?)',
              (user_ip, user_message, ai_response))
    conn.commit()
    conn.close()

# ─── RAG Setup ──────────────────────────────────────────────────────────────
# Load Knowledge Base
try:
    with open("knowledge.txt", "r") as f:
        knowledge_text = f.read()
except FileNotFoundError:
    knowledge_text = "Karri Prasad is an AI Engineer."

text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
docs = text_splitter.create_documents([knowledge_text])

embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2", cache_folder="./.cache")
vectorstore = FAISS.from_documents(docs, embeddings)
retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

# ─── LangChain Setup ────────────────────────────────────────────────────────
llm = ChatGroq(model_name="llama-3.3-70b-versatile", temperature=0.7)

system_prompt = (
    "You are Karri Prasad's AI voice avatar on his portfolio website. "
    "You ARE Karri — speak in first person, warmly and naturally, like a real human conversation. "
    "Be friendly, a little casual, engaging, and confident. Keep responses concise (2-4 sentences max) "
    "unless the recruiter asks for detail. "
    "Use the following retrieved context about your background to answer the user's questions. "
    "If the answer isn't in the context, just answer naturally as Karri but don't hallucinate skills you don't have.\n\n"
    "Context: {context}"
)

prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    MessagesPlaceholder(variable_name="chat_history"),
    ("human", "{input}"),
])

document_chain = create_stuff_documents_chain(llm, prompt)
retrieval_chain = create_retrieval_chain(retriever, document_chain)

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Karri Prasad AI Agent", version="2.0.0")

ALLOWED_ORIGINS = [
    "https://shyamkarri.github.io",
    "http://localhost:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Models ───────────────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str   # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []

class ChatResponse(BaseModel):
    reply: str
    audio_base64: str

# ─── Endpoints ───────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "agent": "Karri Prasad RAG Agent"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    user_ip = request.client.host if request.client else "unknown"

    chat_history = []
    for m in req.history:
        if m.role == "user":
            chat_history.append(HumanMessage(content=m.content))
        else:
            chat_history.append(AIMessage(content=m.content))

    try:
        response = retrieval_chain.invoke({
            "input": req.message,
            "chat_history": chat_history
        })
        reply_text = response["answer"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LangChain/Groq Error: {str(e)}")

    # Log to SQLite
    log_chat(user_ip, req.message, reply_text)

    # TTS
    try:
        tts = gTTS(text=reply_text, lang="en", tld="com", slow=False)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        audio_b64 = base64.b64encode(mp3_buf.read()).decode("utf-8")
    except Exception:
        audio_b64 = ""

    return ChatResponse(reply=reply_text, audio_base64=audio_b64)

@app.get("/greet")
async def greet():
    greeting = (
        "Hey hi! I'm Prasad — Karri Prasad. Welcome to my portfolio! "
        "I'm an AI engineer currently at HCA Healthcare "
        "building production LLM agents and RAG pipelines. "
        "Feel free to ask me anything about my work, skills, or background!"
    )
    try:
        tts = gTTS(text=greeting, lang="en", tld="com", slow=False)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        audio_b64 = base64.b64encode(mp3_buf.read()).decode("utf-8")
    except Exception:
        audio_b64 = ""

    return {"reply": greeting, "audio_base64": audio_b64}

@app.get("/logs", response_class=HTMLResponse)
async def view_logs():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT timestamp, user_ip, user_message, ai_response FROM chat_logs ORDER BY id DESC LIMIT 100')
    rows = c.fetchall()
    conn.close()

    html = "<html><head><title>Chat Logs</title><style>body{font-family:sans-serif; padding:20px; background:#111; color:#eee;} .log{background:#222; margin-bottom:15px; padding:15px; border-radius:8px;} .time{color:#888; font-size:0.8em;} .user{color:#5b8cff; margin:10px 0;} .ai{color:#38d9f5; margin:10px 0;}</style></head><body>"
    html += "<h2>Recent Conversations (Max 100)</h2>"
    for r in rows:
        html += f"<div class='log'>"
        html += f"<div class='time'>{r[0]} | IP: {r[1]}</div>"
        html += f"<div class='user'><strong>User:</strong> {r[2]}</div>"
        html += f"<div class='ai'><strong>AI:</strong> {r[3]}</div>"
        html += f"</div>"
    html += "</body></html>"
    return html
