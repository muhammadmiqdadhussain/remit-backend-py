FROM python:3.10-slim

WORKDIR /app

# Copy app first so requirements check works even when requirements.txt is absent.
COPY . .

# Install dependencies from requirements.txt if present, otherwise install minimal runtime deps.
RUN if [ -f "requirements.txt" ]; then \
      pip install --no-cache-dir -r requirements.txt; \
    else \
      pip install --no-cache-dir fastapi uvicorn httpx; \
    fi

EXPOSE 8005

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005", "--reload"]
