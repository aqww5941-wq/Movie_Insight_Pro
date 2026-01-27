# 1. 直接拉取 2026 年对应版本的官方镜像
# 注意：通常后缀是 -jammy (Ubuntu 22.04) 或 -noble (Ubuntu 24.04)，
# 如果不确定系统代号，直接用 v1.57.0 即可，Docker 会自动找最新的
FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .

# 2. 安装 Python 依赖
# 你的 requirements.txt 里写着 playwright==1.57.0
# 这个镜像里自带的浏览器内核就是为 1.57.0 准备的
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 复制剩余代码
COPY . .

# ⚠️ 再次强调：这里千万别加 RUN playwright install chromium
# 镜像里已经有了！加了反而会因为网络问题卡死！

CMD ["scrapy", "list"]