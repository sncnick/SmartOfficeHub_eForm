# CDD 公司資料子系統

這份文件給要嵌入或呼叫本系統的其他系統使用。CDD 負責把客戶盡職審查（Customer Due Diligence）Excel 轉成可修改的公司文件，並在之後以 MongoDB 為唯一資料來源。

頁面名稱是「CDD 公司資料檢視」。程式目錄是 `cdd-viewer/`。

## 子系統邊界

本系統擁有的是「公司文件」：公司基本資料、董事（DIR）、股東（SH）、公司秘書（Com Sec）、註冊辦事處（RO）、分行、股本、持股，以及由 Excel 帶入的問題清單。

母系統應把本系統當成獨立服務：

- 匯入一次 Excel 之後，讀寫都走本系統的 HTTP API 或內嵌頁面，不要在母系統再解析同一份 Excel。
- 公司主鍵是本系統的 `id`。母系統可以用 `company_no` 對應自己的公司編號，但寫入時要用 `id`。
- 本系統沒有登入、沒有跨來源（CORS）設定，預設只聽 `127.0.0.1`。母系統若要給其他人使用，要自己放在同一台機器的反向代理後面，並加上自己的登入與授權。
- Excel 與 MongoDB 裡是個人資料。不要把資料庫埠號公開到網路，不要把匯出的 JSON 放進程式庫。

母系統不需要複製表單規則。地址是否算地址、哪一筆持股用來計算、OUTDATED 怎麼標，都由本系統在讀取和寫入時處理。

## 架構

```text
母系統
  │  iframe 或連結          HTTP JSON
  ▼                         ▼
cdd-viewer（FastAPI，容器內 8000）
  │
  ▼
MongoDB 7（資料庫 cdd）
  companies   一間公司一份文件，_id = 公司 id
  meta        _id = "workspace"，匯入來源與整張表的問題
```

Excel 只在「匯入所選 Excel」或「上載 Excel」時讀一次。之後重新整理頁面只讀 MongoDB。

沒有設定 `CDD_MONGO_URL`，或設定 `CDD_STORE=memory` 時，資料放在行程記憶體，重啟即消失。正式部署要使用 MongoDB。

## 部署

在 `cdd-viewer/` 目錄：

```bash
docker compose up -d --build
```

預設頁面是 `http://127.0.0.1:8765/`。容器內服務聽 8000，主機埠由 `CDD_PORT` 決定，預設 8765。綁定位址是 `127.0.0.1`，同一台機器上的母系統可以直接呼叫，區網其他電腦預設連不到。

| 服務 | 容器名稱 | 映像 | 資料 |
| --- | --- | --- | --- |
| `cdd-viewer` | `cdd-viewer` | `cdd-viewer:dev` | 程式。重建映像才會換頁面與 API |
| `mongo` | `cdd-mongo` | `mongo:7` | 具名卷 `cdd-mongo`，掛在容器 `/data/db` |

卷：

| 主機 | 容器 | 用途 |
| --- | --- | --- |
| `CDD_EXCEL_DIR`，預設 `../Database purge` | `/data` 唯讀 | 供匯入的 Excel |
| `./output` | `/app/output` | 解析 Excel 時順便寫出的 JSON，不是線上資料來源 |
| `cdd-mongo` | `/data/db` | MongoDB |

健康檢查：

```bash
curl -sf http://127.0.0.1:8765/api/health
```

回傳 `{"status":"ok"}`。MongoDB 未就緒時是 HTTP 503，內文為「資料庫未就緒」。

更新程式：

```bash
docker compose up -d --build cdd-viewer
```

只重建檢視服務。不要刪除 `cdd-mongo` 卷，否則公司資料會消失。

互動式 API 說明在 `http://127.0.0.1:8765/docs`。

### 環境變數

| 變數 | 預設 | 作用 |
| --- | --- | --- |
| `CDD_PORT` | `8765` | 主機埠 |
| `CDD_MONGO_URL` | compose 內為 `mongodb://mongo:27017` | 有值且不是 memory 模式時使用 MongoDB |
| `CDD_MONGO_DB` | `cdd` | 資料庫名稱 |
| `CDD_STORE` | 未設 | 設為 `memory` 時不使用 MongoDB |
| `CDD_DATA_DIR` | 容器內 `/data` | Excel 目錄 |
| `CDD_EXCEL_DIR` | `../Database purge` | compose 對應到 `CDD_DATA_DIR` 的主機目錄 |
| `CDD_SHEET` | `CDD` | 工作表名稱。找不到時會改找含 Date of birth、CI、BR 表頭的表 |
| `CDD_OUTDATED_FILLS` | `00B0F0` | 視為過期的儲存格底色，逗號分隔、不含 `#` |
| `CDD_DEFAULT_FILE` | compose 內為 `CDD base dummy.xlsx` | `/api/files` 的預設檔名 |
| `CDD_OUTPUT_DIR` | 容器內 `/app/output` | 解析結果 JSON 的目錄 |

