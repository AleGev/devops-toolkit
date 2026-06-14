# Stage 1: Base (установка зависимостей)
FROM python:3.11-slim AS base
WORKDIR /app/bot
COPY app/bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Test
FROM base AS test
COPY app/bot/ .
RUN pip install pytest pylint
RUN pylint bot.py && pytest

# Stage 3: Production
FROM base AS production
# Копируем ВСЕ содержимое папки, а не только один файл
COPY app/bot/ .
# Активируйте ENV здесь, чтобы не зависеть от внешних файлов конфигурации
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]