#!/bin/bash

set -e

# Конфигурация
INSTALL_DIR="/opt/3x-controller"
PROJECT_NAME="3x-controller"
CONFIG_FILE="$INSTALL_DIR/.env"

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Проверка зависимостей
check_dependencies() {
    log_info "Проверка зависимостей..."
    
    local missing_deps=()
    
    if ! command -v docker &> /dev/null; then
        missing_deps+=("docker")
    else
        log_success "Docker: $(docker --version)"
    fi
    
    if ! docker compose version &> /dev/null 2>&1 && ! command -v docker-compose &> /dev/null; then
        missing_deps+=("docker-compose")
    else
        log_success "Docker Compose: OK"
    fi
    
    if ! command -v git &> /dev/null; then
        missing_deps+=("git")
    else
        log_success "Git: $(git --version)"
    fi
    
    if [ ${#missing_deps[@]} -ne 0 ]; then
        log_error "Отсутствуют: ${missing_deps[*]}"
        echo "  sudo apt update && sudo apt install -y docker.io docker-compose-plugin git"
        exit 1
    fi
}

# Проверка root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Требуется root (sudo)"
        exit 1
    fi
}

# Установка
install_new() {
    log_info "Новая установка в $INSTALL_DIR"
    
    mkdir -p "$INSTALL_DIR"
    
    # Копируем текущие файлы (для локального деплоя)
    # В проде здесь будет git clone
    log_info "Копирование файлов..."
    
    # Запрашиваем порт
    read -p "Enter port for controller [8080]: " user_port
    CONTROLLER_PORT=${user_port:-8080}
    
    # Проверяем что порт свободен
    if netstat -tuln 2>/dev/null | grep -q ":$CONTROLLER_PORT " || ss -tuln 2>/dev/null | grep -q ":$CONTROLLER_PORT "; then
        log_warning "Порт $CONTROLLER_PORT уже занят!"
        read -p "Продолжить anyway? [y/N]: " confirm
        [[ $confirm =~ ^[Yy]$ ]] || exit 1
    fi
    
    # Создаем .env если нет
    if [ ! -f "$INSTALL_DIR/.env" ]; then
        cat > "$INSTALL_DIR/.env" << EOF
CONTROLLER_PORT=$CONTROLLER_PORT
SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || head -c 64 /dev/urandom | xxd -p | head -c 64)
EOF
        log_success ".env создан (port: $CONTROLLER_PORT)"
    else
        # Обновляем только порт если файл существует
        sed -i "s/^CONTROLLER_PORT=.*/CONTROLLER_PORT=$CONTROLLER_PORT/" "$INSTALL_DIR/.env" 2>/dev/null || \
            echo "CONTROLLER_PORT=$CONTROLLER_PORT" >> "$INSTALL_DIR/.env"
        log_success "Port updated to: $CONTROLLER_PORT"
    fi
    
    cd "$INSTALL_DIR"
    
    log_info "Сборка..."
    docker compose build --no-cache
    
    log_info "Запуск..."
    docker compose up -d
    
    log_success "Готово! http://localhost:$CONTROLLER_PORT"
}

# Обновление
update_existing() {
    log_info "Обновление существующей установки"
    
    cd "$INSTALL_DIR"
    
    log_info "Остановка..."
    docker compose down
    
    # TODO: git pull если есть репозиторий
    
    log_info "Пересборка..."
    docker compose build --no-cache
    
    log_info "Запуск..."
    docker compose up -d
    
    docker image prune -f
    
    log_success "Обновлено!"
}

# Главная функция
main() {
    echo "========================================"
    echo "  3x-controller Installer"
    echo "========================================"
    
    check_root
    check_dependencies
    
    if [ -d "$INSTALL_DIR" ] && [ "$(ls -A "$INSTALL_DIR")" ]; then
        update_existing
    else
        install_new
    fi
    
    echo "========================================"
    log_success "Готово!"
    echo "========================================"
}

main "$@"
