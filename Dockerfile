FROM python:3.12-bookworm

WORKDIR /home/hr306/monitor_support

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

RUN mkdir -p \
    /home/hr306/monitor_support/.auth \
    /home/hr306/monitor_support/output/json

CMD ["tail", "-f", "/dev/null"]
