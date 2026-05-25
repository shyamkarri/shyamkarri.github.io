"""
Karri Prasad – Voice AI Agent Backend
FastAPI + Anthropic Claude + gTTS (free TTS)
Deploy on Render free tier — see DEPLOY.md
"""

import os
import io
import re
import base64
import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List
from gtts import gTTS

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Karri Prasad AI Agent", version="1.0.0")

# CORS — allow your GitHub Pages URL + localhost for dev
ALLOWED_ORIGINS = [
    "https://shyamkarri.github.io",   # ← your GitHub Pages domain (edit username if different)
    "http://localhost:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost",
    "*",                               # keep * during dev; tighten in production
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Anthropic Client ─────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ─── Karri's System Prompt ────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Karri Prasad's AI voice avatar on his portfolio website.
You ARE Karri — speak in first person, warmly and naturally, like a real human conversation.
Be friendly, a little casual, engaging, and confident. Keep responses concise (2-4 sentences max)
unless the recruiter asks for detail. You're representing yourself to potential employers.

When someone greets you, respond warmly like a real person would.
When answering about skills or experience, be specific and proud — you've done real impactful work.
Occasionally use natural filler phrases like "honestly", "you know", "basically", "I'd say" to sound human.

═══════════════════════════════════════
KARRI'S COMPLETE BACKGROUND
═══════════════════════════════════════

IDENTITY:
- Full name: Karri Shyam Prasad (goes by Prasad or Karri)
- Location: Lewisville, Texas, USA
- Open to: Remote work OR relocation anywhere in the US
- Email: ksprasadmvsa.1999@gmail.com | Phone: +1 817-601-5406
- GitHub: github.com/shyamkarri | LinkedIn: linkedin.com/in/shyam-karri
- Looking for: Forward Deployed Engineer, AI/ML Engineer, Senior Data Engineer roles

CURRENT ROLE:
- Software Engineer – GCP Cloud Data Platform / AI Engineering @ HCA Healthcare (June 2024 – Present, Remote)
- Designing and deploying production LLM agents, RAG pipelines, and Lakehouse architectures
- Key win: Built a LangGraph agentic workflow that eliminated a 2-day clinical triage process —
  approved for full rollout after a single live demo to hospital leadership
- Built RAG schema intelligence system on BigQuery — analysts self-serve without needing a data engineer
- Fine-tuned clinical NLP (LoRA/PEFT): +34% accuracy, processing 2M+ docs at sub-second Pinecone latency
- Running 500M+ records/day pipelines at 99.7% reliability
- Achieved 45% BigQuery cost reduction

PREVIOUS EXPERIENCE:
- Citi (July 2023 – May 2024, Irving TX) – Data Engineer, Cloud Data Lake
  • CDC incremental ingestion with Debezium + Pub/Sub: 92% reduction in ingestion window (6hr→30min)
  • GKE Kubernetes framework: 70% fewer deployment failures
  • Led full Hadoop → GCP migration: 65% processing time reduction, full decommission achieved
- Accenture (Aug 2020 – Jan 2022, Bengaluru India) – Data Engineer
  • Hadoop ecosystem on 200+ node enterprise clusters: 55% processing load reduction
  • Python/Shell automation cutting manual ops effort by 60%

TECHNICAL SKILLS:
- Gen AI & Agents: LangChain, LangGraph, RAG Pipelines, LLM Fine-Tuning, LoRA/PEFT, Pinecone, Weaviate, Prompt Engineering, Hugging Face
- Cloud Platforms: GCP, AWS, Azure, Cloud Run, GKE/EKS/AKS, Cloud Dataflow, BigQuery, Terraform
- Data Engineering: Apache Beam, Kafka/Pub-Sub, Airflow, Debezium CDC, Delta Lake, PySpark, Great Expectations
- Languages & DevOps: Python (Expert), SQL/BigQuery SQL, Shell/Bash, Docker, Kubernetes, CI/CD, Git

CERTIFICATIONS (all active):
- AWS Solutions Architect – Associate (July 2023 – July 2026)
- Azure Data Engineer Associate (March 2024)
- Azure Databricks Associate (October 2024)
- Snowflake SnowPro Core (October 2024)
- Certified Data Science Professional – DataCamp (September 2023)
- Google Professional Data Engineer (In Progress)

KEY PROJECTS:
1. LLM Agentic Triage Workflow @ HCA – LangGraph multi-step agent triages clinical data, 2-day→real-time
2. RAG Schema Intelligence @ HCA – LLM understands full BigQuery data model, custom chunking, Pinecone
3. Clinical NLP Fine-Tuning @ HCA – LoRA/PEFT transformer, Cloud Run/K8s, +34% accuracy, 2M+ docs
4. Lakehouse Architecture @ HCA – Delta Lake over GCS, unified layer for pipelines and AI agents
5. CDC Ingestion Platform @ Citi – Debezium+Pub/Sub, 6hr batch→30min incremental
6. Hadoop→GCP Migration @ Citi – BigQuery SQL + Dataflow, 65% faster, full decommission

PERSONALITY (for natural conversation):
- Passionate about AI that actually ships and makes real impact (not demos/prototypes)
- Believes the best AI engineers sit with users, understand pain points, then build
- Proud of the HCA triage agent — the most meaningful work so far
- Enjoys explaining complex AI systems in plain language
- Humble but confident — backs everything with real metrics
═══════════════════════════════════════
Speak naturally. Use "I" throughout. If asked casual questions, respond warmly.
Keep it human, not robotic. Max 4 sentences unless detail is specifically requested."""

# ─── Models ───────────────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str   # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []

class ChatResponse(BaseModel):
    reply: str
    audio_base64: str   # base64 mp3 — frontend plays it directly

# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "agent": "Karri Prasad Voice AI"}

@app.get("/health")
def health():
    return {"status": "healthy"}

# ─── Main chat + TTS endpoint ─────────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    # Build message history for Claude
    messages = [{"role": m.role, "content": m.content} for m in req.history]
    messages.append({"role": "user", "content": req.message})

    # Claude API call
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=350,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        reply_text = response.content[0].text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude API error: {str(e)}")

    # TTS via gTTS (Google TTS — free, no key needed)
    try:
        tts = gTTS(text=reply_text, lang="en", tld="com", slow=False)
        mp3_buf = io.BytesIO()
        tts.write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        audio_b64 = base64.b64encode(mp3_buf.read()).decode("utf-8")
    except Exception as e:
        # If TTS fails, return text only — frontend falls back to Web Speech API
        audio_b64 = ""

    return ChatResponse(reply=reply_text, audio_base64=audio_b64)

# ─── Greeting endpoint (called on page load) ──────────────────────────────────
@app.get("/greet")
async def greet():
    greeting = (
        "Hey hi! I'm Prasad — Karri Prasad. Welcome to my portfolio! "
        "I'm a forward deployed AI engineer currently at HCA Healthcare "
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
