FROM python:3.11-slim

WORKDIR /app

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Создание директорий с правами
RUN mkdir -p /app/data /app/config && chmod 777 /app/data

# Копирование кода
COPY app/ ./app/
COPY config/ ./config/

# Переменные окружения
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.main
ENV FLASK_ENV=production

# Порт
EXPOSE 8080

# Entrypoint будет задан в docker-compose
