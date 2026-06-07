FROM python:3.11 AS base
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS test
COPY app .
RUN pip install pytest pylint
RUN pytest pylint

FROM base AS production
COPY app .
#ENV PYTHONUNBUFFERED=1 # <--- Prevent Python from buffering logs in memory. activated in compose.yaml
CMD ["python", "app.py"]
