FROM python:3.12-bookworm

WORKDIR /root/ocr_extraction/monitor_support

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

RUN mkdir -p \
    /root/ocr_extraction/monitor_support/.auth \
    /root/ocr_extraction/monitor_support/output/json

CMD ["tail", "-f", "/dev/null"]