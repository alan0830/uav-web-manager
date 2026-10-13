# UAV 下載 ＆ 影音刮削整理中心 (Web Manager)

專為 **Synology NAS / Linux Server** 量身打造的多站影片下載與全自動刮削整理系統。
底層整合 **[UAV-Downloader](https://github.com/Alos21750/UAV-Downloader)** 下載引擎，並完整移植 **[JavHelper Pro](https://github.com/Alos21750/UAV-Downloader)** 的多站備援刮削、Google 繁中翻譯、橫幅直式海報裁切與自訂命名規則。

相容 **Jellyfin / Emby / Plex / Kodi** 家庭影音庫！

---

## 🌟 核心特色

1. **多合一下載支援**：
   - 支援 **JableTV、MissAV、SupJav、Hanime1** 等多平台影片解析下載。
   - 批次網址多行貼上、任務佇列排程、即時片段進度條與速度顯示。
   - 下載完成後自動清理非影片雜檔，僅保留純影片檔。
2. **多站備援刮削 (Multi-Source Scraper)**：
   - 自動交叉查詢 **FC2 官方庫 ➜ JavBus (有碼/無碼庫) ➜ JavDB 全網模糊匹配**。
   - 番號錯字自動容錯糾錯（如 `STARTS-` 糾錯為 `START-` / `STARS-`）。
3. **Google 免費繁體中文翻譯**：
   - 自動將日文原名翻譯為繁體中文，並在 NFO 中同時保留繁中標題與日文原名。
4. **橫幅封面智慧裁切為 2:3 直式海報 (CutCoverPoster)**：
   - 支援各網站圖片防盜鏈下載。
   - 橫幅展開圖自動裁切右側 0.704 比例正面，生成高品質直式海報 (`poster.jpg`) 與背景展開圖 (`fanart.jpg`)。
5. **JavHelper Pro 經典自訂命名規則**：
   - 支援單女優 / 多女優分開自訂命名格式。
   - 內建「標準分層」、「女優平鋪」、「番號優先」、「人數分層」等範本一鍵套用。
   - 支援變數標籤快捷插入（`%Name%`, `%ProductNumber%`, `%Title%`, `%ReleaseDate%`, `%ActorCount%` 等）。
   - 網頁介面即時動態預覽（Live Preview）。
6. **兩階段整理入庫**：
   - 遞迴掃描下載目錄與所有子目錄。
   - 點擊「刮削預覽」先在網頁預覽海報、片名、女優與預計入庫路徑。
   - 確認無誤後支援單部入庫、勾選批次入庫與一鍵全部入庫。
   - 移動歸檔後自動清理空的子目錄。
7. **管理者帳號密碼保護**：
   - 首次開啟自動引導註冊管理者帳號。
   - 密鑰與帳號持久化保存，容器重啟不遺失。

---

## 🚀 Synology NAS 部署指南 (Container Manager)

### 1. 準備目錄
在 NAS 上建立以下資料夾（路徑可自行調整）：
- 下載暫存目錄：`/volume1/xVideo/new`
- 整理後媒體庫目錄：`/volume1/xVideo/JAVDB_DATA`
- 專案配置目錄：`/volume2/docker/uav-web-manager`（放置 `docker-compose.yml` 與 `app.py`）

### 2. 配置 `docker-compose.yml`
```yaml
version: '3.8'

services:
  uav-web:
    image: ghcr.io/alos21750/uav-downloader:latest
    container_name: uav-web-downloader
    restart: unless-stopped
    ports:
      - "8088:8080"
    volumes:
      # 下載暫存目錄（僅存放影片主檔）
      - /volume1/xVideo/new:/downloads
      # 刮削整理後的正式媒體庫目錄
      - /volume1/xVideo/JAVDB_DATA:/media_library
      # 網頁程式掛載
      - ./app.py:/app/app.py
    environment:
      - DOWNLOAD_DIR=/downloads
      - MEDIA_LIBRARY_DIR=/media_library
      # 自動同步至網頁顯示的 NAS 實體目錄
      - HOST_DOWNLOAD_DIR=/volume1/xVideo/new
      - HOST_MEDIA_DIR=/volume1/xVideo/JAVDB_DATA
      - RESOLUTION=highest
      - TZ=Asia/Taipei
    entrypoint: ["sh", "-c", "pip install --no-cache-dir flask beautifulsoup4 pillow requests && python -u /app/app.py"]
```

### 3. 啟動專案
開啟 Synology **Container Manager** ➜ **專案 (Project)** ➜ **新增**，選擇放置目錄並啟動即可。

啟動後打開瀏覽器訪問：
```text
http://<NAS-IP>:8088
```

---

## 📂 目錄結構

```text
uav-web-manager/
├── app.py                # Flask Web 應用主程式（包含下載佇列、多站刮削、裁圖與前端 UI）
├── docker-compose.yml    # Docker Compose 部署配置檔
├── .gitignore            # Git 忽略檔案設定
└── README.md             # 專案詳細說明文件
```
