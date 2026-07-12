FROM node:22-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME="/usr/lib/jvm/java-17-openjdk-arm64" \
    PATH="/opt/venv/bin:${JAVA_HOME}/bin:${PATH}" \
    SANDBOX_WORK_ROOT=/tmp/codereferee-sandbox \
    NPM_CONFIG_CACHE=/cache/npm \
    YARN_CACHE_FOLDER=/cache/yarn \
    PIP_CACHE_DIR=/cache/pip \
    COMPOSER_CACHE_DIR=/cache/composer \
    GRADLE_USER_HOME=/cache/gradle \
    MAVEN_OPTS="-Dmaven.repo.local=/cache/maven/repository" \
    SANDBOX_COMMAND_TIMEOUT_SECONDS=600 \
    SANDBOX_REPOSITORY_TIMEOUT_SECONDS=300 \
    SANDBOX_SERVER_START_TIMEOUT_SECONDS=90

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      chromium \
      composer \
      curl \
      git \
      libxml2-dev \
      openjdk-17-jdk-headless \
      php-cli \
      php-curl \
      php-mbstring \
      php-mysql \
      php-xml \
      procps \
      python3 \
      python3-pip \
      python3-venv \
      tini \
      unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir -r /app/requirements.txt
COPY app.py /app/app.py

EXPOSE 8000
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
