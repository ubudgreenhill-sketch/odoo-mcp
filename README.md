# odoo-mcp — расходы из Google-таблицы → Odoo (vendor bills)

MCP-коннектор для Claude и фоновая синхронизация. Читает лист **Expenses** файла
«Green Hills - Incoming invoices 2025-2026.xlsx» на Google Диске и создаёт в Odoo **vendor bills**
(черновики) с прикреплёнными PDF из папки месяца.

## Защита от повторного внесения

| Слой | Как работает | Результат |
|---|---|---|
| Ключ | Каждый счёт получает Bill Reference `EXP-0007` (№ строки) | строка с ключом в Odoo → `exists`, не создаётся никогда |
| Поиск в Odoo | vendor bills / receipts / refunds / проводки и платежи / hr.expense с той же суммой и валютой (±0.1 %) в окне ±5 дней | тот же поставщик → `duplicate`; другой поставщик → `possible_duplicate`. **Не создаются**, пока не указать `force_numbers` или не привязать через `link_existing_bill` |
| Дубли в таблице | одинаковые получатель + сумма + дата + документ | `possible_duplicate` |

Повторный запуск безопасен: если запись в Odoo создалась, а связь потерялась — ключ всё равно найдётся.

## Инструменты MCP

- `preview_sync` — пробный прогон: что новое, что уже есть, что похоже на дубль (ничего не создаёт)
- `run_sync` — создать новые счета (`force_numbers` — создать и подозрительные, `post` — сразу провести)
- `link_existing_bill` — «строка 2 = счёт 1234 в Odoo» (добавляет ключ к его reference)
- `check_duplicate` — проверить произвольный расход
- `reconcile_sheet_vs_odoo` — сверка за период: итоги, чего нет в Odoo, что в Odoo, но не в таблице, расхождения сумм/дат
- `unreconciled_bank_lines` — неразнесённые банковские строки + подходящие открытые счета по сумме
- `last_sync_report`, `connector_status`

### Отчёты по продажам (только чтение, в Odoo ничего не меняют)

- `sales_by_shop(date_from, date_to, shop?, product?, include_draft?, by_month?, include_rows?)` — продажи по
  клиентским счетам и кредит-нотам (по дате счёта): итоги, по месяцам, по магазинам, по товарам и строки
  месяц × магазин × товар — количество, единица, сумма без НДС, с НДС, оплачено / не оплачено (статус оплаты
  счёта, пропорционально строкам), номера счетов. Возвраты вычитаются; черновики — только с `include_draft`.
- `stock_moves_by_location(date_from, date_to, location?, product?)` — движение товара по внутренним локациям
  (консигнационные магазины): получено, отгружено покупателям, возвраты, перемещено на другие склады,
  инвентаризация — по месяцам и товарам. Для сверки консигнации с выставленными счетами.

## Установка

### 1. Odoo API key
Odoo → аватар → **My Profile / Preferences → Account Security → New API Key**. Нужен тариф с External API (Custom).
`ODOO_DB` — обычно поддомен (`green-hills-inc`); проверить: `https://green-hills-inc.odoo.com/web/database/list` или Settings → About.

### 2. Google service account
1. https://console.cloud.google.com → новый проект → **APIs & Services → Enable APIs → Google Drive API**.
2. **IAM & Admin → Service Accounts → Create** → Keys → Add key → JSON (скачается файл).
3. На Google Диске откройте папку с таблицей и папками месяцев → **Share** → e-mail сервисного аккаунта (`…@….iam.gserviceaccount.com`) → **Viewer**.
   Доступ к папке нужен, чтобы прикреплять PDF.

### 3. Railway
1. New Project → **Deploy from GitHub repo** → `ubudgreenhill-sketch/odoo-mcp` (Dockerfile подхватится сам).
2. **Variables** — по `.env.example`. `GOOGLE_SERVICE_ACCOUNT_JSON` — всё содержимое JSON-файла.
   `MCP_SECRET` — длинная случайная строка (например, `openssl rand -hex 24`).
3. **Settings → Networking → Generate Domain**. Проверка: `https://<домен>/health` → `{"ok": true, "configured": true}`.

### 4. Подключить к Claude
claude.ai → Settings → **Connectors → Add custom connector** → URL:
`https://<домен>/<MCP_SECRET>/mcp`

### 5. Первый запуск
1. В чате: «проверь статус коннектора» → «сделай preview_sync».
2. Разобрать `duplicate` / `possible_duplicate` (привязать или создать принудительно).
3. «внеси новые» → `run_sync`. Проверить черновики в Odoo.
4. Когда всё ок — `SCHEDULE_ENABLED=true` (каждые `SYNC_INTERVAL_MINUTES`). По расписанию создаются только `new`; подозрительные ждут вашего решения.

## Настройки правил

- `TYPE_ACCOUNT_MAP` — счёт расходов по колонке Type: `{"Expense":"610000","Tax / duty payment":"217000"}`. Пусто → счёт по умолчанию журнала покупок.
- `PARTNER_ALIASES` — разные написания поставщика → одно имя в Odoo.
- `CREATE_PARTNERS=false` — не создавать новых контрагентов (строка уйдёт в `error`).
- `SYNC_FROM_NUMBER` — игнорировать строки с меньшим №.
- `SYNC_TYPES` — синхронизировать только указанные типы.
- Налоги не выделяются: счёт создаётся на полную сумму (как оплачено), чтобы сходилось с банком.
- Колонка «Paid from director personal account (loan)» и Notes попадают во внутреннюю заметку счёта.

## Тесты
`TEST_XLSX=путь/к/файлу.xlsx python tests/test_sync.py` — офлайн-тест с фейковым Odoo.
`python tests/test_sales.py` — офлайн-тест отчётов по продажам.
