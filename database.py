import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime,
    Float, JSON, Boolean, ForeignKey, inspect, text, Enum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import enum

# ─── Declarative Base ────────────────────────────────────────────────────────
Base = declarative_base()

# ─── Enums ───────────────────────────────────────────────────────────────────
class ApplicationStatus(str, enum.Enum):
    saved = "saved"
    applied = "applied"
    screening = "screening"
    interview = "interview"
    final_round = "final_round"
    offer = "offer"
    rejected = "rejected"

class NotificationType(str, enum.Enum):
    recruiter_email = "recruiter_email"
    interview_invite = "interview_invite"
    job_match = "job_match"
    weekly_report = "weekly_report"
    system = "system"

class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    disabled = "disabled"

class ReportType(str, enum.Enum):
    weekly = "weekly"
    monthly = "monthly"
    agent = "agent"
    job_search = "job_search"
    application = "application"

class ReportFormat(str, enum.Enum):
    pdf = "pdf"
    csv = "csv"
    excel = "excel"

class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"

# ─── Models ──────────────────────────────────────────────────────────────────

class AdminUser(Base):
    """Dashboard admin user with JWT-based auth."""
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(512), nullable=False)
    name = Column(String(255), nullable=True)
    role = Column(String(50), default="admin", nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


class ConversationLog(Base):
    """Existing session/message log — preserved as-is."""
    __tablename__ = "conversation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    session_id = Column(String(255), index=True, nullable=False)
    user_id = Column(String(255), index=True, nullable=True)
    user_name = Column(String(255), nullable=True)
    user_message = Column(Text, nullable=True)
    assistant_response = Column(Text, nullable=True)
    request_duration = Column(Float, nullable=True)
    model_used = Column(String(255), nullable=True)
    token_usage = Column(JSON, nullable=True)
    error_messages = Column(Text, nullable=True)


class AgentExecution(Base):
    """Track individual agent/LLM executions with analytics."""
    __tablename__ = "agent_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    agent_name = Column(String(255), nullable=False, index=True)
    workflow = Column(String(255), nullable=True)
    session_id = Column(String(255), nullable=True, index=True)
    input_text = Column(Text, nullable=True)
    output_text = Column(Text, nullable=True)
    latency_ms = Column(Float, nullable=True)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    tool_used = Column(String(255), nullable=True)
    model_used = Column(String(255), nullable=True)
    token_usage = Column(JSON, nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)


class JobApplication(Base):
    """Job application tracker for the kanban board."""
    __tablename__ = "job_applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    company_name = Column(String(255), nullable=False, index=True)
    position = Column(String(255), nullable=False)
    application_date = Column(DateTime, nullable=True)
    status = Column(String(50), default="saved", nullable=False, index=True)
    notes = Column(Text, nullable=True)
    job_url = Column(String(512), nullable=True)
    salary_range = Column(String(100), nullable=True)
    location = Column(String(255), nullable=True)
    remote = Column(Boolean, default=False)
    contact_name = Column(String(255), nullable=True)
    contact_email = Column(String(255), nullable=True)
    # Interview stages stored as JSON list
    interview_stages = Column(JSON, nullable=True)  # [{stage, date, notes, passed}]
    # Offer details
    offer_amount = Column(String(100), nullable=True)
    offer_deadline = Column(DateTime, nullable=True)
    priority = Column(Integer, default=0)  # 0=normal, 1=high, 2=urgent


class JobPosting(Base):
    """Jobs discovered by the scraper from public job-board APIs."""
    __tablename__ = "job_postings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scraped_at = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String(50), nullable=False, index=True)      # greenhouse / lever / ashby / smartrecruiters / workday
    external_id = Column(String(255), nullable=False)            # platform-specific job id
    company = Column(String(255), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    location = Column(String(512), nullable=True)
    url = Column(String(1024), nullable=False)
    description_snippet = Column(Text, nullable=True)            # first ~2000 chars for keyword flags
    posted_at = Column(DateTime, nullable=True)
    remote = Column(Boolean, default=False)
    # Work-auth signals found in the description
    sponsorship_flag = Column(String(50), nullable=True)         # "friendly" / "restricted" / "unknown"
    matched_keywords = Column(JSON, nullable=True)               # which search terms matched
    is_new = Column(Boolean, default=True, index=True)           # unseen in dashboard
    tracked_application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=True)
    # AI match scoring (filled in by job_intel after each scrape)
    match_score = Column(Integer, nullable=True, index=True)     # 0-100 fit vs profile
    match_reason = Column(Text, nullable=True)                   # one-line explanation


class Notification(Base):
    """Notification center — stores alerts, summaries, reports ready."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    type = Column(String(50), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    message = Column(Text, nullable=True)
    is_read = Column(Boolean, default=False, index=True)
    data = Column(JSON, nullable=True)       # extra payload (application_id, etc.)
    email_sent = Column(Boolean, default=False)
    user_email = Column(String(255), nullable=True)


class SchedulerJob(Base):
    """Cron job definitions and execution history."""
    __tablename__ = "scheduler_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    cron_expression = Column(String(100), nullable=True)
    job_type = Column(String(100), nullable=False)   # "weekly_report", "sync", etc.
    status = Column(String(50), default="pending", index=True)
    enabled = Column(Boolean, default=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    last_run_duration_ms = Column(Float, nullable=True)
    last_error = Column(Text, nullable=True)
    run_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    config = Column(JSON, nullable=True)


class SchedulerJobRun(Base):
    """Individual run history for each scheduler job."""
    __tablename__ = "scheduler_job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("scheduler_jobs.id"), nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(50), nullable=False)
    duration_ms = Column(Float, nullable=True)
    output = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    job = relationship("SchedulerJob", backref="runs")


class Report(Base):
    """Generated report records."""
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    type = Column(String(50), nullable=False, index=True)
    format = Column(String(20), nullable=False)
    title = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    status = Column(String(50), default="pending")
    summary = Column(JSON, nullable=True)    # key metrics included in report
    generated_by = Column(String(255), nullable=True)


# ─── Database Connection ──────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Render provides "postgres://", SQLAlchemy needs "postgresql://"
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
else:
    print(
        "[DB] ⚠️  WARNING: DATABASE_URL is not set — falling back to local SQLite "
        "(sessions.db). On Render the filesystem is EPHEMERAL: every deploy or "
        "restart WIPES this file, and all conversation logs, applications, and "
        "job data are LOST. Set DATABASE_URL to a durable Postgres instance "
        "(e.g. Neon or Supabase free tier) to keep data permanently."
    )
    engine = create_engine(
        "sqlite:///sessions.db",
        # timeout: wait up to 30s for locks instead of instantly erroring
        # while the scraper thread is writing
        connect_args={"check_same_thread": False, "timeout": 30}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all tables. Safe to run multiple times (CREATE IF NOT EXISTS)."""
    inspector = inspect(engine)

    # Create any missing tables
    Base.metadata.create_all(bind=engine)

    # ── Lightweight migration: add any missing columns to evolving tables ──
    for model in (ConversationLog, JobPosting):
        table_name = model.__tablename__
        if not inspector.has_table(table_name):
            continue
        existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
        with engine.begin() as conn:
            for column in model.__table__.columns:
                if column.name not in existing_cols:
                    type_str = str(column.type)
                    if "JSON" in type_str:
                        type_str = "JSON" if "postgresql" in engine.url.drivername else "TEXT"
                    try:
                        conn.execute(text(
                            f"ALTER TABLE {table_name} ADD COLUMN {column.name} {type_str}"
                        ))
                        print(f"[DB] Added column '{column.name}' to {table_name}")
                    except Exception as e:
                        print(f"[DB] Column '{column.name}' migration skipped: {e}")

    print("[DB] All tables initialized.")


def seed_db():
    """Insert default seed data (scheduler jobs, admin user placeholder)."""
    from passlib.context import CryptContext
    pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

    db = SessionLocal()
    try:
        # Seed default admin user if not present
        admin_email = os.getenv("ADMIN_EMAIL", "admin@karriprasad.ai")
        admin_password = os.getenv("ADMIN_PASSWORD", "changeme123")
        if not db.query(AdminUser).filter_by(email=admin_email).first():
            db.add(AdminUser(
                email=admin_email,
                # bcrypt max is 72 bytes — truncate to be safe
                hashed_password = pwd_ctx.hash(admin_password[:72]),
                name="Admin",
                role="admin",
            ))
            print(f"[DB] Seeded admin user: {admin_email}")

        # Seed default scheduler jobs
        default_jobs = [
            {
                "name": "weekly_report",
                "description": "Generate weekly analytics report every Monday at 8 AM",
                "cron_expression": "0 8 * * 1",
                "job_type": "weekly_report",
                "enabled": True,
            },
            {
                "name": "gmail_auto_responder",
                "description": "Check Gmail for job/recruiter emails and auto-reply every 10 minutes",
                "cron_expression": "*/10 * * * *",
                "job_type": "gmail_auto_responder",
                "enabled": True,
            },
            {
                "name": "daily_notification_digest",
                "description": "Send daily summary notification at 9 AM",
                "cron_expression": "0 9 * * *",
                "job_type": "daily_digest",
                "enabled": True,
            },
            {
                "name": "monthly_report",
                "description": "Generate monthly report on 1st of each month",
                "cron_expression": "0 7 1 * *",
                "job_type": "monthly_report",
                "enabled": True,
            },
            {
                "name": "job_scrape",
                "description": "Scrape Greenhouse/Lever/Ashby/SmartRecruiters/Workday for data-engineering roles every 6 hours",
                "cron_expression": "0 */6 * * *",
                "job_type": "job_scrape",
                "enabled": True,
            },
            {
                "name": "followup_reminder",
                "description": "Flag applications quiet for 5+ days and email a follow-up digest, daily at 8:30 AM",
                "cron_expression": "30 8 * * *",
                "job_type": "followup_reminder",
                "enabled": True,
            },
            {
                "name": "morning_briefing",
                "description": "Daily 8 AM email: top-scored new jobs, follow-ups due, pipeline stats",
                "cron_expression": "0 8 * * *",
                "job_type": "morning_briefing",
                "enabled": True,
            },
        ]
        for job_data in default_jobs:
            if not db.query(SchedulerJob).filter_by(name=job_data["name"]).first():
                db.add(SchedulerJob(**job_data))
                print(f"[DB] Seeded scheduler job: {job_data['name']}")

        db.commit()
    finally:
        db.close()
