#!/bin/bash

set -e

# Конфигурация
INSTALL_DIR="/opt/3x-controller"
PROJECT_NAME="3x-controller"
CONFIG_FILE="$INSTALL_DIR/.env"
REPO_URL="https://github.com/ADSFAL-US/x-controller.git"

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
    
    # Клонируем репозиторий
    if [ -d "$INSTALL_DIR/.git" ]; then
        log_info "Репозиторий уже существует, обновляем..."
        cd "$INSTALL_DIR"
        git pull origin master
    else
        log_info "Клонирование репозитория..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    
    cd "$INSTALL_DIR"
    
    # Запрашиваем порт
    read -p "Enter port for controller [8080]: " user_port
    CONTROLLER_PORT=${user_port:-8080}
    
    # Запрашиваем логин и пароль администратора
    read -p "Enter admin username [admin]: " admin_user
    ADMIN_USERNAME=${admin_user:-admin}
    
    read -s -p "Enter admin password (will be hidden): " admin_pass
    echo
    if [ -z "$admin_pass" ]; then
        admin_pass=$(openssl rand -hex 16 2>/dev/null || head -c 32 /dev/urandom | xxd -p | head -c 32)
        log_warning "Password not provided, generated random: $admin_pass"
        echo "Please save this password!"
    fi
    
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
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_PASSWORD=$admin_pass
EOF
        log_success ".env создан (port: $CONTROLLER_PORT, user: $ADMIN_USERNAME)"
    else
        # Обновляем только порт если файл существует
        sed -i "s/^CONTROLLER_PORT=.*/CONTROLLER_PORT=$CONTROLLER_PORT/" "$INSTALL_DIR/.env" 2>/dev/null || \
            echo "CONTROLLER_PORT=$CONTROLLER_PORT" >> "$INSTALL_DIR/.env"
        log_success "Port updated to: $CONTROLLER_PORT"
    fi
    
    # Создаем config директорию с примером если нет
    if [ ! -f "$INSTALL_DIR/config/panels.yaml" ]; then
        mkdir -p "$INSTALL_DIR/config"
        cat > "$INSTALL_DIR/config/panels.yaml" << 'EOF'
panels:
  - name: panel-1
    host: http://localhost:2053
    panel_path: ''
    sub_path: /sub
    username: admin
    password: admin
    priority: 1
    max_clients: 100

  # Пример с секретным путем и отдельным хостом для подписок:
  # - name: panel-2
  #   host: https://panel.example.com:2053
  #   panel_path: /secret-path
  #   sub_host: https://sub.example.com:8080
  #   sub_path: /avava-vpn
  #   username: admin
  #   password: secret
  #   priority: 2
  #   max_clients: 200
EOF
        log_info "Создан пример config/panels.yaml - отредактируйте под ваши панели"
    fi
    
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
    
    # Обновляем из репозитория
    if [ -d "$INSTALL_DIR/.git" ]; then
        log_info "Обновление из репозитория..."
        git pull origin master
    else
        log_warning "Не найден git репозиторий, пропускаем обновление кода"
    fi
    
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
