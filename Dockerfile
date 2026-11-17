FROM node:22-alpine@sha256:e4bf2a82ad0a4037d28035ae71529873c069b13eb0455466ae0bc13363826e34 AS webui-builder

WORKDIR /src/webui

COPY webui/package.json webui/package-lock.json /src/webui/
RUN npm ci

RUN mkdir -p /src/app
COPY webui/ /src/webui/
RUN npm run build


FROM python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY config.yaml /app/config.yaml
COPY app /app/app
COPY --from=webui-builder /src/app/ui2 /app/app/ui2
COPY scripts /app/scripts
RUN sed -i 's/\r$//' /app/scripts/entrypoint.sh && chmod +x /app/scripts/entrypoint.sh

RUN mkdir -p "/app/data" && chown -R appuser:appuser "/app"
# NOTE:
# Some platforms mount persistent volumes as root-owned and not writable by non-root users.
# Running as root by default avoids SQLite "unable to open database file" on such platforms.
# If you want to run as non-root, set a Kubernetes securityContext (runAsUser/fsGroup) or
# build a custom image.
USER root
EXPOSE 8000

# 健康检查以 /readyz 为准（校验 admin token / master key / 数据库就绪）；/healthz 仅表示进程存活。
# 基础镜像为 python:3.11-slim，无 curl/wget，因此使用标准库探测。
# 注意：docker-compose.yml 中各服务声明的 healthcheck 会覆盖此处默认值（控制面 8001 端口场景）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/readyz', timeout=4).status == 200 else 1)"

CMD ["/bin/sh", "/app/scripts/entrypoint.sh"]
