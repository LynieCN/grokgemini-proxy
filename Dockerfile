FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用文件
COPY app.py .

# 暴露端口
EXPOSE 7860

# 运行应用
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
