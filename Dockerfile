FROM python:3.11 AS base
WORKDIR /app/bot
COPY app/bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS test
COPY app/bot .
RUN pip install pytest pylint
RUN pylint *.py && pytest

FROM base AS production
COPY app/bot .
#ENV PYTHONUNBUFFERED=1 # <--- Prevent Python from buffering logs in memory. activated in compose.yaml
CMD ["python", "bot.py"]
