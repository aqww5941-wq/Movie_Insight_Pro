# test_client.py
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",  # ← 唯一改动
    api_key="any-key"                       # auth 关掉时随便填
)

# 非流式
r = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role":"user","content":"用 10 字回答：什么是 goroutine？"}],
)
print("non-stream:", r.choices[0].message.content)
print("tokens:", r.usage)

# 流式
stream = client.chat.completions.create(
    model="kimi",
    messages=[{"role":"user","content":"写一首五言绝句"}],
    stream=True,
)
print("stream: ", end="", flush=True)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
