# syntax=docker/dockerfile:1

FROM node:26-slim AS dashboard-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && test -f /frontend/out/web/login/index.html

FROM python:3.14-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORCHESTRATOR_HOST=0.0.0.0 \
    ORCHESTRATOR_PORT=9088
COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* && \
    ffmpeg -hide_banner -muxers 2>&1 | grep -q chromaprint && \
    pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./
COPY .main-version.json ./
COPY migrations/ ./migrations/
COPY assets/ ./assets/
RUN mkdir -p ./assets/ffmpeg/linux && \
    cp /usr/bin/ffmpeg ./assets/ffmpeg/linux/ffmpeg && \
    cp /usr/bin/ffprobe ./assets/ffmpeg/linux/ffprobe && \
    ./assets/ffmpeg/linux/ffmpeg -hide_banner -h muxer=chromaprint 2>&1 | grep -q fp_format
COPY orchestrator/ ./orchestrator/
COPY --from=dashboard-build /frontend/out/ ./orchestrator/web/
EXPOSE 9088
CMD ["python", "orchestrator/init.py"]
