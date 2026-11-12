FROM python:3.12-slim
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY iniciar.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "iniciar.py"]
