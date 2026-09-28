# 每日訊號的執行環境。
#
# 設計為「跑完即退出」的一次性容器，由主機的 cron 觸發，而非常駐服務。
# 記憶體只在實際執行的十幾秒內佔用，其餘時間為零，這對小型主機很重要。
FROM python:3.12-slim

# 僅安裝伺服器所需套件；回測與繪圖用的相依留在本機
COPY requirements-server.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-server.txt \
    && rm -rf /tmp/requirements-server.txt /root/.cache

WORKDIR /app
COPY config.py ./
COPY src/ ./src/
COPY scripts/ ./scripts/

# 時區固定為台北，讓程式內的日期判斷與台股交易日一致。
# 這只影響容器內部，不會更動主機時區——主機上其他服務的 cron 仍照原設定運作。
ENV TZ=Asia/Taipei
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 資料與紀錄簿掛載自主機，容器本身不保存狀態
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "scripts/daily.py"]
CMD ["--push"]
