#!/usr/bin/env node
/* RuUDC — MCP-сервер для УДК (stdio). Логика и форматы ответов — порт server.py;
 * данные грузятся лениво: локальный UDC_DATA_DIR или статика UDC_DATA_BASE_URL
 * (по умолчанию https://ru-udc-app.tatnet.app) — см. lib.mjs.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import {
  MAX_CHILDREN, SOURCES, SOURCE_BY_NAME, resolveSources, unavailableSources,
  firstRec, childrenOf, ancestorsOf, teacodeUrl, searchInSource, tokens, stem,
} from "./lib.mjs";

const server = new McpServer(
  { name: "ruudc", version: "0.1.0" },
  {
    instructions:
      "Сервер УДК (Universal Decimal Classification). Ищите коды через udc_search / " +
      "udc_suggest, смотрите иерархию через udc_get и udc_children. Основной источник — " +
      "'triumph' (современная редакция 2026, выдаётся первым во всех ответах): " +
      "давайте его коды, если источники расходятся. 'summary' — официальный свод UDC " +
      "Consortium (единственный со вспомогательными таблицами определителей), " +
      "'teacode' — детальные таблицы старого русского издания (самая большая глубина, " +
      "но местами устаревшие формулировки; помечен cancelled не использовать). " +
      "УДК-композиции (уточнение темы) строятся " +
      "знаками ':' (отношение), '+' (объединение), '/' (диапазон) из простых кодов.",
  },
);

const text = (obj) => ({ content: [{ type: "text", text: JSON.stringify(obj) }] });
const SOURCE_ENUM = z.enum(["all", "triumph", "summary", "teacode"]);

/* ------------------------------------------------------------ инструменты */

server.registerTool(
  "udc_sources",
  {
    title: "Источники УДК",
    description: "Список доступных источников УДК: объём, происхождение, дата обновления.",
    inputSchema: {},
  },
  async () => {
    const out = [];
    for (const s of SOURCES) {
      await s.ensureMeta();
      const info = { name: s.name, codes: s.recordsCount, ok: !s.failed, primary: s === SOURCES[0] };
      if (s.failed) info.error = s.failed;
      Object.assign(info, {
        base_url: s.meta?.base_url ?? null,
        retrieved: s.meta?.retrieved ?? null,
        language: s.meta?.language ?? null,
        license: s.meta?.license ?? null,
        edition_note: s.meta?.edition_note ?? null,
      });
      out.push(info);
    }
    return text({ sources: out, total_codes: out.reduce((n, i) => n + i.codes, 0) });
  },
);

server.registerTool(
  "udc_search",
  {
    title: "Поиск по УДК",
    description:
      "Поиск кодов УДК по коду (префикс) и/или по названию (русские или английские слова). " +
      "query: строка запроса, например 'нейронные сети', 'машинное обучение', '621.37', 'gis'. " +
      "source: 'all' | 'teacode' | 'summary' | 'triumph'. limit: максимум результатов на источник. " +
      "Возвращает code_matches (по коду) и text_matches (по названиям, с релевантностью).",
    inputSchema: {
      query: z.string().describe("поисковый запрос: слова темы или префикс кода"),
      source: SOURCE_ENUM.default("all").describe("источник: all | triumph | summary | teacode"),
      limit: z.number().int().min(1).max(50).default(15).describe("максимум результатов на источник"),
    },
  },
  async ({ query, source, limit }) => {
    const { srcs, err } = await resolveSources(source);
    if (err) return text({ ok: false, error: err });
    query = (query || "").trim();
    if (!query) return text({ ok: false, error: "пустой запрос" });
    limit = Math.max(1, Math.min(Math.trunc(limit), 50));
    const results = {};
    let total = 0;
    for (const s of srcs) {
      const { codeMatches, textMatches } = await searchInSource(s, query, limit);
      const cm = [];
      for (const c of codeMatches) {
        const r = await firstRec(s, c);
        cm.push({
          code: c,
          title: r?.title || "",
          source: s.name,
          has_children: (await childrenOf(s, c)).length > 0,
          ...(r?.cancelled ? { cancelled: true } : {}),
        });
      }
      const tm = textMatches.map(({ sc, code, i, sh, r }) => ({
        code,
        title: r.title || "",
        source: s.name,
        score: Math.round(sc * 1000) / 1000,
        has_children: (sh.children.get(code) || []).length > 0,
        ...(r.cancelled ? { cancelled: true } : {}),
      }));
      results[s.name] = { code_matches: cm, text_matches: tm };
      total += cm.length + tm.length;
    }
    if (total === 0) {
      results.hint =
        "Ничего не найдено. Попробуйте более общие термины, другую форму слова " +
        "(УДК-заголовки часто в единственном числе: 'нейронная сеть'), или источник по отдельности.";
    }
    const skip = unavailableSources();
    if (skip.length) results.unavailable_sources = skip;
    return text({ ok: true, query, results });
  },
);

