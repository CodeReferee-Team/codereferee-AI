# CodeReferee 통합 샌드박스 이미지.
#
# 지금은 스택 감지가 컨테이너 안에서(clone 이후) 일어나기 때문에 실행 전에
# 스택을 알 수 없다. 그래서 스택별 이미지를 고르지 못하고 모든 툴체인을 한
# 이미지에 담는다. preflight가 스택을 먼저 감지하도록 바뀌면 스택별로 쪼갠다.
#
# 빌드: docker build -t codereferee/sandbox-multi:1 ai-core/sandbox/
FROM eclipse-temurin:17-jdk-jammy

# 대화형 프런트엔드가 없으면 debconf가 경고를 stderr로 쏟아내고,
# 그 노이즈가 그대로 Judge/Critic의 evidence에 섞인다.
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates \
         curl \
         git \
         maven \
         python3 \
         python3-pip \
         python3-venv \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 검증 스크립트는 런타임에 /workspace로 마운트된다.
WORKDIR /workspace
