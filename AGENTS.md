# AGENTS.md — операционный контекст для агентов

Что это за проект и для пользователей — см. `README.md`. Здесь — как с этим работать
и какие грабли уже найдены (не наступать повторно).

## Состав

- `server.py` — сам MCP-сервер: `MCPServer` из **mcp 2.x**, транспорт stdio.
  Инструменты: `udc_sources`, `udc_search`, `udc_get`, `udc_children`, `udc_suggest`.
  Данные — из `DATA_DIR` (env `UDC_DATA_DIR` либо `./data` относительно server.py).
- `api/index.py` — обёртка того же сервера в Streamable HTTP для Vercel:
  stateless + `json_response`, строит сессию поверх приватного
  `server.mcp._lowlevel_server`, отключает DNS-rebinding-защиту, задаёт
  `UDC_DATA_DIR`, добавляет корень репо в `sys.path`.
- `data/*.json` — снимки источников, в git включены (27 МБ + 466 КБ):
  `teacode_udc.json` (126k+ кодов, издание ~2015, местами устарел) и
  `udcsummary_ru.json` (официальный UDC Summary, ~2 700 кодов — по нему сверять
  актуальность; `udc_get` сам предупреждает о расхождениях источников).
- `scripts/scrape_teacode.py` (~10–15 мин), `scripts/scrape_udcsummary.py` (~15 с) —
  обновление снимков (stdlib only). После обновления — коммит данных + деплой.
- `vercel.json` — деплой; `requirements.txt` — не используется платформой (см. грабли).
- `tests/smoke_test.py` — e2e всех инструментов по stdio.
- Токен Vercel: `VERCEL_TOKEN` в `~/.zshenv` (не в репо; значение нигде не дублировать).

## Проверка изменений

```bash
uv sync                                        # восстановить .venv при необходимости
uv run python tests/smoke_test.py              # быстрый критерий: e2e по stdio
uv run uvicorn api.index:app --port 8377       # локальная проверка HTTP-обёртки
# затем POST http://127.0.0.1:8377/mcp с JSON-RPC initialize / tools/call
```

## Деплой и проверки на проде

- Проект linked (`.vercel/project.json`): `ruudc-mcp`, team `acme-f271`
  (`team_y9rKmS0v8AforZ5xnifkdcg7`), projectId `prj_7njzPSCe7KcEeQ0ZLUVWecA3O8Ap`.
- Деплой: `vercel deploy --prod --yes --token "$VERCEL_TOKEN"` (~30 c, заливает 29 МБ данных).
- Прод-endpoint: `https://ruudc-mcp.vercel.app/mcp` (публичный, read-only).
- Живость проверять **POST**-ом (initialize, `Accept: application/json, text/event-stream`).
  GET почти ничего не говорит: после rewrite это SSE-пинг, а внешние фетчеры с
  таймаутом 10–12 с не дожидаются холодного старта (10–25 с) — их «ошибки» не баг.
- Логи/состояние — через `vercel logs ruudc-mcp.vercel.app --json` и `vercel inspect ...
  --logs` (api.vercel.com стабильно доступен; сам `*.vercel.app` из РФ местами
  пропускает TLS не с первой попытки — это сеть, не приложение).

## Грабли Vercel (воспроизведено, не повторять)

1. Новый Python-пайплайн (pyproject + `uv.lock`): зависимости ставит **uv**,
   `requirements.txt` игнорируется. Собирается ровно **одна** функция-приложение —
   `api/index.py`; прочие `api/*.py` не маршрутизируются (404), даже при явном
   перечислении в `functions`.
2. **Lifespan — только `@contextlib.asynccontextmanager`.** Голый async-генератор
   (Starlette считает его deprecated) приводит к тому, что каждый запрос висит до
   «Vercel Runtime Timeout Error: Task timed out after 60 seconds», при этом 404-роуты
   работают и «session manager started» в логах есть. Симптом: ответ 200 в логах, но
   клиент ничего не получает.
3. **Internal rewrites: функция видит путь-назначение** (`/api/index.py`), а не
   исходный (`/mcp`). Внутри одного приложения разные публичные пути после rewrite
   не различить; отдельные health-функции не работают (см. п. 1).
4. `functions.*.includeFiles` — **строка-glob** (`"{data/**,server.py}"`), не массив,
   иначе `Invalid vercel.json`.
5. **DNS-rebinding защита SDK**: при дефолтном `host="127.0.0.1"` включается
   автоматически и режет все запросы с чужим Host. На Vercel обязательно явно
   `TransportSecuritySettings(enable_dns_rebinding_protection=False)`.
6. Для serverless обязательны `stateless=True` и `json_response=True`
   (никаких SSE-сессий и привязки к инстансу).
7. **Deployment Protection** включается у нового проекта по умолчанию и блокирует
   публичный доступ. Отключение через API:
   `PATCH /v9/projects/ruudc-mcp?teamId=…` с телом `{"ssoProtection": null}`
   (именно `null`; `{"deploymentType":"none"}` даёт ошибку). Если проект пересоздадут —
   включить заново, иначе клиенты получат редирект на логин Vercel.
8. API рассчитан на **mcp 2.x** (`mcp.server.mcpserver.MCPServer`,
   `streamable_http_app`, приватный `_lowlevel_server`). При апгрейде mcp —
   перепроверить `api/index.py` и локально, и на проде.
9. Холодный старт 10–25 с — это парсинг 27 МБ JSON при импорте. Если станет проблемой:
   уменьшать/выносить датасет, а не бороться с платформой.

## Git

- Remote: `git@github.com:madrugado/ru-udc-mcp.git`, ветка `main`.
- В репо не попадают: `.venv/`, `.vercel/`, `.env*`, `.zcode/` (локальный конфиг
  с машинными путями), `__pycache__/`, логи краулеров. Токенов в репо нет —
  перед пушем новых файлов со строками подключения быстро проверять grep-ом.
