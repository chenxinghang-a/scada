FROM python:3.12-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY . .

# 创建运行时目录
RUN mkdir -p logs data exports

# 环境变量默认值
#
# ⚠️ 2026-10-09（round 206）：删掉了 ``ENV SCADA_MODE=simulated`` ——
#    运行模式由 run.py 的**命令行开关**决定（无参=模拟 / --simulator / --real），
#    从来没有代码读 SCADA_MODE。留着它会让运维以为「设 SCADA_MODE=real 就切真实设备」，
#    实际什么都不会发生（而且会以为自己接的是真设备）。
#    SCADA_HOST / SCADA_PORT 此前同样是死变量，现在已在 config.FlaskConfig 里接上 ——
#    尤其 SCADA_HOST=0.0.0.0 是容器能被外部访问的前提（只监听 127.0.0.1 时端口发布打不到）。
ENV SCADA_HOST=0.0.0.0
ENV SCADA_PORT=5000

EXPOSE 5000

# 健康检查走容器内回环，端口跟随 SCADA_PORT（此前写死 5000，
# 一旦运维改了 SCADA_PORT 就会误报不健康）。
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request; p=os.environ.get('SCADA_PORT','5000'); urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health/status')" || exit 1

CMD ["python", "run.py"]
