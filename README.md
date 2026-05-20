# 黑優浦蜜 Discord Bot v2.3.0

## 🆕 v2.3.0 重點
- **「找附近哪裡有賣」改成純 Google Maps 外開**：抽完食物/飲料按「🔍 找附近哪裡有賣」→ 只有「🗺️ 打開 Google Maps」「❌ 不需要」兩個按鈕
- **移除**：地址輸入 Modal、半徑設定、Places API 查詢、Geocoding 查詢
- **`/巴豆妖 吃飯飯` 改成直接產生 Google Maps 連結**（移除半徑參數、不打任何 API）
- Places / Geocoding API **暫停使用**（config 保留，未來可重啟）
- Google Maps 外開用使用者裝置自己的定位，比 Bot 在 Discord 裡解析地址準

# 黑優浦蜜 Discord Bot v2.2.0

## 🆕 v2.2.0 重點
- **天氣台灣優先**：北投/左營/西屯等區名 → 縣市 CWA，不再跳「臺灣省」候選
- **算算數工程計算機**：sympy symbolic（`sum(i^2,i=1..n)`=n(n+1)(2n+1)/6）、`sqrt/sin/log/ln`、留空看符號速查
- **歌姬啦 15 個分 3 頁**：官方置頂、其餘依點閱率
- **本回合歌單**：播完 / 停止時輸出這次唱過的歌
- **找附近半徑上限 10000m**、捷運站多 candidate 解析、一律 Google Maps 保底
- **互動選單 timeout 120 秒** + 過期友善提示
- **小語擴充**：食物 60 句、飲料 62 句 + AI 小語驗證 fallback
- **/黑優浦蜜 狀態** 改 `DEBUG_COMMANDS_ENABLED=true` 才註冊
- **指令分組順序**：/巴豆妖 → /查詢 → /音樂 → /工具 → /黑優浦蜜

## ✨ 指令分類（5 個 Command Group / 18 個指令）

### 🎵 /音樂
| 指令 | 說明 |
|---|---|
| `/音樂 歌姬啦` | 播放或加入佇列（純文字 → 10 個分 2 頁手動選歌） |
| `/音樂 插播` | 把歌塞到佇列最前面，不打斷正在播的 |
| `/音樂 佇列` | 查看歌單 |
| `/音樂 現正播放` | 看目前在播什麼 |
| `/音樂 跳過` | 跳過目前歌（管理員 + 點歌者） |
| `/音樂 循環` | 關閉 / 單曲 / 歌單 |
| `/音樂 停止` | 停止並離開語音 |
| `/音樂 清空佇列` | 清空佇列（管理員限定） |

排序規則：官方 / VEVO / Topic / Records +30 分，限制類型（cover/live/karaoke/instrumental/piano/remix/reaction/tutorial/mashup）-50 分。觀看數 log scale tiebreaker。

### 🍱 /巴豆妖
| 指令 | 說明 |
|---|---|
| `/巴豆妖 吃什麼` | 隨機抽一道菜（500 道，附「找附近」按鈕） |
| `/巴豆妖 喝什麼` | 隨機抽一杯飲料（355 杯 / 8 家店，兩層抽樣，附「找附近」按鈕） |
| `/巴豆妖 吃飯飯` | 搜尋附近餐廳（食物 必填、地點 選填、半徑 100~5000m） |

**「找附近哪裡有賣」流程（v2.1.9 大改）**

按下「🔍 找附近哪裡有賣」後，不再默默用預設位置，先問三選一：

1. **📝 可以，我輸入位置** — 彈出 Modal（地點 + 半徑 100~5000m，預設 1000m）→ 用 Places API 查
2. **🗺️ 打開 Google Maps** — Link Button，直接開 `maps.google.com/?q=...`，不打 Places API
3. **❌ 不可以** — 顯示「那你自己想辦法吧」，不查

Places API 403 處理：自動切換到「Google Maps fallback button」+ Console 印詳細診斷（不洩漏 key）。

### 🔎 /查詢
| 指令 | 說明 |
|---|---|
| `/查詢 天氣` | 中英日地名 → CWA (台灣) / Open-Meteo (國外)，多候選 select menu |
| `/查詢 翻譯姬` | 翻譯文字（直譯 / AI 自然語氣） |

**`/查詢 天氣` 地名解析（v2.1.8 大幅強化）**
- 47 都道府縣完整 mapping（449 條目，含中 / 日漢字 / 平假名 / 片假名 / 英文 / 縣府所在地 / 古名「江戶」「浪速」「平安京」）
- 多層 fallback：清理 → suffix 正規化 → 本地 alias → 國家+城市拆解 → AI 多候選 → raw geocoding
- OpenCC 簡轉繁（避免 geocoding 回「佛罗里达州」）
- 「日本 青森縣」「青森縣 日本」「青森県」「日本 青森」「Aomori Japan」全部能解析

### 🧰 /工具
| 指令 | 說明 |
|---|---|
| `/工具 算算數` | 本地安全計算 |
| `/工具 提醒醒` | 用選單設時間 |
| `/工具 洗芭樂` | 隨機產生器 |

### 🐾 /黑優浦蜜
| 指令 | 說明 |
|---|---|
| `/黑優浦蜜 狀態` | 顯示 Bot 環境（FFmpeg / Spotify / CWA / Places / AI / Redis / DB / 語音狀態 / 佇列） |
| `/黑優浦蜜 幫助` | 顯示所有指令說明 |

---

## 🛠️ 從 v2.1.8 升級

主要改動：
1. **「找附近」流程不再默默用預設位置** — 改 LocationChoice 三選一
2. **/巴豆妖 吃飯飯 加半徑參數** — 100~5000m，自動 clamp
3. **Places API 403 友善處理** — 自動 fallback 到 Google Maps button
4. **debug log 不洩漏 key** — 只印 prefix

---

## 🏗️ 架構

```
helpmee_v2/
├── core/
│   ├── data/
│   │   ├── drink_menus.json         # 8 家店、373 品項
│   │   ├── taiwan_dishes.json       # 500 道菜
│   │   └── location_aliases.json    # 449 日本 / 88 台灣 / 57 國際 / 62 國家
│   ├── music_engine.py
│   ├── music_queue.py               # 含 add_front (插播用)
│   ├── weather_engine.py            # 多層 fallback + OpenCC
│   ├── food_engine.py               # 403 處理 + 不洩漏 key log
│   ├── translate_engine.py
│   ├── utility_ai.py                # normalize_location_candidates
│   ├── food_picker.py
│   └── drink_picker.py              # 兩層抽樣 + primary_search_keyword
├── database/
├── adapters/
│   └── discord_adapter.py           # 5 個 command group / 18 指令
└── main.py
```

## 🚀 啟動

```bash
pip install -r requirements.txt
cp .env.example .env
# 至少填 DISCORD_TOKEN
python main.py
```

**Google Places 403 修法**：
- GCP Console 確認「Places API (New)」已啟用（不是舊版 Places API）
- API key restrictions 確認允許 Places API (New)
- billing 綁定有效

---

## 🗺️ TODO

- CWA 鄉鎮市區預報（F-D0047）
- 天氣自動每日推播
