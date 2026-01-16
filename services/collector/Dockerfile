FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# 声明参数
ARG http_proxy
ARG https_proxy

# 【关键】将参数转为环境变量，供后续 RUN 命令使用
ENV http_proxy=$http_proxy
ENV https_proxy=$https_proxy
ENV no_proxy=$NO_PROXY

COPY requirements.txt .

# 【必须执行】安装 Python 依赖包（包括 scrapy, playwright 等）
RUN pip install --no-cache-dir -r requirements.txt

# 【此时才能执行】有了上面的 pip install，系统才认得 playwright 命令
RUN playwright install chromium

# 复制源码
COPY . .

# 取消代理
ENV http_proxy=""
ENV https_proxy=""

CMD ["tail", "-f", "/dev/null"]