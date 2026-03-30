FROM python:3.10-slim

WORKDIR /app

# Install dependencies if you have a requirements.txt, else install fastapi and uvicorn directly
COPY requirements.txt* ./
RUN if [ -f "requirements.txt" ]; then pip install --no-cache-dir -r requirements.txt; else pip install --no-cache-dir fastapi uvicorn httpx; fi

# Copy application files
COPY . .

# Expose port
EXPOSE 8005

# Start Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005", "--reload"]