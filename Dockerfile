FROM node:22-slim AS dashboard-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.10-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORCHESTRATOR_HOST=0.0.0.0 \
    ORCHESTRATOR_PORT=9090
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./
COPY .main-version.json ./
COPY migrations/ ./migrations/
COPY assets/ ./assets/
COPY orchestrator/ ./orchestrator/
COPY --from=dashboard-build /frontend/out/ ./orchestrator/web/
EXPOSE 9090
CMD ["python", "orchestrator/init.py"]
