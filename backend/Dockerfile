FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt requirements-retrieval.txt constraints.txt ./
# 使用 CPU 推理包，避免默认安装体积较大的 CUDA 运行环境。
RUN pip install --no-cache-dir torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-retrieval.txt
COPY . .
RUN useradd -m agent && mkdir -p /app/data && chown -R agent:agent /app
USER agent
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
