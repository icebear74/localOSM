FROM python:3.12-alpine

RUN apk add --no-cache osmium-tool

WORKDIR /app
COPY app.py /app/app.py
EXPOSE 8080
CMD ["python", "/app/app.py"]
