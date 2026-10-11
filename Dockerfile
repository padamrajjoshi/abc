# Bullseye packages have moved to Debian's signed archive repositories.
# Configure this once so every stage uses the same valid package sources.
FROM python:3.11-slim-bullseye AS bullseye-base
RUN rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources && \
    printf '%s\n' \
      'deb [check-valid-until=no] https://archive.debian.org/debian bullseye main' \
      'deb [check-valid-until=no] https://archive.debian.org/debian-security bullseye-security main' \
      > /etc/apt/sources.list && \
    printf '%s\n' 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/80studio-retries

FROM bullseye-base AS nbis-build
ARG NBIS_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential ca-certificates libx11-dev libxt-dev libopenjp2-7-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git clone https://github.com/biometric-technologies/nist-biometric-image-software-nbis.git nbis && \
    cd nbis && git checkout "$NBIS_REF"
RUN apt-get update && apt-get install -y --no-install-recommends cmake && \
    rm -rf /var/lib/apt/lists/*
COPY scripts/prepare_nbis.py /build/prepare_nbis.py
RUN cd nbis && python /build/prepare_nbis.py && \
    ./setup.sh /usr/local --without-X11 && \
    sed -i 's/-O2 -w/-O2 -fcommon -w/' rules.mak && \
    make config && make it && make install && \
    test -x /usr/local/bin/nfseg && test -x /usr/local/bin/nfiq

FROM bullseye-base AS sourceafis-build
RUN apt-get update && apt-get install -y --no-install-recommends maven ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY vendor/sourceafis/pom.xml /build/pom.xml
RUN --mount=type=cache,target=/root/.m2 mvn -B -f /build/pom.xml org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies \
    -DoutputDirectory=/jars

FROM bullseye-base
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-11-jre-headless libopenjp2-7 libusb-1.0-0 libstdc++6 ca-certificates \
    tesseract-ocr tesseract-ocr-eng && \
    rm -rf /var/lib/apt/lists/* && groupadd --gid 10001 studio && \
    useradd --uid 10001 --gid studio --create-home studio
COPY --from=nbis-build /usr/local/bin/ /usr/local/bin/
COPY --from=nbis-build /usr/local/nbis/ /usr/local/nbis/
WORKDIR /studio
COPY requirements.in .
RUN pip install --no-cache-dir -r requirements.in
COPY --from=sourceafis-build /jars/ /studio/vendor/sourceafis/
COPY app/ /studio/app/
RUN mkdir /studio/data && chown -R studio:studio /studio/data
ENV BIOMETRICS_DB=/studio/data/biometrics.db MATCHER_ENGINE=sourceafis \
    DEVICE_MODE=auto ALLOW_ESTIMATED_QUALITY=1 PYTHONUNBUFFERED=1
USER studio
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "16", "--timeout-keep-alive", "5"]
