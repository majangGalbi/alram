FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# bot.py 가 import 하는 모든 로컬 모듈과 달력용 한글 폰트
COPY *.py ./
COPY fonts ./fonts

# root 가 아닌 사용자로 실행. DB 파일은 appuser 가 쓸 수 있는 /app/data 에 저장
# (컨테이너를 지워도 유지하려면 `-v alram-data:/app/data` 로 볼륨 연결)
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data && chown appuser:appuser /app/data
ENV JOBS_DB_PATH=/app/data/jobs.sqlite \
    POLLS_DB_PATH=/app/data/polls.db \
    ATTENDANCE_DB_PATH=/app/data/attendance.db \
    CALENDAR_DB_PATH=/app/data/calendar.db
USER appuser

# keep-alive 웹서버(Flask) 포트. 실제 포트는 $PORT 환경변수로 결정됨
EXPOSE 8080

# Procfile 의 web 프로세스(`python bot.py`)와 동일한 명령
CMD ["python", "bot.py"]
