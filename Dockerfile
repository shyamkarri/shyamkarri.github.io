FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY . .

# Run the build_index.py script during the docker build phase.
# This will download the HF model into /app/.cache and create the FAISS index in /app/faiss_index
RUN python build_index.py

# Expose port (Render sets the PORT environment variable)
# 10000 is Render's default fallback if $PORT is not set
EXPOSE 10000

# Start the application
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
