FROM ghcr.io/alos21750/uav-downloader:latest

WORKDIR /app

# 安裝 Web 介面、刮削引擎與 Cloudflare 繞過依賴
RUN pip install --no-cache-dir flask beautifulsoup4 pillow requests curl_cffi

# 複製應用程式主程式與女優快取檔案
COPY app.py /app/app.py
COPY actress_cache.json /app/actress_cache.json

# 建立預設目錄
RUN mkdir -p /app/config /downloads /media_library

# 暴露 Web 介面連接埠
EXPOSE 8080

# 預設啟動 Web 服務
ENTRYPOINT ["python", "-u", "/app/app.py"]
