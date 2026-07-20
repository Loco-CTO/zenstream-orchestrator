FROM node:26-slim AS dashboard-build
WORKDIR /frontend
COPY frontend/package.json ./
RUN npm install --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORCHESTRATOR_HOST=0.0.0.0 \
    ORCHESTRATOR_PORT=9088
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./
COPY .main-version.json ./
COPY migrations/ ./migrations/
COPY assets/ ./assets/
COPY orchestrator/ ./orchestrator/
COPY --from=dashboard-build /frontend/out/ ./orchestrator/web/
EXPOSE 9088
CMD ["python", "orchestrator/init.py"]
