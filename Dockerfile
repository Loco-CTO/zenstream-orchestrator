FROM python:3.10-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORCHESTRATOR_HOST=0.0.0.0 \
    ORCHESTRATOR_PORT=9088
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY orchestrator/ ./orchestrator/
EXPOSE 9088 9091
CMD ["python", "orchestrator/init.py"]