容器以 uid 1000 執行。上傳上限 30 MB，只接受 `.xlsx` 與 `.xlsm`。

### 本機不經 Docker 執行

```bash
cd cdd-viewer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
CDD_MONGO_URL=mongodb://127.0.0.1:27017 CDD_DATA_DIR="../Database purge" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

測試：

```bash
cd cdd-viewer
python3 -m unittest discover -s tests -t .
node --check app/static/app.js
```

只把 Excel 轉成 JSON、不寫入資料庫：

```bash
python3 -m app.cli workbook.xlsx -o workbook.json
```

## 建議的整合方式

### 1. 內嵌頁面

母系統已登入的使用者，用 iframe 或同站反向代理打開：

```text
https://母系統網域/cdd/            →  http://127.0.0.1:8765/
https://母系統網域/cdd/api/        →  http://127.0.0.1:8765/api/
```

頁面與 `/static/` 回應 `Cache-Control: no-cache`。反向代理不要再把這些路徑快取很久。

公司網址雜湊是公司 `id`，例如 `http://127.0.0.1:8765/#351`。母系統可以把它當成深連結。`id` 通常是公司編號；編號重複時改為 `R` 加 Excel 行號。

### 2. 只呼叫 API

適合母系統自己的畫面，或夜間對帳。順序：

1. `GET /api/health`
2. 若資料庫是空的，呼叫匯入；已有公司就不要再匯入
3. `GET /api/database` 取得全部公司
4. 用下面的寫入端點改單筆，伺服器回傳整間公司，母系統應以這次回傳覆寫自己的快取

本系統沒有 webhook。母系統若要同步，請拉 `GET /api/database` 或 `GET /api/export`，用每間公司的 `updated_at` 判斷是否變過。

### 3. 不要做的事

- 不要在母系統啟動時自動再匯入 Excel。匯入會整批取代 MongoDB。
- 不要並行對同一間公司寫入。寫入是整份文件取代，後寫的會蓋掉前寫的。
- 不要假設陣列位置永遠不變。人物、地址、持股用 `party_id` 或 `record_id`。欄位路徑裡的數字下標只在該次讀到的陣列上有效。
- 不要把 `/api/data` 或 `/api/upload` 當成線上資料。它們只解析 Excel，不寫入 MongoDB。

## 生命週期

```text
Excel 檔（/data 或上傳）
    │  POST /api/import 或 POST /api/import/upload
    ▼
parse_workbook → ensure_party_ids → replace_all
    ▼
MongoDB companies + meta
    │  頁面自動儲存，或母系統呼叫 field / parties / records / outdated
    ▼
GET /api/database  （讀取時再次 ensure_party_ids，有變更會存回）
```

`ensure_party_ids` 會補上缺少的 `party_id`、`record_id`，依證件號碼記下 HKID，把沒有從地址 1 依序填寫的地址移到 `other`，並把計算用持股之前的未過期持股標成轉讓前。母系統不需要自己做這些步驟。

## HTTP API

除了特別說明的端點，成功時回傳 JSON。錯誤時 FastAPI 的 `detail` 是中文句子。

| 狀態 | 意義 |
| --- | --- |
| 400 | 路徑或內容不接受 |
| 404 | 找不到公司、檔案或上傳 |
| 409 | 資料庫已有公司，匯入未帶 `replace=true` |
| 413 | 上傳超過 30 MB |
| 422 | Excel 無法解析 |
| 503 | 資料庫未就緒 |

### 健康與清單

`GET /api/health`

```json
{"status": "ok"}
```

`GET /api/files`

```json
{
  "files": [{"name": "CDD base dummy.xlsx", "size": 12345}],
  "default": "CDD base dummy.xlsx"
}
```

`name` 是 `/data` 下的相對路徑，可直接交給匯入端點。

### 讀取

`GET /api/database`

線上畫面使用的完整內容。

