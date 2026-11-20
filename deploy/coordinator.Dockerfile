FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 coordinator && useradd --uid 10001 --gid coordinator --no-create-home coordinator \
    && mkdir /state && chown coordinator:coordinator /state
COPY coordinator/ ./coordinator/
COPY gate/ ./gate/
COPY scout/ ./scout/
COPY dashboard/__init__.py dashboard/cloud.py ./dashboard/
COPY config.json ./config.json
USER 10001:10001
ENTRYPOINT ["python", "-m", "coordinator", "--db", "/state/coordinator.sqlite3"]
CMD ["run", "--env-file", "/run/secrets/.env"]
