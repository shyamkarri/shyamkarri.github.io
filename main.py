"""
Karri Prasad – Voice AI Agent Backend
FastAPI + LangChain + Groq + FAISS RAG + PostgreSQL/SQLite history
"""

import os
import io
import base64
import time
import uuid
import logging
import json
from datetime import datetime, date
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from gtts import gTTS

# LangChain
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.callbacks import BaseCallbackHandler

# Database and Utilities
from database import init_db, SessionLocal, ConversationLog, engine
from utils import sanitize_text

# ─── Structured JSON Logging ──────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        # Attach session_id if present in logging record extra
        if hasattr(record, "session_id"):
            log_record["session_id"] = record.session_id
        return json.dumps(log_record)

# Configure loggers
root_logger = logging.getLogger()
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

# Suppress / configure uvicorn access logs to match
for uvicorn_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logger = logging.getLogger(uvicorn_logger)
    logger.handlers = []
    logger.addHandler(handler)
    logger.propagate = False

logger = logging.getLogger("agent_logger")

# ─── DB Session Dependency ───────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Initialize DB
init_db()

# ─── RAG Setup ──────────────────────────────────────────────────────────────
embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2", cache_folder="./.cache")

try:
    vectorstore = FAISS.load_local("faiss_index", embeddings, allow_dangerous_deserialization=True)
except Exception as e:
    logger.warning(f"Could not load pre-computed FAISS index: {e}")
    try:
        with open("knowledge.txt", "r") as f:
            knowledge_text = f.read()
    except FileNotFoundError:
        knowledge_text = "Karri Prasad is an AI Engineer."
    
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    docs = text_splitter.create_documents([knowledge_text])
    vectorstore = FAISS.from_documents(docs, embeddings)

retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

# ─── LangChain Setup ────────────────────────────────────────────────────────
llm = ChatGroq(model_name="llama-3.3-70b-versatile", temperature=0.7)

