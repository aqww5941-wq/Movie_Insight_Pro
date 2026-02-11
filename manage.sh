#!/bin/bash

# ==========================================
# 项目管理脚本
# Movie Insight Pro - 一键管理工具
# ==========================================

set -e

PROJECT_NAME="Movie Insight Pro"
COMPOSE_FILE="docker compose.yml"
ENV_FILE=".env"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查环境文件
check_env() {
    if [ ! -f "$ENV_FILE" ]; then
        print_warning ".env 文件不存在，正在从模板创建..."
        if [ -f ".env.example" ]; then
            cp .env.example .env
            print_info "请编辑 .env 文件配置必要的环境变量"
            exit 1
        else
            print_error ".env.example 文件也不存在！"
            exit 1
        fi
    fi
}

# 启动所有服务
start_all() {
    print_info "🚀 启动 $PROJECT_NAME..."
    check_env
    docker compose up -d
    print_success "✅ 所有服务已启动"
    show_status
}

# 停止所有服务
stop_all() {
    print_info "🛑 停止所有服务..."
    docker compose down
    print_success "✅ 所有服务已停止"
}

# 重启所有服务
restart_all() {
    print_info "🔄 重启所有服务..."
    docker compose restart
    print_success "✅ 所有服务已重启"
}

# 查看服务状态
show_status() {
    print_info "📊 服务状态："
    docker compose ps
}

# 查看日志
show_logs() {
    if [ -z "$1" ]; then
        docker compose logs -f --tail=100
    else
        docker compose logs -f --tail=100 "$1"
    fi
}

# 构建镜像
build_images() {
    print_info "🔨 构建 Docker 镜像..."
    docker compose build --no-cache
    print_success "✅ 镜像构建完成"
}

# 清理未使用的资源
cleanup() {
    print_warning "⚠️  即将清理未使用的 Docker 资源..."
    read -p "确认继续？(y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker system prune -f
        docker volume prune -f
        print_success "✅ 清理完成"
    else
        print_info "取消清理操作"
    fi
}

# 备份数据库
backup_database() {
    print_info "💾 开始备份数据库..."
    
    BACKUP_DIR="./backups"
    mkdir -p "$BACKUP_DIR"
    
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="$BACKUP_DIR/postgres_backup_$TIMESTAMP.sql"
    
    docker compose exec -T db pg_dump -U root movie_db > "$BACKUP_FILE"
    
    if [ $? -eq 0 ]; then
        print_success "✅ 数据库备份成功: $BACKUP_FILE"
        
        # 压缩备份文件
        gzip "$BACKUP_FILE"
        print_success "✅ 备份文件已压缩: ${BACKUP_FILE}.gz"
        
        # 保留最近30天的备份
        find "$BACKUP_DIR" -name "postgres_backup_*.gz" -mtime +30 -delete
        print_info "已清理30天前的旧备份"
    else
        print_error "❌ 数据库备份失败"
        exit 1
    fi
}

# 恢复数据库
restore_database() {
    if [ -z "$1" ]; then
        print_error "请指定备份文件路径"
        echo "用法: $0 restore <backup_file>"
        exit 1
    fi
    
    BACKUP_FILE="$1"
    
    if [ ! -f "$BACKUP_FILE" ]; then
        print_error "备份文件不存在: $BACKUP_FILE"
        exit 1
    fi
    
    print_warning "⚠️  即将恢复数据库，当前数据将被覆盖！"
    read -p "确认继续？(y/N): " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_info "正在恢复数据库..."
        
        # 如果是压缩文件，先解压
        if [[ "$BACKUP_FILE" == *.gz ]]; then
            gunzip -c "$BACKUP_FILE" | docker compose exec -T db psql -U root movie_db
        else
            cat "$BACKUP_FILE" | docker compose exec -T db psql -U root movie_db
        fi
        
        if [ $? -eq 0 ]; then
            print_success "✅ 数据库恢复成功"
        else
            print_error "❌ 数据库恢复失败"
            exit 1
        fi
    else
        print_info "取消恢复操作"
    fi
}

# 进入容器
enter_container() {
    if [ -z "$1" ]; then
        print_error "请指定容器名称"
        echo "可用容器: backend, collector, consumer, db, redis, rabbitmq, nginx"
        exit 1
    fi
    
    print_info "进入容器: $1"
    docker compose exec "$1" /bin/bash || docker compose exec "$1" /bin/sh
}

# 查看健康状态
health_check() {
    print_info "🏥 健康检查..."
    
    # 检查后端 API
    if curl -sf http://localhost/health > /dev/null; then
        print_success "✅ 后端 API 正常"
    else
        print_error "❌ 后端 API 异常"
    fi
    
    # 检查数据库
    if docker compose exec -T db pg_isready -U root > /dev/null 2>&1; then
        print_success "✅ PostgreSQL 正常"
    else
        print_error "❌ PostgreSQL 异常"
    fi
    
    # 检查 Redis
    if docker compose exec -T redis redis-cli ping > /dev/null 2>&1; then
        print_success "✅ Redis 正常"
    else
        print_error "❌ Redis 异常"
    fi
    
    # 检查 RabbitMQ
    if docker compose exec -T rabbitmq rabbitmq-diagnostics -q ping > /dev/null 2>&1; then
        print_success "✅ RabbitMQ 正常"
    else
        print_error "❌ RabbitMQ 异常"
    fi
}

# 更新项目
update_project() {
    print_info "📦 更新项目..."
    
    # 拉取最新代码
    if [ -d ".git" ]; then
        git pull
    fi
    
    # 重新构建
    build_images
    
    # 重启服务
    restart_all
    
    print_success "✅ 项目更新完成"
}

# 显示使用帮助
show_help() {
    echo ""
    echo "🎬 $PROJECT_NAME - 管理脚本"
    echo ""
    echo "用法: $0 <command> [options]"
    echo ""
    echo "可用命令:"
    echo "  start           启动所有服务"
    echo "  stop            停止所有服务"
    echo "  restart         重启所有服务"
    echo "  status          查看服务状态"
    echo "  logs [service]  查看日志（可指定服务名）"
    echo "  build           构建 Docker 镜像"
    echo "  cleanup         清理未使用的 Docker 资源"
    echo "  backup          备份数据库"
    echo "  restore <file>  恢复数据库"
    echo "  enter <service> 进入容器"
    echo "  health          健康检查"
    echo "  update          更新项目"
    echo "  help            显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 start                    # 启动所有服务"
    echo "  $0 logs backend             # 查看后端日志"
    echo "  $0 enter db                 # 进入数据库容器"
    echo "  $0 backup                   # 备份数据库"
    echo "  $0 restore backups/xxx.gz   # 恢复数据库"
    echo ""
}

# 主逻辑
case "${1:-}" in
    start)
        start_all
        ;;
    stop)
        stop_all
        ;;
    restart)
        restart_all
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs "$2"
        ;;
    build)
        build_images
        ;;
    cleanup)
        cleanup
        ;;
    backup)
        backup_database
        ;;
    restore)
        restore_database "$2"
        ;;
    enter)
        enter_container "$2"
        ;;
    health)
        health_check
        ;;
    update)
        update_project
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "未知命令: ${1:-}"
        show_help
        exit 1
        ;;
esac
