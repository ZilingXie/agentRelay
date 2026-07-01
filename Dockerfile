FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY server ./server
COPY dashboard ./dashboard

RUN mkdir -p /app/data && chmod -R a+rX /app/server /app/dashboard

USER 1000:1000

EXPOSE 8787 8788

CMD ["python", "-m", "server.app"]
