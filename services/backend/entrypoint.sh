#!/bin/bash

# 遇到任何问题直接退出
set -e

# 1.自动运行迁移
echo "Running database migrations..."
alembic upgrade head

# 这里的 "$@" 代表接收来自 CMD 的所有参数
exec "$@"