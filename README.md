# inventory-scheduler

Standalone Python inventory scheduling application with a Flask JSON API.
The scheduling engine uses only the Python standard library; Flask and its
dependencies are needed only for HTTP access. No chatbot, Azure, Redis, OpenAI,
LLM, map service, credentials, or external service configuration is required.

## Run locally

Use Python 3.11 or newer. From the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

On Windows, activate with `.venv\Scripts\activate` instead.
The API listens on `http://127.0.0.1:5000`.
Alternatively, run `flask --app app run`. The WSGI application is `app:app`;
an application factory is available as `app.create_app`.
The development server is for local use only. Production deployment must provide
a production WSGI server, authentication/access controls, TLS, and request limits;
this application does not implement authentication. Do not expose personnel data
through a public, unauthenticated endpoint.

Save the complete JSON example below as `request.json`, then call:

```sh
curl -X POST http://127.0.0.1:5000/inventory_schedule \
  -H 'Content-Type: application/json' --data-binary @request.json
```

For direct Python use (no Flask installation required by the engine):

```python
import json
from utils.inventory_scheduling import InventorySchedulingAgent

with open("request.json", encoding="utf-8") as source:
    result = InventorySchedulingAgent().generate(json.load(source))
print(json.dumps(result, ensure_ascii=False, indent=2))
```

## Tests

```sh
python -m unittest discover -s tests -v
```

The original inventory engine and API regression tests are preserved, including
Haversine distances, scheduling constraints, validation, and HTTP error handling.
Additional tests exercise the standalone app and check that it does not import
the removed service integrations.

## Migration provenance

