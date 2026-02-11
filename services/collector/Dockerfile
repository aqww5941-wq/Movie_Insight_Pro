# ==========================================
# 多阶段构建 Dockerfile - 爬虫服务
# ==========================================

# 使用官方 Playwright Python 镜像
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy as base

WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 阶段 2: 依赖安装
FROM base as dependencies

COPY requirements.txt .

# 安装 Python 依赖（使用清华镜像源加速）
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 阶段 3: 运行环境
FROM base as runtime

WORKDIR /app

# 从依赖阶段复制已安装的包
COPY --from=dependencies /usr/local/lib /usr/local/lib
COPY --from=dependencies /usr/local/bin /usr/local/bin

# 复制应用代码
COPY . .

# ⚠️ 重要：此镜像已包含浏览器，无需运行 playwright install

# 暴露常用端口（如需要）
# EXPOSE 9222

CMD ["scrapy", "list"]