```json
{
  "source": {
    "file": "workbook.xlsx",
    "sheet": "CDD",
    "header_rows": [4, 5],
    "parsed_at": "2026-10-09T00:00:00+00:00"
  },
  "summary": {
    "companies": 1,
    "issues": {"error": 0, "warning": 1, "info": 0}
  },
  "issues": [],
  "companies": []
}
```

`source` 與頂層 `issues` 來自匯入時的 meta。`companies` 是公司文件陣列，見下一節。`summary.issues` 把整張表與各公司的問題加總。

`GET /api/export`

內容與 `/api/database` 相同，並帶下載檔名 `cdd-database.json`。

### 匯入

匯入會刪除所有公司再寫入。資料庫不是空的時，必須明確同意取代。

由伺服器上的檔案匯入：

```http
POST /api/import?file=CDD%20base%20dummy.xlsx&replace=true
```

由呼叫端上傳：

```http
POST /api/import/upload?replace=true
Content-Type: multipart/form-data

file: workbook.xlsx
```

成功：

```json
{
  "imported": 1,
  "meta": {
    "file": "workbook.xlsx",
    "imported_at": "2026-10-09T00:00:00+00:00",
    "sheet": "CDD",
    "header_rows": [4, 5],
    "sheet_issues": [],
    "count": 1
  }
}
```

`replace` 省略或為 false，且已有公司時，回 409：「資料庫已有公司。再匯入會取代現有資料，請確認後再執行。」

### 只解析、不入庫

這兩個端點給偵錯或預覽。頁面正常操作不要呼叫它們。

- `POST /api/upload`，multipart 欄位 `file`。回傳解析結果，並在 `source.upload_id` 給一個暫存編號。行程內最多留 5 份。
- `GET /api/data?file=` 或 `GET /api/data?upload=`
- `GET /api/download?file=` 或 `GET /api/download?upload=`，同上內容但作為附件。

### 改一間公司

以下端點的成功回應都是整間公司文件，不是只有改過的欄位。`updated_at` 會更新成 UTC 時間。

#### 改欄位

```http
POST /api/companies/{id}/field
Content-Type: application/json

{"path": ["directors", "ab12cd34ef56", "name_en"], "value": "Ada Lau"}
```

`path` 是字串陣列。空字串會存成 `null`。頁面在停止輸入約 500 毫秒，或離開欄位時呼叫；下拉選單在變更時呼叫。

常用路徑：

| 路徑 | 內容 |
| --- | --- |
| `general` / `name_en`、`name_zh`、`ci_no`、`br_no`、`incorporation_date` | 公司基本資料 |
| `company_no` | 公司編號 |
| `issued_shares` | 已發行股數 |
| `directors` / `{party_id}` / `name_en`、`name_zh`、`date_of_birth`、`ci_no`、`br_no`、`appoint_date`、`resign_date` | 人物。`shareholders`、`company_secretaries`、`other_parties` 同形 |
| 同上 / `id_numbers` / `{index}` / `value` 或 `id_type` | 證件。`id_type` 只可為 `hkid`、`passport` 或空字串 |
| 同上 / `emails`、`phones`、`other` / `{index}` / `value` | 電郵、電話、未定義 |
| 同上 / `addresses` / `{index}` / `lines` / `0` 到 `3` | 地址 1 至 4 |
| 同上 / `addresses` / `{index}` / `from`、`to` | 地址開始、結束日期 |
| 同上 / `shareholdings` / `{record_id}` / `no_of_shares`、`total_no_of_shares`、`consideration`、`total_consideration`、`class`、`type`、`date`、`end_date`、`label` | 持股 |
| 同上 / `transfer` / `from`、`to`、`date`、`no_of_shares`、`consideration` | 轉讓。`from` 與 `to` 存股東的 `party_id`，舊資料可能仍是姓名文字 |
| 同上 / `declaration_of_trust` / `bo`、`trustee`、`date`、`shares` | 信託聲明 |
| `registered_offices` / `{record_id}` / `lines` / `0`–`3`、`from`、`to`、`address` | RO |
| `branches` / `{record_id}` / `lines`、`from`、`to`、`label`、`br_no` | 分行 |
| `share_capital` / `{record_id}` / 股本次欄位 | 股本。數字下標也可 |

`directors` 可換成 `shareholders` 或 `company_secretaries`。人物若寫入有效辭任日期，該人物會標成 OUTDATED。

未知路徑回 400：「不能修改這個欄位」。

#### 新增人物

```http
POST /api/companies/{id}/parties
```

