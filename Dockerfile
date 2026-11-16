FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml SPEC.md DATABASE.md ./
COPY formspec ./formspec

RUN pip install --no-cache-dir -e . && mkdir -p /data

ENV FORMSPEC_DATABASE_URL="sqlite:////data/formspec.db" \
    HOST="0.0.0.0" \
    PORT="8080"

VOLUME ["/data"]
EXPOSE 8080

CMD ["python3", "-m", "formspec", "ide"]
