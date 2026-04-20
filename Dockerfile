FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install chromium --with-deps

COPY . .

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,urllib.request;port=os.getenv('PORT','8080');u=f'http://127.0.0.1:{port}/health';sys.exit(0 if urllib.request.urlopen(u, timeout=5).status==200 else 1)"

CMD ["python", "main.py"]
