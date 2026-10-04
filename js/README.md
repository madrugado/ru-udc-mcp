# ruudc-mcp (node)

MCP-сервер для УДК (универсальной десятичной классификации) на Node.js — stdio-транспорт.
Логика и форматы ответов повторяют питоновский `server.py`; данные (280 шардов, три источника)
грузятся **лениво** — при старте только meta+vocab, шарды по запросу.

## Запуск

```bash
npm install
node index.js          # stdio MCP-сервер
```

Данные по умолчанию тянутся со статики `https://ru-udc-app.tatnet.app` (json.gz).
Переменные окружения:

- `UDC_DATA_DIR` — каталог с `data/shards/` (локальные шарды, офлайн; имена файлов —
  python `quote(key, safe='')`);
- `UDC_DATA_BASE_URL` — другой статический базовый URL (hex-имена шардов, см.
  `scripts/build_site.py`).

## Подключение к MCP-клиенту

Claude Desktop / ZCode / другой клиент (`mcpServers`):

```json
{
  "mcpServers": {
    "ruudc": {
      "command": "node",
      "args": ["/absolute/path/to/RuUDC-MCP/js/index.js"]
    }
  }
}
```

После публикации в npm — просто `npx ruudc-mcp`.

## Инструменты

`udc_sources`, `udc_search`, `udc_get`, `udc_children`, `udc_suggest` — те же, что в
питоновской версии (см. корневой README). Источники: `triumph` (основной, «УДК 2026»,
112k кодов), `summary` (официальный UDC Summary, CC BY-NC-ND, 2.7k), `teacode`
(старое издание ~2015, 121k, местами устарел).

Проверка: `uv run python tests/smoke_test_js.py` — e2e обоих режимов данных.
