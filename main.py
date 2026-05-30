"""
Karri Prasad – Voice AI Agent Backend + AI Operations Dashboard
FastAPI + LangChain + Groq + FAISS RAG + PostgreSQL
Modules: Sessions, Agent Analytics, Application Tracker,
         Notifications, Dashboard, Scheduler, Reporting, Auth
"""

import os
import io
import csv
import base64
import time
import uuid
import logging
import json
import smtplib
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Depends, status, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from jose import JWTError, jwt
from passlib.context import CryptContext

# LangChain
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEndpointEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.callbacks import BaseCallbackHandler

# APScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Database
from database import (
    init_db, seed_db, SessionLocal,
    ConversationLog, AgentExecution, JobApplication,
    Notification, SchedulerJob, SchedulerJobRun, Report, AdminUser, engine
)
from utils import sanitize_text
from sqlalchemy import func, desc, distinct

# ─── Constants ───────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-dashboard-key-change-in-prod-2025")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 24h

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@karriprasad.ai")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme123")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@karriprasad.ai")

REPORTS_DIR = os.getenv("REPORTS_DIR", "/tmp/reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ─── Auth Utilities ──────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_current_user(token: str = Depends(oauth2_scheme), db=Depends(get_db)) -> AdminUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(AdminUser).filter_by(email=email).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user


# Optional auth — returns user if token present, None if not
async def get_optional_user(request: Request, db=Depends(get_db)) -> Optional[AdminUser]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        return db.query(AdminUser).filter_by(email=email).first()
    except Exception:
        return None


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
        if hasattr(record, "session_id"):
            log_record["session_id"] = record.session_id
        return json.dumps(log_record)


root_logger = logging.getLogger()
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

for uvicorn_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    lg = logging.getLogger(uvicorn_logger)
    lg.handlers = []
    lg.addHandler(handler)
    lg.propagate = False

logger = logging.getLogger("agent_logger")

# ─── DB + Seed Init ──────────────────────────────────────────────────────────
init_db()
seed_db()

# ─── RAG Setup ───────────────────────────────────────────────────────────────
embeddings = HuggingFaceEndpointEmbeddings(
    model="sentence-transformers/all-MiniLM-L6-v2",
    huggingfacehub_api_token=os.getenv("HUGGINGFACEHUB_API_TOKEN")
)

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

# ─── LangChain Setup ─────────────────────────────────────────────────────────
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


class GroqUsageCallbackHandler(BaseCallbackHandler):
    def __init__(self):
        self.token_usage = None
        self.model_name = None

    def on_llm_end(self, response, **kwargs):
        try:
            if response.generations:
                for gen in response.generations:
                    for g in gen:
                        if hasattr(g, "message") and hasattr(g.message, "response_metadata"):
                            meta = g.message.response_metadata
                            if "token_usage" in meta:
                                self.token_usage = meta["token_usage"]
                            if "model_name" in meta:
                                self.model_name = meta["model_name"]
        except Exception as e:
            logger.error(f"Callback extraction error: {e}")


# ─── APScheduler ─────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()


def _run_job(job_name: str, job_type: str):
    """Execute a scheduler job and log its run."""
    db = SessionLocal()
    start = time.time()
    run_record = SchedulerJobRun(job_id=None, started_at=datetime.utcnow(), status="running")
    try:
        job = db.query(SchedulerJob).filter_by(name=job_name).first()
        if not job:
            return
        run_record.job_id = job.id
        db.add(run_record)
        db.commit()
        db.refresh(run_record)

        output = ""
        if job_type == "weekly_report":
            output = _generate_report_data("weekly", db)
        elif job_type == "monthly_report":
            output = _generate_report_data("monthly", db)
        elif job_type == "daily_digest":
            output = _send_daily_digest(db)

        duration_ms = (time.time() - start) * 1000
        run_record.status = "success"
        run_record.finished_at = datetime.utcnow()
        run_record.duration_ms = duration_ms
        run_record.output = str(output)
        job.last_run = datetime.utcnow()
        job.run_count = (job.run_count or 0) + 1
        job.last_run_duration_ms = duration_ms
        job.status = "success"
        db.commit()
        logger.info(f"[Scheduler] Job '{job_name}' completed in {duration_ms:.0f}ms")
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        run_record.status = "failed"
        run_record.finished_at = datetime.utcnow()
        run_record.duration_ms = duration_ms
        run_record.error = str(e)
        if run_record.job_id:
            job = db.query(SchedulerJob).filter_by(id=run_record.job_id).first()
            if job:
                job.fail_count = (job.fail_count or 0) + 1
                job.last_error = str(e)
                job.status = "failed"
        db.commit()
        logger.error(f"[Scheduler] Job '{job_name}' failed: {e}")
    finally:
        db.close()


def _generate_report_data(report_type: str, db) -> str:
    """Generate summary report data and store a Report record."""
    now = datetime.utcnow()
    if report_type == "weekly":
        since = now - timedelta(days=7)
    else:
        since = now - timedelta(days=30)

    total_sessions = db.query(func.count(distinct(ConversationLog.session_id)))\
        .filter(ConversationLog.timestamp >= since).scalar() or 0
    total_messages = db.query(func.count(ConversationLog.id))\
        .filter(ConversationLog.timestamp >= since).scalar() or 0
    total_agents = db.query(func.count(AgentExecution.id))\
        .filter(AgentExecution.timestamp >= since).scalar() or 0
    total_apps = db.query(func.count(JobApplication.id))\
        .filter(JobApplication.created_at >= since).scalar() or 0

    summary = {
        "period": report_type,
        "from": since.isoformat(),
        "to": now.isoformat(),
        "sessions": total_sessions,
        "messages": total_messages,
        "agent_executions": total_agents,
        "job_applications": total_apps,
    }
    report = Report(
        type=report_type,
        format="json",
        title=f"{report_type.capitalize()} Report — {now.strftime('%Y-%m-%d')}",
        status="completed",
        summary=summary,
        generated_by="scheduler",
    )
    db.add(report)
    db.commit()

    # Create notification
    notif = Notification(
        type="weekly_report",
        title=f"{report_type.capitalize()} report ready",
        message=f"Your {report_type} analytics report for {since.strftime('%b %d')}–{now.strftime('%b %d')} is ready.",
        data={"report_id": report.id},
    )
    db.add(notif)
    db.commit()
    return json.dumps(summary)


def _send_daily_digest(db) -> str:
    """Create a daily digest notification."""
    now = datetime.utcnow()
    since = now - timedelta(days=1)
    unread = db.query(func.count(Notification.id)).filter_by(is_read=False).scalar() or 0
    msgs = db.query(func.count(ConversationLog.id))\
        .filter(ConversationLog.timestamp >= since).scalar() or 0
    notif = Notification(
        type="system",
        title="Daily digest",
        message=f"Yesterday: {msgs} new messages, {unread} unread notifications.",
    )
    db.add(notif)
    db.commit()
    return f"digest: {msgs} messages, {unread} unread"


def _start_scheduler():
    """Load and start all enabled cron jobs from DB."""
    db = SessionLocal()
    try:
        jobs = db.query(SchedulerJob).filter_by(enabled=True).all()
        for job in jobs:
            if job.cron_expression:
                try:
                    parts = job.cron_expression.split()
                    if len(parts) == 5:
                        trigger = CronTrigger(
                            minute=parts[0], hour=parts[1],
                            day=parts[2], month=parts[3], day_of_week=parts[4]
                        )
                        scheduler.add_job(
                            _run_job,
                            trigger=trigger,
                            args=[job.name, job.job_type],
                            id=f"job_{job.id}",
                            replace_existing=True,
                            misfire_grace_time=300,
                        )
                        logger.info(f"[Scheduler] Registered job '{job.name}' [{job.cron_expression}]")
                except Exception as e:
                    logger.error(f"[Scheduler] Failed to register '{job.name}': {e}")
    finally:
        db.close()


scheduler.start()
_start_scheduler()

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Karri Prasad AI Agent + Operations Dashboard", version="3.0.0")

ALLOWED_ORIGINS = [
    "https://shyamkarri.github.io",
    "http://localhost:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost",
    "http://localhost:8000",
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Pydantic Models ──────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str
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

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict

class LoginRequest(BaseModel):
    email: str
    password: str

class ApplicationCreate(BaseModel):
    company_name: str
    position: str
    application_date: Optional[datetime] = None
    status: str = "saved"
    notes: Optional[str] = None
    job_url: Optional[str] = None
    salary_range: Optional[str] = None
    location: Optional[str] = None
    remote: bool = False
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    priority: int = 0

class ApplicationUpdate(BaseModel):
    company_name: Optional[str] = None
    position: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    salary_range: Optional[str] = None
    location: Optional[str] = None
    remote: Optional[bool] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    interview_stages: Optional[list] = None
    offer_amount: Optional[str] = None
    offer_deadline: Optional[datetime] = None
    priority: Optional[int] = None

class AgentLogRequest(BaseModel):
    agent_name: str
    workflow: Optional[str] = None
    session_id: Optional[str] = None
    input_text: Optional[str] = None
    output_text: Optional[str] = None
    latency_ms: Optional[float] = None
    success: bool = True
    error_message: Optional[str] = None
    tool_used: Optional[str] = None
    model_used: Optional[str] = None
    token_usage: Optional[dict] = None

class NotificationCreate(BaseModel):
    type: str
    title: str
    message: Optional[str] = None
    data: Optional[dict] = None
    user_email: Optional[str] = None

class SchedulerJobCreate(BaseModel):
    name: str
    description: Optional[str] = None
    cron_expression: Optional[str] = None
    job_type: str
    enabled: bool = True
    config: Optional[dict] = None

class ReportRequest(BaseModel):
    type: str  # weekly, monthly, agent, job_search, application
    format: str = "csv"  # pdf, csv, excel

# ─── Helper: send email ──────────────────────────────────────────────────────
def _send_email(to_addr: str, subject: str, body: str):
    if not SMTP_HOST or not SMTP_USER:
        logger.info(f"[Email] SMTP not configured — skipped email to {to_addr}: {subject}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, to_addr, msg.as_string())
        logger.info(f"[Email] Sent '{subject}' to {to_addr}")
    except Exception as e:
        logger.error(f"[Email] Failed to send to {to_addr}: {e}")


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC ENDPOINTS (portfolio chatbot — no auth required)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"status": "ok", "agent": "Karri Prasad RAG Agent", "version": "3.0.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request, db=Depends(get_db)):
    start_time = time.time()
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
            {"input": user_msg_clean, "chat_history": chat_history},
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
        log_entry = ConversationLog(
            session_id=session_id, user_id=user_id, user_name=user_name,
            user_message=user_msg_clean,
            assistant_response=reply_text if not error_msg else None,
            request_duration=duration, model_used=model_used,
            token_usage=token_usage, error_messages=error_msg,
        )
        db.add(log_entry)

        # Also log to AgentExecution for analytics
        exec_entry = AgentExecution(
            agent_name="portfolio-rag-agent",
            workflow="chat",
            session_id=session_id,
            input_text=user_msg_clean[:500],
            output_text=reply_text[:500] if reply_text else None,
            latency_ms=duration * 1000,
            success=error_msg is None,
            error_message=error_msg,
            model_used=model_used,
            token_usage=token_usage,
        )
        db.add(exec_entry)
        db.commit()
        logger.info(f"Chat processed: {user_msg_clean[:50]}", extra={"session_id": session_id})

    try:
        from gtts import gTTS
        tts = gTTS(text=reply_text, lang="en", tld="com", slow=False)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        audio_b64 = base64.b64encode(mp3_buf.read()).decode("utf-8")
    except Exception as e:
        logger.error(f"TTS failed: {e}", extra={"session_id": session_id})
        audio_b64 = ""

    return ChatResponse(reply=reply_text, audio_base64=audio_b64, session_id=session_id)


@app.get("/greet")
async def greet(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    db=Depends(get_db)
):
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
        from gtts import gTTS
        tts = gTTS(text=greeting, lang="en", tld="com", slow=False)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        audio_b64 = base64.b64encode(mp3_buf.read()).decode("utf-8")
    except Exception as e:
        logger.error(f"TTS greeting failed: {e}")
        audio_b64 = ""

    duration = time.time() - start_time
    db.add(ConversationLog(
        session_id=session_id, user_id=user_id, user_name=user_name,
        user_message="/greet", assistant_response=greeting,
        request_duration=duration, model_used="static",
    ))
    db.commit()
    return {"reply": greeting, "audio_base64": audio_b64, "session_id": session_id}


# ─── Legacy /logs endpoint (kept for backward compat) ────────────────────────
@app.get("/logs", response_class=HTMLResponse)
async def view_logs(db=Depends(get_db)):
    rows = db.query(ConversationLog).order_by(ConversationLog.id.desc()).limit(100).all()
    html = "<html><head><title>Chat Logs</title><style>body{font-family:sans-serif;padding:20px;background:#111;color:#eee;}.log{background:#222;margin-bottom:15px;padding:15px;border-radius:8px;}.time{color:#888;font-size:0.8em;}.user{color:#5b8cff;margin:10px 0;}.ai{color:#38d9f5;margin:10px 0;}</style></head><body>"
    html += f"<h2>Recent Conversations (Max 100) — <a href='/admin' style='color:#5b8cff'>Open Dashboard →</a></h2>"
    for r in rows:
        html += "<div class='log'>"
        html += f"<div class='time'>{r.timestamp} | Session: {r.session_id} | User: {r.user_name or 'Anonymous'}</div>"
        html += f"<div class='user'><strong>User:</strong> {r.user_message}</div>"
        html += f"<div class='ai'><strong>AI:</strong> {r.assistant_response}</div>"
        html += "</div>"
    html += "</body></html>"
    return html


# ─── Legacy /admin/sessions, /admin/stats (kept for backward compat) ─────────
@app.get("/admin/sessions")
async def get_admin_sessions(db=Depends(get_db)):
    logs = db.query(ConversationLog).order_by(ConversationLog.timestamp.desc()).all()
    sessions = {}
    for log in logs:
        s_id = log.session_id
        if s_id not in sessions:
            sessions[s_id] = {
                "session_id": s_id, "user_id": log.user_id, "user_name": log.user_name,
                "messages_count": 0, "last_active": log.timestamp.isoformat(),
                "created_at": log.timestamp.isoformat()
            }
        sessions[s_id]["messages_count"] += 1
        if log.timestamp.isoformat() < sessions[s_id]["created_at"]:
            sessions[s_id]["created_at"] = log.timestamp.isoformat()
    return list(sessions.values())


@app.get("/admin/session/{session_id}")
async def get_admin_session(session_id: str, db=Depends(get_db)):
    logs = db.query(ConversationLog).filter_by(session_id=session_id).order_by(ConversationLog.timestamp.asc()).all()
    if not logs:
        raise HTTPException(status_code=404, detail="Session not found")
    return [{
        "id": l.id, "timestamp": l.timestamp.isoformat(),
        "user_id": l.user_id, "user_name": l.user_name,
        "user_message": l.user_message, "assistant_response": l.assistant_response,
        "request_duration": l.request_duration, "model_used": l.model_used,
        "token_usage": l.token_usage, "error_messages": l.error_messages
    } for l in logs]


@app.get("/admin/stats")
async def get_admin_stats(db=Depends(get_db)):
    logs = db.query(ConversationLog).all()
    total_messages = len([l for l in logs if l.user_message and l.user_message != "/greet"])
    sessions = set(l.session_id for l in logs)
    total_sessions = len(sessions)
    users = set(l.user_id if l.user_id else l.session_id for l in logs)
    total_users = len(users)
    daily_users: Dict[str, set] = {}
    for l in logs:
        day = l.timestamp.date().isoformat()
        user = l.user_id if l.user_id else l.session_id
        daily_users.setdefault(day, set()).add(user)
    daily_active_users = {day: len(u) for day, u in daily_users.items()}
    avg_messages_per_session = total_messages / total_sessions if total_sessions > 0 else 0
    session_durations = []
    for s_id in sessions:
        s_logs = [l for l in logs if l.session_id == s_id]
        if s_logs:
            ts = [l.timestamp for l in s_logs]
            session_durations.append((max(ts) - min(ts)).total_seconds())
    avg_session_duration_seconds = sum(session_durations) / len(session_durations) if session_durations else 0
    return {
        "total_users": total_users, "total_sessions": total_sessions,
        "total_messages": total_messages, "daily_active_users": daily_active_users,
        "average_session_length": {
            "messages": round(avg_messages_per_session, 2),
            "duration_seconds": round(avg_session_duration_seconds, 2),
        }
    }


# ════════════════════════════════════════════════════════════════════════════
# MODULE 8 — AUTHENTICATION
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/auth/token", response_model=TokenResponse)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    user = db.query(AdminUser).filter_by(email=form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    user.last_login = datetime.utcnow()
    db.commit()
    token = create_access_token({"sub": user.email, "role": user.role})
    return TokenResponse(
        access_token=token, token_type="bearer",
        user={"email": user.email, "name": user.name, "role": user.role}
    )


@app.post("/api/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest, db=Depends(get_db)):
    user = db.query(AdminUser).filter_by(email=req.email).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    user.last_login = datetime.utcnow()
    db.commit()
    token = create_access_token({"sub": user.email, "role": user.role})
    return TokenResponse(
        access_token=token, token_type="bearer",
        user={"email": user.email, "name": user.name, "role": user.role}
    )


@app.get("/api/auth/me")
async def me(current_user: AdminUser = Depends(get_current_user)):
    return {"email": current_user.email, "name": current_user.name, "role": current_user.role,
            "last_login": current_user.last_login.isoformat() if current_user.last_login else None}


# ════════════════════════════════════════════════════════════════════════════
# MODULE 5 — DASHBOARD (unified KPIs)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/dashboard")
async def dashboard_kpis(db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)):
    now = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    # Sessions
    total_sessions = db.query(func.count(distinct(ConversationLog.session_id))).scalar() or 0
    active_today = db.query(func.count(distinct(ConversationLog.session_id)))\
        .filter(ConversationLog.timestamp >= day_start).scalar() or 0
    messages_today = db.query(func.count(ConversationLog.id))\
        .filter(ConversationLog.timestamp >= day_start).scalar() or 0
    avg_dur = db.query(func.avg(ConversationLog.request_duration)).scalar() or 0

    # Agents
    total_agents = db.query(func.count(AgentExecution.id)).scalar() or 0
    agents_today = db.query(func.count(AgentExecution.id))\
        .filter(AgentExecution.timestamp >= day_start).scalar() or 0
    success_count = db.query(func.count(AgentExecution.id)).filter_by(success=True).scalar() or 0
    avg_latency = db.query(func.avg(AgentExecution.latency_ms)).scalar() or 0
    success_rate = round((success_count / total_agents * 100) if total_agents > 0 else 0, 1)

    # Applications
    total_apps = db.query(func.count(JobApplication.id)).scalar() or 0
    interviews = db.query(func.count(JobApplication.id))\
        .filter(JobApplication.status.in_(["interview", "final_round"])).scalar() or 0
    offers = db.query(func.count(JobApplication.id)).filter_by(status="offer").scalar() or 0
    offer_rate = round((offers / total_apps * 100) if total_apps > 0 else 0, 1)

    # Notifications
    unread_notifs = db.query(func.count(Notification.id)).filter_by(is_read=False).scalar() or 0

    # Scheduler
    failed_jobs = db.query(func.count(SchedulerJob.id)).filter_by(status="failed").scalar() or 0

    # Reports
    total_reports = db.query(func.count(Report.id)).scalar() or 0

    # Time series: sessions per day (last 30 days)
    sessions_timeseries = []
    for i in range(29, -1, -1):
        d = (now - timedelta(days=i)).date()
        count = db.query(func.count(distinct(ConversationLog.session_id)))\
            .filter(func.date(ConversationLog.timestamp) == d).scalar() or 0
        sessions_timeseries.append({"date": d.isoformat(), "count": count})

    # Agent usage timeseries
    agent_timeseries = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date()
        count = db.query(func.count(AgentExecution.id))\
            .filter(func.date(AgentExecution.timestamp) == d).scalar() or 0
        agent_timeseries.append({"date": d.isoformat(), "count": count})

    # Application status distribution
    app_by_status = {}
    for status_val in ["saved", "applied", "screening", "interview", "final_round", "offer", "rejected"]:
        c = db.query(func.count(JobApplication.id)).filter_by(status=status_val).scalar() or 0
        app_by_status[status_val] = c

    # Recent activity feed
    recent_sessions = db.query(ConversationLog)\
        .order_by(desc(ConversationLog.timestamp)).limit(5).all()
    recent_agents = db.query(AgentExecution)\
        .order_by(desc(AgentExecution.timestamp)).limit(5).all()
    activity_feed = []
    for s in recent_sessions:
        activity_feed.append({
            "type": "session", "time": s.timestamp.isoformat(),
            "text": f"Chat: {(s.user_message or '')[:60]}",
            "status": "error" if s.error_messages else "success",
        })
    for a in recent_agents:
        activity_feed.append({
            "type": "agent", "time": a.timestamp.isoformat(),
            "text": f"{a.agent_name}: {(a.input_text or '')[:50]}",
            "status": "success" if a.success else "error",
        })
    activity_feed.sort(key=lambda x: x["time"], reverse=True)
    activity_feed = activity_feed[:10]

    return {
        "kpis": {
            "sessions": {"total": total_sessions, "today": active_today, "label": "Active Sessions"},
            "messages": {"total": messages_today, "label": "Messages Today"},
            "avg_duration": {"value": round(avg_dur * 1000, 0), "label": "Avg Response (ms)"},
            "agent_executions": {"total": total_agents, "today": agents_today, "label": "Agent Executions"},
            "success_rate": {"value": success_rate, "label": "Success Rate %"},
            "avg_latency": {"value": round(avg_latency, 0), "label": "Avg Latency (ms)"},
            "applications": {"total": total_apps, "interviews": interviews, "offers": offers, "offer_rate": offer_rate},
            "notifications": {"unread": unread_notifs},
            "scheduler": {"failed_jobs": failed_jobs},
            "reports": {"total": total_reports},
        },
        "charts": {
            "sessions_timeseries": sessions_timeseries,
            "agent_timeseries": agent_timeseries,
            "app_by_status": app_by_status,
        },
        "activity_feed": activity_feed,
        "system": {
            "status": "healthy",
            "uptime": "N/A",
            "db": "postgresql" if "postgresql" in str(engine.url) else "sqlite",
            "timestamp": now.isoformat(),
        }
    }


# ════════════════════════════════════════════════════════════════════════════
# MODULE 1 — SESSION LOGGING
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/sessions")
async def api_sessions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user),
):
    q = db.query(
        ConversationLog.session_id,
        func.count(ConversationLog.id).label("message_count"),
        func.max(ConversationLog.timestamp).label("last_active"),
        func.min(ConversationLog.timestamp).label("created_at"),
        func.max(ConversationLog.user_name).label("user_name"),
        func.max(ConversationLog.user_id).label("user_id"),
        func.sum(func.case((ConversationLog.error_messages != None, 1), else_=0)).label("errors"),
        func.avg(ConversationLog.request_duration).label("avg_duration"),
    ).group_by(ConversationLog.session_id)

    if search:
        q = q.filter(ConversationLog.session_id.contains(search) |
                     ConversationLog.user_name.contains(search) |
                     ConversationLog.user_message.contains(search))

    total = q.count()
    results = q.order_by(desc("last_active")).offset((page - 1) * limit).limit(limit).all()

    sessions = [{
        "session_id": r.session_id,
        "message_count": r.message_count,
        "last_active": r.last_active.isoformat() if r.last_active else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "user_name": r.user_name,
        "user_id": r.user_id,
        "errors": int(r.errors or 0),
        "avg_duration_ms": round((r.avg_duration or 0) * 1000, 1),
        "duration_seconds": round(
            (r.last_active - r.created_at).total_seconds()
            if r.last_active and r.created_at else 0, 1
        ),
    } for r in results]

    return {"sessions": sessions, "total": total, "page": page, "limit": limit}


@app.get("/api/sessions/{session_id}")
async def api_session_detail(
    session_id: str, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    logs = db.query(ConversationLog).filter_by(session_id=session_id)\
        .order_by(ConversationLog.timestamp.asc()).all()
    if not logs:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session_id,
        "messages": [{
            "id": l.id, "timestamp": l.timestamp.isoformat(),
            "user_message": l.user_message, "assistant_response": l.assistant_response,
            "request_duration_ms": round((l.request_duration or 0) * 1000, 1),
            "model_used": l.model_used, "token_usage": l.token_usage,
            "error": l.error_messages,
        } for l in logs]
    }


@app.get("/api/sessions/{session_id}/export")
async def api_session_export(
    session_id: str, format: str = Query("csv", enum=["csv", "json"]),
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    logs = db.query(ConversationLog).filter_by(session_id=session_id)\
        .order_by(ConversationLog.timestamp.asc()).all()
    if not logs:
        raise HTTPException(status_code=404, detail="Session not found")

    if format == "json":
        data = json.dumps([{
            "id": l.id, "timestamp": l.timestamp.isoformat(),
            "user_message": l.user_message, "assistant_response": l.assistant_response,
            "duration_ms": round((l.request_duration or 0) * 1000, 1),
            "model": l.model_used, "tokens": l.token_usage,
        } for l in logs], indent=2)
        return StreamingResponse(io.BytesIO(data.encode()), media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=session_{session_id}.json"})

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Timestamp", "User Message", "AI Response", "Duration(ms)", "Model"])
    for l in logs:
        writer.writerow([l.id, l.timestamp.isoformat(), l.user_message,
                         l.assistant_response, round((l.request_duration or 0) * 1000, 1), l.model_used])
    output.seek(0)
    return StreamingResponse(io.BytesIO(output.read().encode()), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=session_{session_id}.csv"})


@app.get("/api/sessions/stats/daily")
async def api_sessions_daily(
    days: int = Query(30, ge=1, le=90),
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    now = datetime.utcnow()
    result = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).date()
        sessions = db.query(func.count(distinct(ConversationLog.session_id)))\
            .filter(func.date(ConversationLog.timestamp) == d).scalar() or 0
        messages = db.query(func.count(ConversationLog.id))\
            .filter(func.date(ConversationLog.timestamp) == d).scalar() or 0
        result.append({"date": d.isoformat(), "sessions": sessions, "messages": messages})
    return result


# ════════════════════════════════════════════════════════════════════════════
# MODULE 2 — AGENT ANALYTICS
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/agents/log")
async def log_agent_execution(
    req: AgentLogRequest, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    entry = AgentExecution(
        agent_name=req.agent_name, workflow=req.workflow, session_id=req.session_id,
        input_text=req.input_text, output_text=req.output_text, latency_ms=req.latency_ms,
        success=req.success, error_message=req.error_message, tool_used=req.tool_used,
        model_used=req.model_used, token_usage=req.token_usage,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "message": "Logged"}


@app.get("/api/agents/stats")
async def agent_stats(
    days: int = Query(30, ge=1, le=90),
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    since = datetime.utcnow() - timedelta(days=days)
    q = db.query(AgentExecution).filter(AgentExecution.timestamp >= since)
    all_execs = q.all()

    total = len(all_execs)
    success = sum(1 for e in all_execs if e.success)
    failed = total - success
    avg_latency = round(sum(e.latency_ms or 0 for e in all_execs) / total, 1) if total else 0

    # By agent
    by_agent: Dict[str, dict] = {}
    for e in all_execs:
        a = e.agent_name
        if a not in by_agent:
            by_agent[a] = {"name": a, "total": 0, "success": 0, "failed": 0, "total_latency": 0}
        by_agent[a]["total"] += 1
        by_agent[a]["success" if e.success else "failed"] += 1
        by_agent[a]["total_latency"] += e.latency_ms or 0

    agent_rankings = []
    for a, stats in by_agent.items():
        agent_rankings.append({
            "name": a,
            "total": stats["total"],
            "success": stats["success"],
            "failed": stats["failed"],
            "success_rate": round(stats["success"] / stats["total"] * 100, 1),
            "avg_latency_ms": round(stats["total_latency"] / stats["total"], 1),
        })
    agent_rankings.sort(key=lambda x: x["success_rate"], reverse=True)

    # Tool usage
    tool_usage: Dict[str, int] = {}
    for e in all_execs:
        if e.tool_used:
            tool_usage[e.tool_used] = tool_usage.get(e.tool_used, 0) + 1

    # Daily breakdown
    now = datetime.utcnow()
    daily = []
    for i in range(min(days, 30) - 1, -1, -1):
        d = (now - timedelta(days=i)).date()
        day_execs = [e for e in all_execs if e.timestamp.date() == d]
        s = sum(1 for e in day_execs if e.success)
        daily.append({
            "date": d.isoformat(), "total": len(day_execs), "success": s,
            "failed": len(day_execs) - s,
            "avg_latency_ms": round(
                sum(e.latency_ms or 0 for e in day_execs) / len(day_execs), 1
            ) if day_execs else 0,
        })

    return {
        "summary": {
            "total": total, "success": success, "failed": failed,
            "success_rate": round(success / total * 100, 1) if total else 0,
            "avg_latency_ms": avg_latency,
        },
        "agent_rankings": agent_rankings,
        "tool_usage": [{"tool": k, "count": v} for k, v in sorted(tool_usage.items(), key=lambda x: -x[1])],
        "daily": daily,
    }


@app.get("/api/agents/feed")
async def agent_feed(
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    execs = db.query(AgentExecution).order_by(desc(AgentExecution.timestamp)).limit(limit).all()
    return [{
        "id": e.id, "timestamp": e.timestamp.isoformat(), "agent_name": e.agent_name,
        "workflow": e.workflow, "session_id": e.session_id,
        "input_preview": (e.input_text or "")[:80],
        "latency_ms": e.latency_ms, "success": e.success,
        "error": e.error_message, "tool_used": e.tool_used, "model_used": e.model_used,
    } for e in execs]


# ════════════════════════════════════════════════════════════════════════════
# MODULE 3 — APPLICATION TRACKER
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/applications")
async def list_applications(
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = None, search: Optional[str] = None,
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    q = db.query(JobApplication)
    if status:
        q = q.filter_by(status=status)
    if search:
        q = q.filter(
            JobApplication.company_name.ilike(f"%{search}%") |
            JobApplication.position.ilike(f"%{search}%")
        )
    total = q.count()
    apps = q.order_by(desc(JobApplication.updated_at)).offset((page - 1) * limit).limit(limit).all()
    return {
        "applications": [_app_to_dict(a) for a in apps],
        "total": total, "page": page, "limit": limit
    }


@app.post("/api/applications", status_code=201)
async def create_application(
    req: ApplicationCreate, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    app_obj = JobApplication(**req.model_dump())
    db.add(app_obj)
    db.commit()
    db.refresh(app_obj)

    # Create notification
    db.add(Notification(
        type="system", title=f"Application added: {req.company_name}",
        message=f"New {req.status} application for {req.position} at {req.company_name}.",
        data={"application_id": app_obj.id},
    ))
    db.commit()
    return _app_to_dict(app_obj)


@app.get("/api/applications/kanban")
async def applications_kanban(
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    statuses = ["saved", "applied", "screening", "interview", "final_round", "offer", "rejected"]
    result = {}
    for s in statuses:
        apps = db.query(JobApplication).filter_by(status=s)\
            .order_by(desc(JobApplication.updated_at)).all()
        result[s] = [_app_to_dict(a) for a in apps]
    return result


@app.get("/api/applications/{app_id}")
async def get_application(
    app_id: int, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    app_obj = db.query(JobApplication).filter_by(id=app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    return _app_to_dict(app_obj)


@app.patch("/api/applications/{app_id}")
async def update_application(
    app_id: int, req: ApplicationUpdate, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    app_obj = db.query(JobApplication).filter_by(id=app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    old_status = app_obj.status
    for field, value in req.model_dump(exclude_none=True).items():
        setattr(app_obj, field, value)
    app_obj.updated_at = datetime.utcnow()

    # Notify on status change
    if req.status and req.status != old_status:
        db.add(Notification(
            type="system",
            title=f"Status update: {app_obj.company_name}",
            message=f"{app_obj.position} at {app_obj.company_name}: {old_status} → {req.status}",
            data={"application_id": app_id},
        ))
    db.commit()
    db.refresh(app_obj)
    return _app_to_dict(app_obj)


@app.delete("/api/applications/{app_id}", status_code=204)
async def delete_application(
    app_id: int, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    app_obj = db.query(JobApplication).filter_by(id=app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    db.delete(app_obj)
    db.commit()


@app.get("/api/applications/stats/summary")
async def application_stats(
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    total = db.query(func.count(JobApplication.id)).scalar() or 0
    by_status = {}
    for s in ["saved", "applied", "screening", "interview", "final_round", "offer", "rejected"]:
        by_status[s] = db.query(func.count(JobApplication.id)).filter_by(status=s).scalar() or 0
    interviews = by_status["interview"] + by_status["final_round"]
    applied = by_status["applied"] + by_status["screening"] + interviews + by_status["offer"] + by_status["rejected"]
    response_rate = round((interviews + by_status["offer"]) / applied * 100, 1) if applied > 0 else 0
    offer_rate = round(by_status["offer"] / applied * 100, 1) if applied > 0 else 0
    return {
        "total": total, "by_status": by_status,
        "interviews_scheduled": interviews, "response_rate": response_rate,
        "offer_rate": offer_rate,
    }


def _app_to_dict(a: JobApplication) -> dict:
    return {
        "id": a.id, "company_name": a.company_name, "position": a.position,
        "status": a.status, "notes": a.notes, "job_url": a.job_url,
        "salary_range": a.salary_range, "location": a.location, "remote": a.remote,
        "contact_name": a.contact_name, "contact_email": a.contact_email,
        "interview_stages": a.interview_stages or [],
        "offer_amount": a.offer_amount,
        "offer_deadline": a.offer_deadline.isoformat() if a.offer_deadline else None,
        "priority": a.priority,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "application_date": a.application_date.isoformat() if a.application_date else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# MODULE 4 — NOTIFICATIONS
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/notifications")
async def list_notifications(
    page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100),
    unread_only: bool = False, type: Optional[str] = None,
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    q = db.query(Notification)
    if unread_only:
        q = q.filter_by(is_read=False)
    if type:
        q = q.filter_by(type=type)
    total = q.count()
    notifs = q.order_by(desc(Notification.created_at)).offset((page - 1) * limit).limit(limit).all()
    return {
        "notifications": [{
            "id": n.id, "type": n.type, "title": n.title, "message": n.message,
            "is_read": n.is_read, "created_at": n.created_at.isoformat(),
            "data": n.data, "email_sent": n.email_sent,
        } for n in notifs],
        "total": total, "unread_count": db.query(func.count(Notification.id)).filter_by(is_read=False).scalar() or 0
    }


@app.post("/api/notifications", status_code=201)
async def create_notification(
    req: NotificationCreate, background_tasks: BackgroundTasks,
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    notif = Notification(
        type=req.type, title=req.title, message=req.message,
        data=req.data, user_email=req.user_email,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    if req.user_email:
        background_tasks.add_task(_send_email, req.user_email, req.title, req.message or "")
    return {"id": notif.id, "message": "Created"}


@app.post("/api/notifications/mark-read")
async def mark_notifications_read(
    ids: Optional[List[int]] = None, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    q = db.query(Notification)
    if ids:
        q = q.filter(Notification.id.in_(ids))
    q.update({"is_read": True}, synchronize_session=False)
    db.commit()
    return {"message": "Marked as read"}


@app.delete("/api/notifications/{notif_id}", status_code=204)
async def delete_notification(
    notif_id: int, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    n = db.query(Notification).filter_by(id=notif_id).first()
    if n:
        db.delete(n)
        db.commit()


# ════════════════════════════════════════════════════════════════════════════
# MODULE 6 — SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/scheduler/jobs")
async def list_scheduler_jobs(
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    jobs = db.query(SchedulerJob).order_by(SchedulerJob.name).all()
    return [_job_to_dict(j) for j in jobs]


@app.post("/api/scheduler/jobs", status_code=201)
async def create_scheduler_job(
    req: SchedulerJobCreate, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    job = SchedulerJob(**req.model_dump())
    db.add(job)
    db.commit()
    db.refresh(job)
    # Register with APScheduler if enabled
    if job.enabled and job.cron_expression:
        try:
            parts = job.cron_expression.split()
            if len(parts) == 5:
                trigger = CronTrigger(
                    minute=parts[0], hour=parts[1],
                    day=parts[2], month=parts[3], day_of_week=parts[4]
                )
                scheduler.add_job(
                    _run_job, trigger=trigger, args=[job.name, job.job_type],
                    id=f"job_{job.id}", replace_existing=True,
                )
        except Exception as e:
            logger.error(f"Failed to register job: {e}")
    return _job_to_dict(job)


@app.post("/api/scheduler/jobs/{job_id}/run")
async def run_job_now(
    job_id: int, background_tasks: BackgroundTasks,
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    job = db.query(SchedulerJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    background_tasks.add_task(_run_job, job.name, job.job_type)
    return {"message": f"Job '{job.name}' triggered"}


@app.patch("/api/scheduler/jobs/{job_id}")
async def update_scheduler_job(
    job_id: int, enabled: Optional[bool] = None,
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    job = db.query(SchedulerJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if enabled is not None:
        job.enabled = enabled
    db.commit()
    return _job_to_dict(job)


@app.get("/api/scheduler/history")
async def scheduler_history(
    job_id: Optional[int] = None, limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    q = db.query(SchedulerJobRun)
    if job_id:
        q = q.filter_by(job_id=job_id)
    runs = q.order_by(desc(SchedulerJobRun.started_at)).limit(limit).all()
    return [{
        "id": r.id, "job_id": r.job_id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status, "duration_ms": r.duration_ms,
        "output": r.output, "error": r.error,
    } for r in runs]


def _job_to_dict(j: SchedulerJob) -> dict:
    return {
        "id": j.id, "name": j.name, "description": j.description,
        "cron_expression": j.cron_expression, "job_type": j.job_type,
        "status": j.status, "enabled": j.enabled,
        "last_run": j.last_run.isoformat() if j.last_run else None,
        "next_run": j.next_run.isoformat() if j.next_run else None,
        "last_run_duration_ms": j.last_run_duration_ms,
        "last_error": j.last_error, "run_count": j.run_count, "fail_count": j.fail_count,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# MODULE 7 — REPORTING
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports")
async def list_reports(
    page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    q = db.query(Report)
    total = q.count()
    reports = q.order_by(desc(Report.created_at)).offset((page - 1) * limit).limit(limit).all()
    return {
        "reports": [_report_to_dict(r) for r in reports],
        "total": total, "page": page, "limit": limit
    }


@app.post("/api/reports/generate", status_code=201)
async def generate_report(
    req: ReportRequest, background_tasks: BackgroundTasks,
    db=Depends(get_db), current_user: AdminUser = Depends(get_current_user)
):
    report = Report(
        type=req.type, format=req.format,
        title=f"{req.type.capitalize()} Report — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
        status="generating", generated_by=current_user.email,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    background_tasks.add_task(_build_report, report.id, req.type, req.format)
    return {"id": report.id, "message": "Report generation started", "status": "generating"}


@app.get("/api/reports/{report_id}/download")
async def download_report(
    report_id: int, db=Depends(get_db),
    current_user: AdminUser = Depends(get_current_user)
):
    report = db.query(Report).filter_by(id=report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status != "completed":
        raise HTTPException(status_code=400, detail="Report not ready yet")
    if not report.file_path or not os.path.exists(report.file_path):
        # Return summary as CSV fallback
        data = json.dumps(report.summary or {}, indent=2)
        return StreamingResponse(
            io.BytesIO(data.encode()),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=report_{report_id}.json"}
        )
    return FileResponse(
        report.file_path,
        filename=os.path.basename(report.file_path),
        media_type="application/octet-stream",
    )


def _build_report(report_id: int, report_type: str, fmt: str):
    db = SessionLocal()
    try:
        report = db.query(Report).filter_by(id=report_id).first()
        if not report:
            return
        now = datetime.utcnow()
        since = now - timedelta(days=7 if report_type == "weekly" else 30)
        summary = json.loads(_generate_report_data(report_type if report_type in ("weekly","monthly") else "weekly", db))

        if fmt == "csv":
            file_path = os.path.join(REPORTS_DIR, f"report_{report_id}.csv")
            with open(file_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Metric", "Value"])
                for k, v in summary.items():
                    w.writerow([k, v])
        elif fmt == "excel":
            try:
                import openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Report"
                ws.append(["Metric", "Value"])
                for k, v in summary.items():
                    ws.append([str(k), str(v)])
                file_path = os.path.join(REPORTS_DIR, f"report_{report_id}.xlsx")
                wb.save(file_path)
            except Exception:
                file_path = None
        else:
            file_path = None

        report.status = "completed"
        report.summary = summary
        report.file_path = file_path
        report.file_size_bytes = os.path.getsize(file_path) if file_path and os.path.exists(file_path) else None
        db.commit()
    except Exception as e:
        report.status = "failed"
        db.commit()
        logger.error(f"Report {report_id} failed: {e}")
    finally:
        db.close()


def _report_to_dict(r: Report) -> dict:
    return {
        "id": r.id, "type": r.type, "format": r.format, "title": r.title,
        "status": r.status, "summary": r.summary,
        "file_size_bytes": r.file_size_bytes, "generated_by": r.generated_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD — Serve the HTML UI
# ════════════════════════════════════════════════════════════════════════════

@app.get("/admin", response_class=HTMLResponse)
async def serve_admin_dashboard():
    """Serve the premium admin dashboard HTML."""
    try:
        with open("admin_dashboard.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Dashboard not found. Deploy admin_dashboard.html</h1>", status_code=404)
