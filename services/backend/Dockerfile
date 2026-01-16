FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
# 后端库很小，直接用清华源加速即可
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .

# 启动 Uvicorn 服务器
# --reload 模式可以在你修改代码时自动重启服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]