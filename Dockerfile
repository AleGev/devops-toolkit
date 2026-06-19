FROM python:3.11-slim AS base
WORKDIR /app/bot
COPY app/bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS test
COPY app/bot .
RUN pip install pytest pylint
RUN pylint *.py && pytest

FROM base AS production
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser:appuser app/bot .
USER appuser
CMD ["python", "bot.py"]