server.registerTool(
  "udc_get",
  {
    title: "Карточка кода УДК",
    description:
      "Полная карточка кода УДК: описание, примечания, цепочка предков, прямые дети. " +
      "code: код УДК, например '621.372', '004.9', '616-006'. Составные коды ('621.372:004.9', " +
      "'62+68') автоматически раскладываются на составляющие с объяснением.",
    inputSchema: {
      code: z.string().describe("код УДК, простой или составной"),
      source: SOURCE_ENUM.default("all").describe("источник: all | triumph | summary | teacode"),
    },
  },
  async ({ code, source }) => {
    const { srcs, err } = await resolveSources(source);
    if (err) return text({ ok: false, error: err });
    code = (code || "").trim();
    if (!code) return text({ ok: false, error: "пустой код" });

    const entries = [];
    for (const s of srcs) {
      const r = await firstRec(s, code);
      if (r) entries.push(await entry(s, code, r));
    }

    if (!entries.length) {
      const parts = [...new Set(code.split(/[+:\[\]]/).map((p) => p.trim()).filter(Boolean))];
      if (parts.length > 1) {
        const resolved = [];
        for (const p of parts) {
          const found = [];
          for (const s of srcs) {
            const pr = await firstRec(s, p);
            if (pr) found.push({ code: p, title: pr.title || "", source: s.name });
          }
          resolved.push({ part: p, matches: found });
        }
        return text({
          ok: true,
          code,
          compound: true,
          note:
            "Это составной код УДК (знаки ':' — отношение тем, '+' — объединение, " +
            "':' повторяется между частями). Ниже — найденные составляющие; " +
            "уточните каждую через udc_get при необходимости.",
          parts: resolved,
        });
      }
      return text({
        ok: false,
        error: `код ${JSON.stringify(code)} не найден`,
        hint: "Проверьте написание через udc_search (префикс кода или слова из названия).",
      });
    }

    const warnings = [];
    for (let i = 0; i < entries.length; i++) {
      for (let j = i + 1; j < entries.length; j++) {
        const a = entries[i], b = entries[j];
        if (a.title && b.title && bigramSim(a.title, b.title) < 0.5) {
          warnings.push(
            `Источники '${a.source}' и '${b.source}' по-разному раскрывают этот код ` +
            `(«${a.title.slice(0, 60)}» vs «${b.title.slice(0, 60)}»). teacode основан на старом ` +
            "русском издании УДК (особенно устарели разделы 2 'Религия', 60, 79). " +
            "Основной источник — triumph: используйте его трактовку; summary и " +
            "triumph отражают актуальную редакцию.",
          );
        }
      }
    }
    const resp = { ok: true, code, entries };
    if (warnings.length) return text({ ...resp, warnings });
    const skip = unavailableSources();
    if (skip.length) resp.unavailable_sources = skip;
    return text(resp);
  },
);

server.registerTool(
  "udc_children",
  {
    title: "Дочерние коды УДК",
    description:
      "Прямые дочерние коды УДК (один уровень вниз). Удобно для обзора раздела. " +
      "code: код УДК или '' для корневых разделов.",
    inputSchema: {
      code: z.string().describe("код УДК; '' — корневые разделы"),
      source: SOURCE_ENUM.default("all").describe("источник: all | triumph | summary | teacode"),
      limit: z.number().int().min(1).max(MAX_CHILDREN).default(100).describe("максимум детей на источник"),
    },
  },
  async ({ code, source, limit }) => {
    const { srcs, err } = await resolveSources(source);
    if (err) return text({ ok: false, error: err });
    code = (code || "").trim();
    if (!code) return text({ ok: false, error: "пустой код (для корня используйте '')" });
    limit = Math.max(1, Math.min(Math.trunc(limit), MAX_CHILDREN));
    const out = {};
    let total = 0;
    for (const s of srcs) {
      const kids = await childrenOf(s, code);
      if (code !== "" && !kids.length && !(await firstRec(s, code))) {
        out[s.name] = { error: "код не найден" };
        continue;
      }
      const list = [];
      for (const c of kids.slice(0, limit)) {
        const r = await firstRec(s, c);
        list.push({ code: c, title: r?.title || "", has_children: (await childrenOf(s, c)).length > 0 });
      }
      out[s.name] = { children: list, total: kids.length, truncated: Math.max(0, kids.length - limit) };
      total += kids.length;
    }
    const resp = { ok: true, code, sources: out };
    const skip = unavailableSources();
    if (skip.length) resp.unavailable_sources = skip;
    return text(resp);
  },
);

