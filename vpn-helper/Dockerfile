FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY gateway.py /opt/pts-vpn/gateway.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/opt/pts-vpn/gateway.py"]
