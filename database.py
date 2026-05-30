import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, JSON, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Declarative base
Base = declarative_base()

class ConversationLog(Base):
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

# Database Connection Setup
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Render PostgreSQL support
    # Render sometimes provides "postgres://", but SQLAlchemy requires "postgresql://"
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    # Fallback to local SQLite
    engine = create_engine("sqlite:///sessions.db", connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """Initialize DB and perform automatic schema migrations/column additions if necessary."""
    # Check if table exists
    inspector = inspect(engine)
    if not inspector.has_table("conversation_logs"):
        # Create all tables
        Base.metadata.create_all(bind=engine)
        print("Created conversation_logs table.")
    else:
        # Check for missing columns (automatic migrations)
        existing_cols = {col["name"] for col in inspector.get_columns("conversation_logs")}
        # Iterate over defined model columns and add missing ones
        with engine.begin() as conn:
            for column in ConversationLog.__table__.columns:
                if column.name not in existing_cols:
                    # SQLite has limited ALTER TABLE support but adding columns is fine
                    # Define type string based on column type
                    type_str = str(column.type)
                    if "JSON" in type_str:
                        # SQLite does not support JSON column type natively, it uses TEXT or JSON
                        type_str = "JSON" if "postgresql" in engine.url.drivername else "TEXT"
                    
                    alter_query = f'ALTER TABLE conversation_logs ADD COLUMN {column.name} {type_str}'
                    conn.execute(text(alter_query))
                    print(f"Added column {column.name} to conversation_logs table.")
