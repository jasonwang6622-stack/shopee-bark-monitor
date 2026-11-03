# 蝦皮補貨監控 → Bark iPhone 推播（Python）

這套工具會監控 **台灣蝦皮 `shopee.tw` 商品**，發現商品／指定規格從「缺貨」變成「可購買」時，透過 Bark 推播到 iPhone。可同時追蹤多個商品。

**重要限制**：這是依目前觀察到的蝦皮公開商品頁面資料進行判斷，並非官方庫存 API。蝦皮可能改版、限制讀取、要求登入或驗證，屆時程式會記錄 `unknown` 而不誤判為缺貨／補貨。未提供真實商品連結，無法保證特定商品頁可讀；需先以 `--diagnose` 試跑。

> **已替你設定商品**：本 ZIP 的 `config.json` 已填入你分享的 `https://s.shopee.tw/8Kqe14Qu9q` 對應的商品，賣家 ID `1510747083`、商品 ID `53668488583`。預設監控「任一規格」補貨，**尚未確認商品名稱與即時庫存**。只需補上 Bark Key。
>
> **重要**：本環境可以解析短網址的目標商品 ID，但無法完成蝦皮商品頁庫存讀取。這份程式已通過離線測試，**不是**已確認能在該商品頁面正常判斷庫存。請先執行 `--diagnose`；如果顯示 `unknown`，代表尚無法可靠監控，不能當作補貨提醒已生效。

## 1. iPhone 設定 Bark

1. App Store 搜尋並安裝 **Bark - Custom Notifications**。
2. 開啟 Bark，允許通知。
3. 在 Bark App 首頁找到裝置專屬推播 Key（也有測試推播網址）；只複製 Key，**不需要貼給別人**。

## 2. MacBook 安裝（終端機）

確認有 Python 3.10 以上。打開「終端機」，進入解壓縮的資料夾：

```bash
cd ~/Downloads/shopee_bark_monitor
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

如資料夾不在 `~/Downloads`，請自行修改路徑。安裝 Chromium 是為了在純文字頁面無法取得資料時，嘗試用瀏覽器讀取公開商品資料；**不繞過驗證或登入**。

## 3. 修改 config.json

本次壓縮檔已附上填好商品網址的 `config.json`，因此**不用自行修改商品網址**。只需在 `config.json` 填入 Bark Key；若要自訂規格才修改 `variant_keywords`。

```bash
open -e config.json
```

- `bark_key`：填 Bark App 裡的推播 Key（不要加上 `https://api.day.app/`）。
- `items[].url`：改成商品的完整網址，支援 `https://shopee.tw/商品名稱-i.賣家ID.商品ID`、`https://shopee.tw/product/賣家ID/商品ID`、`https://shopee.tw/opaanlp/賣家ID/商品ID`。短網址通常需要先解析成商品完整網址；這次已幫你解析。
- `items[].name`：自由命名，方便辨識。
- `items[].variant_keywords`：空陣列 `[]` 代表 **任一規格** 可買就算有貨；若只要「黑色、M」，填 `["黑色", "M"]`，會找名稱同時含這些字的規格。
- `interval_seconds`：預設 600 秒（10 分鐘）；為降低網站負擔，程式拒絕低於 600 秒的頻率。

多個商品就往 `items` 陣列增加相同格式的物件。

## 4. 測試與開始

**先執行 `--diagnose`，確認顯示的是 `out_of_stock` 或 `in_stock`，而非 `unknown`。** 若顯示 `unknown`，無法確保補貨推播功能實際生效。

```bash
python3 monitor.py --test-push  # 先確認 iPhone 能收到 Bark
python3 monitor.py --diagnose   # 只檢查商品庫存資料，絕不推播或改寫狀態
python3 monitor.py --once       # 正式檢查一次、記錄狀態
python3 monitor.py              # 持續監控（每 10 分鐘左右）
```

`Ctrl+C` 停止；MacBook 必須保持開機、不要進入睡眠，程式才會持續檢查。若想永續運行，可日後移到常開的電腦／雲端主機。

### 判斷規則

- 只有從已確認「out_of_stock」變成已確認「in_stock」才發通知。
- **首次執行只建立基準狀態，不會因為原本有貨而推播。** 要驗證通知通道請用 `--test-push`。
- `unknown`（網路失敗、無匹配規格、頁面驗證、反爬限制、資料不完整）不會清除舊狀態或觸發補貨通知。
- Bark 推播成功後才將最新狀態記錄於 `state.json`，以減少通知失敗時漏報。
- **勿分享** `config.json`、Bark Key；通知網址帶有商品連結。
- `monitor.log` 是監控紀錄，`state.json` 是上次已確認狀態。

## 排除問題

| 訊息 | 說明 |
| --- | --- |
| `unknown | 頁面找不到商品庫存結構` | 蝦皮頁面改版／地區不符／無法公開讀取；請提供商品公開連結讓程式調整，**不是**已售完。 |
| `unknown | 找不到指定規格` | `variant_keywords` 與網站資料中的規格名稱不一致。 |
| `unknown | 網站拒絕請求` | 可能遇到存取限制，程式不會繞過。 |
| `已提交測試推播` 但沒看到通知 | 檢查 Bark 裝置 Key、iOS 通知授權、專注模式、網路連線。 |
| Mac 休眠後沒通知 | 需要保持電腦正常運作，或另設雲端執行。 |

## 安全與使用提醒

只做公開頁面的低頻讀取，不處理登入、驗證碼、下單或付款；請遵守蝦皮網站規範。補貨可能發生在兩次檢查之間並售完，因此不能保證搶到商品。免費 Bark 伺服器可能受網路／服務狀態影響，非即時保證。
