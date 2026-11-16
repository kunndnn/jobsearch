FROM python:3.11-slim

# System dependencies for curl_cffi and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and prebuilt frontend
COPY backend/ backend/
COPY jobspy/ jobspy/
COPY frontend/dist/ frontend/dist/
COPY start.py scrape.py main.py ./

# Default environment variables
ENV JOBSPY_DATA_DIR=/app/data
ENV PORT=8000

# Create data directory
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "start.py", "--host", "0.0.0.0", "--no-browser"]