```json
{
  "kind": "DIR",
  "label": "DIR4",
  "name_en": "Ada Lau",
  "name_zh": "",
  "date_of_birth": "",
  "id_number": "A123456(7)",
  "id_type": "hkid",
  "ci_no": "",
  "br_no": "",
  "appoint_date": "",
  "resign_date": "",
  "line1": "",
  "line2": "",
  "line3": "",
  "line4": "",
  "shares": ""
}
```

`kind` 是 `DIR`、`SH` 或 `CS`。`label` 必填。只打數字會加上種類，例如 SH 的 `1` 變成 `SH1`。編號可以和現有的相同，以便把舊的一筆標成 OUTDATED。`shares` 只在 `SH` 使用。有效的 `resign_date` 會讓新人物直接成為 OUTDATED。

#### 新增其他紀錄

```http
POST /api/companies/{id}/records
```

| `kind` | 寫入位置 | 主要欄位 |
| --- | --- | --- |
| `RO` | `registered_offices` | `line1`–`line4`、`from`、`to` |
| `BR` | `branches` | 同上，另加 `label`、`br_no` |
| `SHARES` | `share_capital` | `no_of_shares`、`total_no_of_shares`、`consideration`、`total_consideration`、`currency`、`class`、`type`、`date` |
| `HOLDING` | 指定人物的 `shareholdings` | `party_id`，以及股數、日期、`transfer_from`、`transfer_to`、`transfer_date` |
| `OTHER` | `other_parties` | `label`、`name_en`、`name_zh` |

`HOLDING` 找不到 `party_id` 時回 400：「找不到這個人」。

頁面上的持股表不會自動出現空白列。轉讓前紀錄與最新持股會保留。電話欄會多顯示一格空白，讓使用者填另一個號碼；這格在寫入前不入庫。

#### 標示或取消 OUTDATED

```http
POST /api/companies/{id}/outdated
```

```json
{"party_id": "ab12cd34ef56", "outdated": true}
```

`party_id` 可以是人物的 `party_id`，也可以是 RO、分行、股本、持股、地址或清單項目的 `record_id`。`outdated: false` 會取消過期，並去掉 `blue`、`end_date`、`superseded`、`block` 這些自動原因。

頁面只在四種大區塊顯示這個按鈕：RO、DIR、SH、Com Sec。地址、證件、電話、持股、分行、股本不會各自顯示按鈕。

取消公司秘書的某一組過期地址時，該組變成現行地址，原先的現行地址會改標 OUTDATED，使畫面上仍是一組現行地址。

## 公司文件

`GET /api/database` 的 `companies[]` 每個元素是一間公司。重點欄位：

| 欄位 | 說明 |
| --- | --- |
| `id` | 主鍵。單一公司編號，或 `R` 加起始行號 |
| `company_no` | A 欄公司編號 |
| `row` | Excel 起始行 |
| `active` | 是否現行 |
| `status` | `active`、`inactive`、`resigned`、`deregistered` |
| `label_color` | A 欄底色，6 位十六進位、不含 `#` |
| `color_group` | `orange` 正常現行、`red` inactive、`other` 其他顏色、`none` 沒有底色 |
| `general` | `name_en`、`name_zh`、`ci_no`、`br_no`、`incorporation_date` |
| `issued_shares` | 已發行股數 |
| `directors`、`shareholders`、`company_secretaries`、`other_parties` | 人物陣列 |
| `registered_offices`、`branches`、`share_capital` | 各自的紀錄陣列 |
| `shareholding_summary` | 計入總數的現行持股 |
| `issues` | 這間公司的問題 |
| `imported_from`、`created_at`、`updated_at` | 匯入與更新時間 |

人物：

| 欄位 | 說明 |
| --- | --- |
| `party_id` | 穩定識別，12 位十六進位 |
| `label` | 例如 `DIR1`、`SH2`、`CS1`。可以重複 |
| `name_en`、`name_zh`、`date_of_birth` | 各一組 |
| `ci_no`、`br_no` | 各一組。DIR、SH、Com Sec 的畫面都有這兩格 |
| `appoint_date`、`resign_date` | 各一組 |
| `id_numbers` | 證件。畫面只呈現一組現行證件，另加 HKID / Passport 勾選 |
| `addresses` | 地址。現行只呈現一組四格，加開始與結束日期 |
| `phones` | 電話。畫面比已儲存的號碼多一格空白 |
| `emails` | 電郵 |
| `other` | 未定義。沒有內容時畫面不顯示這個區塊 |
| `shareholdings` | 轉讓前紀錄與最新持股 |
| `outdated`、`outdated_reasons` | 是否過期，以及原因 |
| `current_shares`、`current_holding_row`、`percentage` | 由最新持股算出 |

