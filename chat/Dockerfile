FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY MAINTENANCE.md ./MAINTENANCE.md
COPY INTEGRATION.md TESTING.md .
RUN useradd -m -u 1000 app
USER app
EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1", "--no-access-log"]
