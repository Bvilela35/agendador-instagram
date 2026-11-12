FROM python:3.12-slim
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "servidor.py"]
