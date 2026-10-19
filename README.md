# UAV 下載 ＆ 影音刮削整理中心 (Web Manager)

專為 **Synology NAS / Linux Server** 量身打造的多站影片下載與全自動刮削整理系統。
底層整合 **[UAV-Downloader](https://github.com/Alos21750/UAV-Downloader)** 下載引擎，並完整移植 **JavHelper Pro** 的多站備援刮削、Google 繁中翻譯、橫幅直式海報裁切與自訂命名規則。

相容 **Jellyfin / Emby / Plex / Kodi** 等主流家庭影音伺服器！

---

## 🌟 核心特色

1. **線上影音探索 ＆ 一鍵下載中心**：
   - **多站即時切換**：支援 **JableTV**、**MissAV**、**SupJav** 三大主流影音來源隨選切換。
   - **六大熱門分類**：今日熱門、本週熱門、本月熱門、最新上架、中文字幕、無碼流出，藥丸標籤即點即看。
   - **人氣女優官方照庫**：內建 112 位主流人氣女優官方高畫質寫真頭像，點擊任一女優即可深入展開其專屬作品庫。
   - **關鍵字多站搜尋**：支援輸入番號（如 `SSIS-001`）、女優名或關鍵字，支援 <kbd>Enter</kbd> 鍵快速搜尋。
   - **一鍵下載與批次排程**：每部影片均提供「➕ 一鍵下載」；勾選多部影片時底部自動彈出「批次加入下載佇列浮動列」。
   - **傳統貼網址保留**：完整相容多行網址貼上批次下載功能。

2. **多站備援刮削 (Multi-Source Scraper)**：
   - 自動交叉查詢 **FC2 官方庫 ➜ JavBus (有碼/無碼庫) ➜ JavDB 全網模糊匹配**。
   - 番號錯字自動容錯糾錯（如 `STARTS-` 糾錯為 `START-` / `STARS-`）。

3. **Google 免費繁體中文翻譯**：
   - 自動將日文原名翻譯為繁體中文，並在 NFO 中同時保留繁中標題與日文原名。

4. **橫幅封面智慧裁切為 2:3 直式海報 (CutCoverPoster)**：
   - 支援各大網站圖片防盜鏈下載與轉發快取。
   - 橫幅展開圖自動裁切右側 0.704 比例正面，生成高品質直式海報 (`poster.jpg`) 與背景展開圖 (`fanart.jpg`)。

5. **JavHelper Pro 經典自訂命名規則**：
   - 支援單女優 / 多女優分開自訂命名格式。
   - 內建「標準分層」、「女優平鋪」、「番號優先」、「人數分層」等範本一鍵套用。
   - 支援變數標籤快捷插入（`%Name%`, `%ProductNumber%`, `%Title%`, `%ReleaseDate%`, `%ActorCount%` 等）。
   - 網頁介面即時動態預覽（Live Preview）。

6. **兩階段整理入庫 ＆ 刪除清理**：
   - 遞迴掃描下載目錄與所有子目錄。
   - 點擊「刮削預覽」先在網頁預覽海報、片名、女優與預計入庫路徑。
   - 確認無誤後支援單部入庫、勾選批次入庫與一鍵全部入庫。
   - 支援待整理庫中單片與批次勾選直接刪除不要的檔案。
   - 移動歸檔後自動清理空的子目錄。

7. **手機端極致響應式體驗 (RWD)**：
   - 手機瀏覽時自動切換為雙欄影片網格（2 Columns Grid）與獨立觸控操作卡片。

8. **管理者帳號保護**：
   - 首次開啟自動引導註冊管理者帳號。
   - 密鑰與帳號持久化保存，容器重啟不遺失。

---

## 🚀 Synology NAS 部署指南 (Container Manager)

### 1. 準備目錄
在 NAS 上建立以下資料夾（路徑可依個人需求自行調整）：
- 下載暫存目錄：`/volume1/video/downloads`
- 整理後媒體庫目錄：`/volume1/video/media_library`
- 專案配置目錄：`/volume1/docker/uav-web-manager`（存放 `docker-compose.yml` 與設定資料）

### 2. 配置 `docker-compose.yml`
在專案目錄下建立 `docker-compose.yml`：

```yaml
version: '3.8'

services:
  uav-web:
    # 官方全封裝映像檔（自動包含 Web 管理、影音探索、女優庫與 Cloudflare 繞過引擎）
    image: alantsai0830/uav-web-manager:latest
    container_name: uav-web-downloader
    restart: unless-stopped
    ports:
      - "8088:8080"
    volumes:
      # 下載暫存目錄（請替換為您 NAS 上的實際下載資料夾）
      - /volume1/video/downloads:/downloads
      # 刮削整理後的正式媒體庫目錄（請替換為您 NAS 上的實際媒體庫資料夾）
      - /volume1/video/media_library:/media_library
      # 系統配置與帳號持久化目錄（存放登入密鑰與自訂命名規則）
      - ./config:/app/config
    environment:
      - DOWNLOAD_DIR=/downloads
      - MEDIA_LIBRARY_DIR=/media_library
      # 自動同步至網頁介面顯示的 NAS 實體路徑（請與上方 volumes 左側路徑保持一致）
      - HOST_DOWNLOAD_DIR=/volume1/video/downloads
      - HOST_MEDIA_DIR=/volume1/video/media_library
      - RESOLUTION=highest
      - TZ=Asia/Taipei
```

### 3. 啟動與訪問
1. 開啟 Synology **Container Manager** ➜ **專案 (Project)** ➜ **新增**，選擇放置目錄並啟動即可（或在該目錄執行 `docker compose up -d`）。
2. 啟動後打開瀏覽器訪問：
   ```text
   http://<您的 NAS IP>:8088
   ```

---

## 🔄 日後版本更新方式

因為本專案已經完全封裝並透過 GitHub Actions 自動建置發布至 Docker Hub，日後若有新版本更新，更新方式極度簡單：

- **群暉 Container Manager 圖形化操作**：
  在專案列表中對 `uav-web` 點擊右鍵 ➜ **「動作」** ➜ **「組建 / 重新啟動」**（或選擇 **「停止」** ➜ **「清除（拉取最新映像檔）」** ➜ **「啟動」**）。
- **指令列操作**：
  ```bash
  docker compose pull
  docker compose up -d
  ```
無需再進 SSH 執行 git pull，1 秒快速重啟即可享受最新功能！

---

## 📂 目錄結構

```text
uav-web-manager/
├── app.py                # Flask Web 應用主程式（包含下載佇列、影音探索、多站刮削、裁圖與前端 UI）
├── actress_cache.json    # 112 位熱門女優高畫質官方寫真快取庫
├── Dockerfile            # 容器封裝建置腳本
├── docker-compose.yml    # Docker Compose 部署配置範本
├── .github/workflows/    # CI/CD 自動編譯與 Docker Hub 發布工作流
├── .gitignore            # Git 忽略檔案設定
└── README.md             # 專案詳細說明文件
```
