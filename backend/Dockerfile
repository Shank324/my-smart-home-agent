# 基础镜像与 pip 源均可用 build-arg 覆盖，方便在无法直连 Docker Hub / PyPI 的网络里构建：
#   docker compose build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

# build context 是仓库根目录，不是 backend/
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r backend/requirements.txt

# 再拷代码：backend 包、app 包（知识库）、种子文档
COPY backend ./backend
COPY app ./app
COPY seed_data ./seed_data

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
