# CodeReferee AI core. Actions에서 빌드해 ECR로 올리고 EC2에서 실행한다.
# 빌드 컨텍스트는 ai-core/ 이다.
FROM python:3.12-slim

WORKDIR /app

# Preflight가 git ls-remote로 레포 접근성을 확인한다. git이 있어야 한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# 의존성만 먼저 설치해 레이어 캐시를 살린다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 기본은 Redis 워커(큐 소비 → 검증 파이프라인). FastAPI 수신 서버가 필요하면
# CMD를 덮어쓴다: uvicorn app.main:app --host 0.0.0.0 --port 8000
# layer-1(로컬 도커) 샌드박스를 쓰려면 런타임에 docker 소켓을 마운트해야 한다.
# layer-2는 SANDBOX_BASE_URL로 샌드박스 HTTP 서비스를 가리키면 docker가 필요 없다.
CMD ["python", "-m", "app.worker"]