地址：

```json
{
  "record_id": "a1b2c3d4e5f6",
  "lines": ["Flat A", "1/F", "Example Building", "Hong Kong"],
  "from": "2020-01-01",
  "to": null,
  "row": 12,
  "outdated": false,
  "outdated_reasons": []
}
```

`lines` 長度是 4，依序是地址 1 至 4。

問題：

```json
{
  "row": 12,
  "severity": "warning",
  "code": "ADDRESS_SUSPICIOUS",
  "message": "……",
  "field": "address.line.4"
}
```

`severity` 是 `error`、`warning` 或 `info`。`field` 用來對到畫面欄位。`row` 是 Excel 行號。頁面上「資料問題（附 Excel 行號）」的每一列可點，會展開 OUTDATED 並捲到該欄。

## Excel 如何變成文件

表頭是連續兩列。第二列要同時有 Date of birth、CI、BR。表頭之前的列會略過，不寫入公司資料。

公司由 A 欄開始。空的黃列分隔公司。A 欄是橙色或數字時開始一間公司。A 欄紅色，或註明 Resigned、Dereg，公司為 inactive。

過期是「或」：儲存格是設定的藍色，或結束／辭任欄有真正日期。不清楚的文字只產生警告，不標過期。

地址以逗號或中文逗號切開，由後面填入四格，最後一段是地址 4。填好的地址從地址 1 起連續有內容。只填地址 4、只填地址 3 和 4、或空著地址 1 卻填了後面，整段改放 `other`，畫面稱為「未定義」。已經按 1、2、3、4 填好且標成 OUTDATED 的地址，仍留在地址的 OUTDATED 區。

DIR、SH、Com Sec 的現行畫面各只有一組：名字、出生日期、證件、CI、BR、委任、辭任、一組地址，以及該地址的開始與結束日期。

持股若有轉讓，只有最後一筆用來計算現時持股，標成「現行（用作計算）」。它前面的未過期紀錄標成「轉讓前」。系統不會為持股自動新增空白列。

證件號碼符合香港身份證格式時，`id_type` 為 `hkid`。儲存格文字含 passport 時為 `passport`。使用者可改勾選。

缺少中文名、證件或地址不是錯誤。股本必須有資料。公司秘書可以多於一個，但現行公司至少要有一個現任公司秘書。RO 只保留一個現行，以未過期且開始日期最新的一筆為準。

## 母系統對應建議

| 母系統 | 本系統 |
| --- | --- |
| 公司主檔編號 | `company_no`，寫入用 `id` |
| 董事、股東、公司秘書 | `directors`、`shareholders`、`company_secretaries` 的 `party_id` |
| 是否仍在任 | 人物 `outdated == false` 且公司 `active == true` |
| 現時股數 | `current_shares`，百分比用 `percentage` |
| 通訊地址 | 人物 `addresses` 裡 `outdated == false` 的那一組 `lines` |
| 需人工處理 | `issues` 裡 `severity` 不是 `info` 的項目 |

讀取範例：

```http
GET /api/database
```

找到 `company_no` 後，只提交改過的路徑：

```http
POST /api/companies/351/field
Content-Type: application/json

{"path": ["company_secretaries", "ab12cd34ef56", "phones", "1", "value"], "value": "21234567"}
```

回應裡的整間公司就是新狀態。

## 保安

- 服務綁在本機。MongoDB 沒有對應到主機埠。
- API 沒有認證。反向代理必須在轉發前完成母系統的登入檢查。
- 匯入與匯出都包含身份證明與地址。記錄與備份要按個人資料處理。
- 程式庫不要提交 Excel、`output/` 的 JSON，或資料庫傾印。`cdd-viewer/output/` 與 `*.xlsx` 已在上層 `.gitignore`。

## 目錄

```text
cdd-viewer/
  app/main.py            HTTP API 與匯入
  app/store.py           MongoDB 與記憶體儲存
  app/documents.py       新增、改欄位、OUTDATED、地址與持股規則
  app/excel_parser.py    Excel 轉公司文件
  app/cli.py             離線轉 JSON
  app/static/            頁面
  tests/                 解析與文件規則測試
  docker-compose.yml
  Dockerfile
```
