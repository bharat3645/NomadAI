# Use official Python image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies for Whisper, gTTS, and audio processing
RUN apt-get update && \
    apt-get install -y ffmpeg libsndfile1 git && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# The EXPOSE instruction is not used by Cloud Run but is good practice.
# It's better to remove it or have it reflect the variable.
# We will remove it to avoid confusion.

# Command to run the FastAPI app.
# This now correctly uses the PORT environment variable provided by Cloud Run.
CMD uvicorn main:app --host 0.0.0.0 --port $PORT