system_prompt = (
    "You are Karri Prasad's AI voice avatar on his portfolio website. "
    "You ARE Karri — speak in first person, warmly and naturally, like a real human conversation. "
    "IMPORTANT: Your responses will be read aloud to the user using text-to-speech. "
    "NEVER state that you are a text-based AI, a language model, or that you cannot speak, talk, or hear. "
    "Always behave as if you are speaking directly to the user. "
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

# ─── Custom Callback Handler for Token Usage ─────────────────────────────────
class GroqUsageCallbackHandler(BaseCallbackHandler):
    def __init__(self):
        self.token_usage = None
        self.model_name = None

    def on_llm_end(self, response, **kwargs):
        try:
            if response.generations:
                for gen in response.generations:
                    for g in gen:
                        if hasattr(g, 'message') and hasattr(g.message, 'response_metadata'):
                            meta = g.message.response_metadata
                            if 'token_usage' in meta:
                                self.token_usage = meta['token_usage']
                            if 'model_name' in meta:
                                self.model_name = meta['model_name']
        except Exception as e:
            logger.error(f"Callback extraction error: {e}")

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
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    audio_base64: str
    session_id: str

# ─── Endpoints ───────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "agent": "Karri Prasad RAG Agent"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request, db=Depends(get_db)):
    start_time = time.time()
    
    # Session handling
    session_id = req.session_id or str(uuid.uuid4())
    user_id = sanitize_text(req.user_id) if req.user_id else None
    user_name = sanitize_text(req.user_name) if req.user_name else None
    user_msg_clean = sanitize_text(req.message)
    
    chat_history = []
    for m in req.history:
        role = sanitize_text(m.role)
        content = sanitize_text(m.content)
        if role == "user":
            chat_history.append(HumanMessage(content=content))
        else:
            chat_history.append(AIMessage(content=content))

    reply_text = ""
    error_msg = None
    token_usage = None
    model_used = "llama-3.3-70b-versatile"
    
    cb = GroqUsageCallbackHandler()
    
    try:
        response = retrieval_chain.invoke(
            {
                "input": user_msg_clean,
                "chat_history": chat_history
            },
            config={"callbacks": [cb]}
        )
        reply_text = sanitize_text(response["answer"])
        token_usage = cb.token_usage
        if cb.model_name:
            model_used = cb.model_name
    except Exception as e:
        error_msg = str(e)
        logger.error(f"LangChain/Groq Error: {error_msg}", extra={"session_id": session_id})
        raise HTTPException(status_code=500, detail=f"LangChain/Groq Error: {error_msg}")
    finally:
        duration = time.time() - start_time
        
        # Log to Database
        log_entry = ConversationLog(
            session_id=session_id,
            user_id=user_id,
            user_name=user_name,
            user_message=user_msg_clean,
            assistant_response=reply_text if not error_msg else None,
            request_duration=duration,
            model_used=model_used,
            token_usage=token_usage,
            error_messages=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        # Structured JSON application logging
        logger.info(
            f"User message processed: {user_msg_clean[:50]}...",
            extra={
                "session_id": session_id,
                "duration": duration,
                "model": model_used,
                "tokens": token_usage
            }
        )

    # TTS
    try:
        tts = gTTS(text=reply_text, lang="en", tld="com", slow=False)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        audio_b64 = base64.b64encode(mp3_buf.read()).decode("utf-8")
    except Exception as e:
        logger.error(f"TTS conversion failed: {e}", extra={"session_id": session_id})
        audio_b64 = ""

    return ChatResponse(reply=reply_text, audio_base64=audio_b64, session_id=session_id)

@app.get("/greet")
async def greet(session_id: Optional[str] = None, user_id: Optional[str] = None, user_name: Optional[str] = None, db=Depends(get_db)):
    start_time = time.time()
    session_id = session_id or str(uuid.uuid4())
    user_id = sanitize_text(user_id) if user_id else None
    user_name = sanitize_text(user_name) if user_name else None

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
    except Exception as e:
        logger.error(f"TTS greeting failed: {e}", extra={"session_id": session_id})
        audio_b64 = ""

    duration = time.time() - start_time
    
    # Log the greeting interaction to trace session start
    log_entry = ConversationLog(
        session_id=session_id,
        user_id=user_id,
        user_name=user_name,
        user_message="/greet",
        assistant_response=greeting,
        request_duration=duration,
        model_used="static",
        token_usage=None,
        error_messages=None
    )
    db.add(log_entry)
    db.commit()

    return {"reply": greeting, "audio_base64": audio_b64, "session_id": session_id}

@app.get("/logs", response_class=HTMLResponse)
async def view_logs(db=Depends(get_db)):
    rows = db.query(ConversationLog).order_by(ConversationLog.id.desc()).limit(100).all()

    html = "<html><head><title>Chat Logs</title><style>body{font-family:sans-serif; padding:20px; background:#111; color:#eee;} .log{background:#222; margin-bottom:15px; padding:15px; border-radius:8px;} .time{color:#888; font-size:0.8em;} .user{color:#5b8cff; margin:10px 0;} .ai{color:#38d9f5; margin:10px 0;}</style></head><body>"
    html += "<h2>Recent Conversations (Max 100)</h2>"
    for r in rows:
        html += "<div class='log'>"
        html += f"<div class='time'>{r.timestamp} | Session ID: {r.session_id} | User: {r.user_name or 'Anonymous'}</div>"
        html += f"<div class='user'><strong>User:</strong> {r.user_message}</div>"
        html += f"<div class='ai'><strong>AI:</strong> {r.assistant_response}</div>"
        html += "</div>"
    html += "</body></html>"
    return html

# ─── Admin Endpoints ─────────────────────────────────────────────────────────
@app.get("/admin/sessions")
async def get_admin_sessions(db=Depends(get_db)):
    """Retrieve list of unique conversation sessions with stats."""
    # Find all sessions aggregated with activity summary
    logs = db.query(ConversationLog).order_by(ConversationLog.timestamp.desc()).all()
    sessions = {}
    for log in logs:
        s_id = log.session_id
        if s_id not in sessions:
            sessions[s_id] = {
                "session_id": s_id,
                "user_id": log.user_id,
                "user_name": log.user_name,
                "messages_count": 0,
                "last_active": log.timestamp.isoformat(),
                "created_at": log.timestamp.isoformat()
            }
        sessions[s_id]["messages_count"] += 1
        if log.timestamp.isoformat() < sessions[s_id]["created_at"]:
            sessions[s_id]["created_at"] = log.timestamp.isoformat()
            
    return list(sessions.values())

@app.get("/admin/session/{session_id}")
async def get_admin_session(session_id: str, db=Depends(get_db)):
    """Retrieve the full transcript of a specific session."""
    logs = db.query(ConversationLog).filter(ConversationLog.session_id == session_id).order_by(ConversationLog.timestamp.asc()).all()
    if not logs:
        raise HTTPException(status_code=404, detail="Session not found")
        
    return [
        {
            "id": l.id,
            "timestamp": l.timestamp.isoformat(),
            "user_id": l.user_id,
            "user_name": l.user_name,
            "user_message": l.user_message,
            "assistant_response": l.assistant_response,
            "request_duration": l.request_duration,
            "model_used": l.model_used,
            "token_usage": l.token_usage,
            "error_messages": l.error_messages
        } for l in logs
    ]

@app.get("/admin/stats")
async def get_admin_stats(db=Depends(get_db)):
    """Retrieve analytics stats for conversations."""
    logs = db.query(ConversationLog).all()
    
    total_messages = len([l for l in logs if l.user_message and l.user_message != "/greet"])
    
    # Unique sessions
    sessions = set(l.session_id for l in logs)
    total_sessions = len(sessions)
    
    # Unique users: union of defined user_ids, fallback to session_ids for anonymous
    users = set(l.user_id if l.user_id else l.session_id for l in logs)
    total_users = len(users)
    
    # DAU (Daily Active Users): active users per calendar day
    daily_users = {}
    for l in logs:
        day = l.timestamp.date()
        user = l.user_id if l.user_id else l.session_id
        if day not in daily_users:
            daily_users[day] = set()
        daily_users[day].add(user)
    
    daily_active_users = {day.isoformat(): len(usrs) for day, usrs in daily_users.items()}
    
    # Average session length:
    # 1. average messages per session
    avg_messages_per_session = total_messages / total_sessions if total_sessions > 0 else 0
    
    # 2. average session duration in seconds
    session_durations = []
    for s_id in sessions:
        s_logs = [l for l in logs if l.session_id == s_id]
        if s_logs:
            timestamps = [l.timestamp for l in s_logs]
            duration = (max(timestamps) - min(timestamps)).total_seconds()
            session_durations.append(duration)
            
    avg_session_duration_seconds = sum(session_durations) / len(session_durations) if session_durations else 0

    return {
        "total_users": total_users,
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "daily_active_users": daily_active_users,
        "average_session_length": {
            "messages": round(avg_messages_per_session, 2),
            "duration_seconds": round(avg_session_duration_seconds, 2)
        }
    }
