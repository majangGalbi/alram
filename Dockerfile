FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py time_parser.py ./

# root 가 아닌 사용자로 실행
RUN useradd --create-home --uid 10001 appuser
USER appuser

# keep-alive 웹서버(Flask) 포트. 실제 포트는 $PORT 환경변수로 결정됨
EXPOSE 8080

# Procfile 의 web 프로세스(`python bot.py`)와 동일한 명령
CMD ["python", "bot.py"]