Migrated from [maxliu-data/chatbotdemo01](https://github.com/maxliu-data/chatbotdemo01/tree/87934a260e90e5788e1e6ba50f0e96c24a9618c1),
branch `copilot/generate-monthly-schedule-agent`, commit
`87934a260e90e5788e1e6ba50f0e96c24a9618c1`.
The inventory engine, its Python import path, the `POST /inventory_schedule`
contract, inventory tests, and inventory documentation were migrated.
The standalone engine now also minimizes employee round-trip distances as
described below; this optimization is an enhancement to the original scheduler.
The original MIT license is included in `LICENSE`. The source repository is
unchanged. The unrelated generic monthly employee scheduler (`/monthly_schedule`),
chatbot tools/UI, Socket.IO, and cloud integrations are intentionally not migrated.

## 店舖與盤點人員排程

端點為 **`POST /inventory_schedule`**，可直接呼叫
`utils.inventory_scheduling.InventorySchedulingAgent().generate(data)`。
核心只使用 Python 標準函式庫，不需 LLM、地圖 API 或外部服務。

### 八張輸入表

最外層包含 `month`（`YYYY-MM`）及下列 JSON 陣列，日期使用 `YYYY-MM-DD`，
識別碼可使用字串或整數，但同一識別碼的型別須一致（建議使用字串），
布林欄位僅接受 `true`／`false` 或 `1`／`0`：

| JSON 欄位 | 每列欄位 |
| --- | --- |
| `store_requirements` | `store_id`, `required_worker_num`（最終核定人數，不重新依庫存計算） |
| `store_calendar` | `store_id`, `date`, `allowed_am`, `allowed_pm` |
| `store_groups` | `store_group_id`, `store_id`（複數店） |
| `store_locations` | `store_id`, `map_x`, `map_y` |
| `store_areas` | `area_id`, `store_id` |
| `workers` | `worker_char`, `worker_id`, `is_leader`, `area_id`, `map_x`, `map_y` |
| `worker_groups` | `group_id`, `worker_id` |
| `worker_calendar` | `worker_id`, `date`, `allowed_am`, `allowed_pm` |

`store_groups`、`store_locations`、`store_areas` 可省略，預設空陣列。
其餘五張表必填；最多 100 店、200 位員工，每張日曆最多 6,200 列。
不接受未定義欄位、重複主鍵／日曆紀錄或不存在的店舖／員工參照。
沒有日曆紀錄的日期／時段視為**不可安排**，不是預設可出勤。
休假、它業、教育訓練及其他不可出勤行程應反映在 `worker_calendar`。
員工可屬於多個群組，但同一店全體員工必須具有**共同的** `group_id`，
不是僅任兩人之間有群組重疊即可。`worker_char` 只作顯示，不代替唯一 `worker_id`。

`map_x` 為 WGS84 經度（−180 到 180），`map_y` 為緯度（−90 到 90）。
所有距離採 Haversine 球面公式、地球半徑 6,371 公里，**不是車程或道路里程**。
缺漏座標不當成零距離；不會因此猜測可達時間。
單一地點的經緯度須同時提供，或同時省略／設為 `null`；
超出範圍、無限值及非數字座標屬輸入錯誤。

### 最小完整範例

以下兩間複數店排同一天，上午需要兩人、下午需要一人；週六可排：

```json
{
  "month": "2026-10",
  "store_requirements": [
    {"store_id": "S1", "required_worker_num": 2},
    {"store_id": "S2", "required_worker_num": 1}
  ],
  "store_calendar": [
    {"store_id": "S1", "date": "2026-10-03", "allowed_am": 1, "allowed_pm": 0},
    {"store_id": "S2", "date": "2026-10-03", "allowed_am": 0, "allowed_pm": 1}
  ],
  "store_groups": [
    {"store_group_id": "M1", "store_id": "S1"},
    {"store_group_id": "M1", "store_id": "S2"}
  ],
  "store_locations": [
    {"store_id": "S1", "map_x": 121.50, "map_y": 25.04},
    {"store_id": "S2", "map_x": 121.51, "map_y": 25.04}
  ],
  "store_areas": [
    {"area_id": "A1", "store_id": "S1"},
    {"area_id": "A1", "store_id": "S2"}
  ],
  "workers": [
    {"worker_char": "王", "worker_id": "W1", "is_leader": 1, "area_id": "A1", "map_x": 121.50, "map_y": 25.04},
    {"worker_char": "林", "worker_id": "W2", "is_leader": 0, "area_id": "A1", "map_x": 121.51, "map_y": 25.04}
  ],
  "worker_groups": [
    {"group_id": "G1", "worker_id": "W1"},
    {"group_id": "G1", "worker_id": "W2"}
  ],
  "worker_calendar": [
    {"worker_id": "W1", "date": "2026-10-03", "allowed_am": 1, "allowed_pm": 1},
    {"worker_id": "W2", "date": "2026-10-03", "allowed_am": 1, "allowed_pm": 1}
  ]
}
```

### 店況與週期補充資料

僅八張表時，`store_requirements` 視為**上游已確認本月應盤店舖**，
日曆視為已處理未提供的業務條件；系統不會自行猜測 RC／FC、上次盤點、
轉換、改裝或組織歸屬。需要引擎檢查這些條件時，另傳 `store_rules`：

| 規則 | `store_rules` 每店可提供欄位 |
| --- | --- |
| 週期 | `store_id`, `store_type`（`RC`／`FC`）, `last_inventory_date`, `cycle_months`（月份數字陣列） |
| 開店／轉換／續約 | `opened_date`, `converted_date`, `renewed_date`, `conversion_interval_exception` |
| 複數首店 | `first_store_id`（同一複數群組的首店） |
| 接店 | `takeover_date`, `takeover_slot`（`AM`／`PM`） |
| 改裝 | `renovation_start`, `renovation_end`（含起訖日禁排） |
| 日期條件 | `expiry_check_dates`, `weekdays`（星期一＝1、星期日＝7）, `month_part`（`early`／`middle`／`late`） |
| 特殊店況 | `ultra_remote`, `priority`, `status`（`normal`／`closed`／`transfer`／`terminated`／`renewal`）, `status_date` |
| 指定人員 | `required_worker_ids` |
| 組織支援 | `department_id`, `section_id` |

最外層可另提供 `holidays` 日期陣列。員工主檔可補充 `meeting_dates`（課會日 PM 禁排）、
`holiday_available`（假日出勤資格）、`department_id`、`section_id`。
`holidays` 上的日期必須員工有 `holiday_available: true` 且雙方日曆允許才可排；
週六本身不要求另設此旗標，週日仍禁排。
組織限制以最外層 `organizational_support: true` 啟用；啟用時需補齊所有相關
店舖與員工歸屬，`department_id` 使用 `department1`／`department2` 或整數 `1`／`2`，
`section_id` 表示課別。不以 `worker_groups` 猜測部／課。

FC `cycle_months` 須為六個交替月份，例如 `[1, 3, 5, 7, 9, 11]`；
未提供時可依 `last_inventory_date` 推得單／雙月。指定複數首店必須在本次輸入中，
屬於同一複數店群組，且有可確認的 FC 週期。缺乏週期依據不自行猜測。
週期欄位須搭配 `store_type`；RC／FC 皆可提供 `converted_date`，
`renewed_date` 用於 FC 首盤規則，RC 續約店況則使用 `status: "renewal"`。
新開／轉換／續約的複數 FC 依事件月份之後首個符合首店週期的月份安排。
逾期且未完成的 RC 首盤仍列為應盤並提示；未解決的 FC 首盤逾期列入未排清單，
不能以正常雙月週期靜默跳過。
`month_part` 的 `early`／`middle`／`late` 分別為 1–10／11–20／21–月底。
特殊 `status` 須搭配 `status_date` 作為最後允許盤點日期。

### 排程限制與偏好

- 一店占一個半天，每店足額且至少一名 Leader；同人同時段不能重複安排。
- 週日預設禁排，週六可依雙方日曆安排；135／246 指星期，不是日期尾數。
- 複數店以當月應盤成員為單位同日安排；整組無法安排時不只排部分成員。
- 超遠程僅 AM，其參與員工全天保留，必須 AM／PM 均可出勤。
- 接店當日其他複數店須較早完成：PM 接店時其他店限 AM；AM 接店無更早半日時段則回報衝突。
- RC 每月盤點且間隔至少 14 日；轉換間隔例外須明確提供。
- FC 一般間隔為 **46–74 日**。次月首盤／複數首店月份優先於間隔限制，
  但不覆蓋店況、人員可出勤及 Leader 等其他限制；例外需在結果揭露。
- 閉店、轉店、解約、續約店優先且限 AM；效期檢查同日禁排。
- 改裝前 3 個工作日或改裝後 3 個工作日內安排；工作日按週一至週六、
  扣除 `holidays` 計算，與個別員工休假分開。
- 上下午可換組；同組、店間 **10 公里內**及人員間距離仍為搜尋順序的軟性偏好，
  但排入店數相同時，以全體員工往返總距離較短的班表為優先。
  這些偏好不能犧牲共同群組或核定人數。
- 大複數店及特殊店優先；區域匹配、月中地區店與後續外地行程作為排序偏好。
- 一部可跨課支援、二部不能跨課；跨部支援不自動開放，共同員工群組限制仍適用。
- **暫不處理出車、駕駛輪替、油料補助及星期五遠途出車規則**；不納入超市或庫存重算。

### 員工往返距離最小化

目標依序為：

1. 在所有硬性規則下，盡量排入更多應盤店舖；不會為了降低距離而少排店。
2. 排入店數相同時，最小化**全月、全體員工每日往返距離的總和**。
   不是只找最近的上午／下午店，也不是分別保證每位員工的個人路線最短。

員工主檔的 `map_x`／`map_y` 視為住址座標。每天按實際分派計算：

- 上午、下午都有店：住址 → 上午店鋪 → 下午店鋪 → 住址。
- 只有上午或下午有店：住址 → 該店 → 住址。
- 超遠程店雖保留全天，只有一個實際店點，因此也計算住址 → 該店 → 住址。
- 每位員工各自計算再加總；多人同組不視為共乘，不以車輛里程計算。

三段距離都採 Haversine 球面距離，不是道路里程或交通時間。
必須提供**所有本月應盤店及所有輸入員工**的完整座標才會啟用距離最佳化。
任一缺漏時，保留原有排班搜尋，並回傳
`distance_optimization_disabled: missing coordinates` 提醒，不把未知距離當成零。
即使缺座標的員工最終未被安排，仍採此保守退回規則。

找到第一份完整班表後，搜尋仍會在 `search_limit` 範圍內比較其他班表；
同店數、同距離時保留先找到的結果。達搜尋上限就回傳目前找到的最佳班表，
**不保證全域最短**。完整且總距離為零時可提早停止，因已達距離下界。

### 回應與限制

HTTP 200 回傳 `status`（`complete`／`partial`）、`schedule`（店舖、日期、
`slot`、`worker_ids`、`leader_id`）、`unscheduled`（店舖與原因）、
`skipped`（非本月應盤店）、`warnings`、`notes` 及 `metrics`。
`metrics` 包含搜尋統計及實際 AM／PM 銜接的 `transitions`；
同組保持、拆組、距離超過 10 公里或無法計算距離會另行統計／提示。
另提供以下往返距離欄位：

| `metrics` 欄位 | 說明 |
| --- | --- |
| `distance_optimization_enabled` | 是否具備完整座標並啟用往返距離最佳化 |
| `total_commute_distance_km` | 班表中所有員工每日往返距離總和；任一實際路線距離未知則為 `null`，空班表為 `0` |
| `worker_routes` | 每位已安排員工每天的 `worker_id`、`date`、`am_store_id`、`pm_store_id`、`distance_km`；無該半天班別時店號為 `null`，距離未知亦為 `null` |
| `distance_optimal` | 是否已確認在最多可排店數下的距離最小解；座標不足或搜尋達上限時為 `false` |

`status: "complete"` 僅表示應盤店均已排入，**不代表距離已最佳化完成**；
請同時檢查 `distance_optimal` 與 `reached_search_limit`。
未達搜尋上限而完成搜尋（或完整班表已達零距離下界）且已啟用距離最佳化時，
`distance_optimal` 才為 `true`；此判斷只針對模型中的球面距離及限制。

`unscheduled[].diagnostics` 補充可直接確認的原因，例如無可用時段、
複數店無共同日期、人數不足、無 Leader、無共同員工群組或指定人員不可用；
組合衝突保留 `no_feasible_assignment`，搜尋達上限使用 `search_limit`，
不將有限搜尋未找到解誤稱為不存在可行解。
資料不正確回傳 400、非 JSON 回傳 415、請求過大（超過 2 MiB）回傳 413；
API 不回傳內部例外細節。

最外層 `search_limit` 預設 20,000、範圍 1–200,000，限制搜尋節點數。
引擎為有搜尋上限的確定性啟發式排程，不保證全月最優解；`partial`
表示本次未排滿，不等同數學上無解。資料不足、搜尋達上限、無共同可用時段等
情況不能偽裝成完成。班表僅供人工審核，不自動發布，也不保證勞動法規合規。
此 API 不呼叫外部服務、不保存人員資料；部署須提供存取控制，
不可將含人員資料的服務直接公開。
