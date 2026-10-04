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
    
    # Создаем локальную конфигурацию панелей из отслеживаемого шаблона
    if [ ! -f "$INSTALL_DIR/config/panels.yaml" ]; then
        mkdir -p "$INSTALL_DIR/config"
        cp "$INSTALL_DIR/config/panels.example.yaml" "$INSTALL_DIR/config/panels.yaml"
        chmod 600 "$INSTALL_DIR/config/panels.yaml"
        log_info "Создан config/panels.yaml из шаблона - отредактируйте под ваши панели"
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

    # Back up local files that would otherwise block this self-update.
    local update_backup=""
    local panel_config_backup=""
    local installer_backup=""
    if { [ -f "$INSTALL_DIR/config/panels.yaml" ] && \
         git ls-files --error-unmatch config/panels.yaml >/dev/null 2>&1; } || \
       ! git diff --quiet HEAD -- install.sh; then
        mkdir -p "$INSTALL_DIR/data"
        update_backup="$(mktemp -d "$INSTALL_DIR/data/update-backup.XXXXXX")"
        chmod 700 "$update_backup"
    fi

    if [ -f "$INSTALL_DIR/config/panels.yaml" ] && \
       git ls-files --error-unmatch config/panels.yaml >/dev/null 2>&1; then
        panel_config_backup="$update_backup/panels.yaml"
    cp -p "$INSTALL_DIR/config/panels.yaml" "$panel_config_backup"
        git show HEAD:config/panels.yaml > "$INSTALL_DIR/config/panels.yaml"
    fi

    if ! git diff --quiet HEAD -- install.sh; then
        installer_backup="$update_backup/install.sh.local"
        cp -p "$INSTALL_DIR/install.sh" "$installer_backup"
        git show HEAD:install.sh > "$INSTALL_DIR/install.sh"
    fi
    
    # Обновляем из репозитория
    if [ -d "$INSTALL_DIR/.git" ]; then
        log_info "Обновление из репозитория..."
        if ! git pull --ff-only origin master; then
            if [ -n "$panel_config_backup" ]; then
                cp -p "$panel_config_backup" "$INSTALL_DIR/config/panels.yaml"
            fi
            if [ -n "$installer_backup" ]; then
                cp -p "$installer_backup" "$INSTALL_DIR/install.sh"
            fi
            if [ -n "$update_backup" ]; then
                rm -f "$update_backup/panels.yaml" "$update_backup/install.sh.local"
                rmdir "$update_backup"
            fi
            return 1
        fi
    else
        log_warning "Не найден git репозиторий, пропускаем обновление кода"
    fi

    if [ -n "$panel_config_backup" ]; then
        cp -p "$panel_config_backup" "$INSTALL_DIR/config/panels.yaml"
    elif [ ! -f "$INSTALL_DIR/config/panels.yaml" ]; then
        cp "$INSTALL_DIR/config/panels.example.yaml" "$INSTALL_DIR/config/panels.yaml"
        chmod 600 "$INSTALL_DIR/config/panels.yaml"
    fi

    log_info "Остановка..."
    docker compose down

    if [ -n "$installer_backup" ]; then
        log_warning "Локальная версия install.sh сохранена для сравнения: $installer_backup"
        if [ -n "$panel_config_backup" ]; then
            rm -f "$panel_config_backup"
        fi
    elif [ -n "$update_backup" ]; then
        if [ -n "$panel_config_backup" ]; then
            rm -f "$panel_config_backup"
        fi
        rmdir "$update_backup"
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
