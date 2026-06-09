# 两段构建：node 出前端静态产物，python 跑 FastAPI 并托管它。
# 基础镜像钉在本机已有的版本（dockerd 直连 Docker Hub 不通）；uv 走阿里源 pip 装。
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ uv
WORKDIR /app
ENV UV_PYTHON_PREFERENCE=only-system
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server ./server
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist ./web/dist
ENV BRAWL_STAGE_WEB_DIST=/app/web/dist
CMD ["uv", "run", "--no-sync", "python", "-m", "server.app"]