server.registerTool(
  "udc_suggest",
  {
    title: "Подбор кодов УДК",
    description:
      "Подбор кодов УДК по свободному тексту (тема статьи, аннотация, ключевые слова). " +
      "Оценивает покрытие текста взвешенными терминами заголовков УДК; возвращает топ-коды " +
      "с цепочкой предков (path) для выбора уровня детализации. Для составного кода " +
      "объедините лучшие независимые коды знаком ':' или '+', см. примечание в ответе.",
    inputSchema: {
      text: z.string().describe("свободный текст: аннотация, тема, ключевые слова"),
      source: SOURCE_ENUM.default("all").describe("источник: all | triumph | summary | teacode"),
      limit: z.number().int().min(1).max(20).default(8).describe("максимум подсказок на источник и в top"),
    },
  },
  async ({ text: rawText, source, limit }) => {
    const { srcs, err } = await resolveSources(source);
    if (err) return text({ ok: false, error: err });
    const query = (rawText || "").trim();
    if (!query) return text({ ok: false, error: "пустой текст" });
    limit = Math.max(1, Math.min(Math.trunc(limit), 20));
    const qTokens = tokens(query);
    if (!qTokens.length) {
      return text({
        ok: false,
        error: "в тексте нет содержательных терминов (только стоп-слова)",
        stopwords_dropped: query.split(/\s+/).length,
      });
    }
    const out = {};
    const bestOverall = [];
    for (const s of srcs) {
      // 200 — щедрая надборка сверху, дальше режем порогом 0.15 и limit (как в server.py)
      const { textMatches } = await searchInSource(s, query, 200);
      const top = [];
      for (const { sc, code, i, sh, r } of textMatches) {
        if (sc < 0.15) break;
        const path = await ancestorsOf(s, code);
        top.push({
          code,
          title: r.title || "",
          source: s.name,
          score: Math.round(sc * 1000) / 1000,
          matched_terms: qTokens.filter((q) => sh.titleTokens[i].has(stem(q))).slice(0, 8),
          path: path.filter((a) => a.title).map((a) => `${a.code} ${a.title}`).join(" > ") || null,
          has_children: (sh.children.get(code) || []).length > 0,
          ...(r.cancelled ? { cancelled: true } : {}),
        });
        if (top.length >= limit) break;
      }
      out[s.name] = { suggestions: top };
      bestOverall.push(...top);
    }
    const bestByCode = new Map();
    for (const item of bestOverall) {
      const cur = bestByCode.get(item.code);
      const rank = (it) => [SOURCE_BY_NAME[it.source]?.prio ?? 9, -it.score];
      if (!cur || ltRank(rank(item), rank(cur))) bestByCode.set(item.code, item);
    }
    const mergedTop = [...bestByCode.values()]
      .sort((a, b) => b.score - a.score || (SOURCE_BY_NAME[a.source].prio - SOURCE_BY_NAME[b.source].prio) ||
        a.code.length - b.code.length || (a.code < b.code ? -1 : 1))
      .slice(0, limit);
    const resp = {
      ok: true,
      terms: qTokens.slice(0, 20),
      suggestions: out,
      top: mergedTop,
      note:
        "Основной источник — triumph (современная редакция 2026): его трактовка " +
        "приоритетна, при равном счёте код показывается от него. Проверьте уровень " +
        "детализации: широкому обзору соответствует верхний код path, узкой теме — сам " +
        "код. Составные коды: 'A:B' — отношение (например '004.9:621.372'), " +
        "'A+B' — объединение, '(A...)'/'=...'/'-02' — определители места/языка/лиц " +
        "(есть только в summary). Исключённые (cancelled) коды не используйте.",
    };
    const skip = unavailableSources();
    if (skip.length) resp.unavailable_sources = skip;
    return text(resp);
  },
);

/* --------------------------------------------------------------- хелперы */

async function entry(src, code, r) {
  const parent = r.parent || r.parent_hint || "";
  const e = { code, title: r.title || "", source: src.name };
  const note = r.note || r.notes;
  if (note) e.note = note;
  if (r.refs) e.cross_references = r.refs;
  if (r.cancelled) e.cancelled = true;
  if (src.name === "teacode" && r.linked) e.url = teacodeUrl(code);
  e.ancestors = await ancestorsOf(src, code);
  if (parent) e.parent = parent;
  const kids = await childrenOf(src, code);
  e.children = [];
  for (const c of kids.slice(0, MAX_CHILDREN)) {
    const cr = await firstRec(src, c);
    e.children.push({ code: c, title: cr?.title || "" });
  }
  if (kids.length > MAX_CHILDREN) e.children_truncated = kids.length - MAX_CHILDREN;
  return e;
}

function bigramSim(a, b) {
  const grams = (s) => {
    const out = new Set();
    const t = s.toLowerCase();
    for (let i = 0; i < t.length - 1; i++) out.add(t.slice(i, i + 2));
    return out;
  };
  const ga = grams(a), gb = grams(b);
  if (!ga.size || !gb.size) return 0;
  let inter = 0;
  for (const g of ga) if (gb.has(g)) inter++;
  return (2 * inter) / (ga.size + gb.size);
}

const ltRank = (a, b) => a[0] < b[0] || (a[0] === b[0] && a[1] < b[1]);

/* ------------------------------------------------------------------- старт */

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("RuUDC MCP (node) ready. data: " +
  (process.env.UDC_DATA_DIR ? `local ${process.env.UDC_DATA_DIR}` : process.env.UDC_DATA_BASE_URL || "https://ru-udc-app.tatnet.app"));
