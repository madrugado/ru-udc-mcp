/* Данные и текстовая логика RuUDC для node-MCP — порт udctext.py и слоя шардов
 * server.py. Два режима загрузки:
 *   • UDC_DATA_DIR задан         — локальные шарды (data/shards/<name>/...,
 *                                  имена shard-файлов — python quote(key, safe=''));
 *   • иначе UDC_DATA_BASE_URL    — статика (по умолчанию tatnet); имена — hex от
 *                                  ключа (см. scripts/build_site.py), файлы .json.gz
 *                                  распаковываются через DecompressionStream,
 *                                  фолбэк на несжатые .json.
 */

import { readFile } from "node:fs/promises";
import { join } from "node:path";

const DATA_DIR = process.env.UDC_DATA_DIR || "";
const BASE_URL = (process.env.UDC_DATA_BASE_URL || "https://ru-udc-app.tatnet.app").replace(/\/+$/, "");

/* ---------------------------------------------------------- порт udctext */

const RU_STOP = new Set(("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по ее мне " +
  "было вот от меня еще нет о из ему теперь когда даже ну ли если уже или ни быть был него " +
  "до вас ведь там потом себя ничего ей может они тут где есть надо ней для мы их чем была " +
  "сам без чего раз тоже себе под будет кто этот того потому этого какой совсем ним здесь " +
  "этом один почти мой тем чтобы нее куда зачем всех можно при два об другой хоть после над " +
  "больше тот через эти нас про всего них эта много три эту моя своей этой перед иногда том " +
  "такой им более всегда между поэтому также также т.е т.д т.п т.п. т.д.").split(" "));
const EN_STOP = new Set(("the a an of and or to in on for with as by at is are be this that it its from into about " +
  "not no nor but if then than so such can could may might will would should have has had " +
  "was were been being do does did done their there these those which who whom what when " +
  "where why how all any both each few more most other some only own same too very").split(" "));
export const STOP = new Set([...RU_STOP, ...EN_STOP]);

const HOMO = { a: "а", e: "е", y: "у", o: "о", p: "р", c: "с", k: "к", m: "м", x: "х" };

export function norm(s) {
  s = String(s || "").toLowerCase().replace(/ё/g, "е").replace(/[aeyopckmx]/g, (ch) => HOMO[ch]);
  s = s.replace(/[^\p{L}\p{N}_\s:./+\-()]+/gu, " ");
  return s.replace(/\s+/g, " ").trim();
}

const STEM1 = ["иями", "ями", "ами", "иями", "иях", "ях", "ах", "ов", "ев", "ой", "ый", "ий",
  "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ому", "ему", "ого", "его",
  "ую", "ью", "ия", "ии", "ию", "ом", "ем", "ам", "ям", "ей", "а", "я", "ы",
  "и", "о", "е", "й", "ь", "у", "ю"];
const STEM2 = ["ическ", "ическ", "ческ", "енн", "нн", "изм", "ость", "ств", "ций", "тель"];

export function stem(w) {
  for (const suf of STEM1) {
    if (w.endsWith(suf) && w.length - suf.length >= 3) { w = w.slice(0, w.length - suf.length); break; }
  }
  if (w.endsWith("s") && !w.endsWith("ss") && !w.endsWith("us") && !w.endsWith("is") && w.length >= 5) {
    w = w.slice(0, -1);
  }
  for (const suf of STEM2) {
    if (w.endsWith(suf) && w.length - suf.length >= 3) { w = w.slice(0, w.length - suf.length); break; }
  }
  return w;
}

export function tokens(s) {
  const out = [];
  for (let t of norm(s).split(/[\s/:+()\[\];,.]+/)) {
    t = t.replace(/^-+|-+$/g, "");
    if (!t || STOP.has(t)) continue;
    if (t.length >= 3 || (t.length === 2 && !/^\d+$/.test(t))) out.push(t);
  }
  return out;
}

/* python quote(key, safe='') — encodeURIComponent покрывает всё, кроме !'()* */
export function pyQuote(key) {
  return encodeURIComponent(key).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
}

/* hex-имя шарда без расширения — getRemote сам добавит .json.gz/.json */
export function hexName(key) {
  const bytes = new TextEncoder().encode(key);
  let h = "";
  for (const b of bytes) h += b.toString(16).padStart(2, "0");
  return h;
}

/* --------------------------------------------------------------- загрузка */

const inflight = new Map();

async function fetchJson(base, gz) {
  const url = base + (gz ? ".json.gz" : ".json");
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  let text;
  if (gz) text = await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).text();
  else text = await res.text();
  return JSON.parse(text);
}

async function getRemote(basePath) {
  if (inflight.has(basePath)) return inflight.get(basePath);
  const p = (async () => {
    if (typeof DecompressionStream === "function") {
      try {
        return await fetchJson(BASE_URL + basePath, true);
      } catch (e) {
        if (!/HTTP 404/.test(String(e.message))) throw e;
      }
    }
    return fetchJson(BASE_URL + basePath, false);
  })();
  inflight.set(basePath, p);
  p.catch(() => inflight.delete(basePath));
  return p;
}

async function loadJson(name, file) {
  if (DATA_DIR) return JSON.parse(await readFile(join(DATA_DIR, "shards", name, `${file}.json`), "utf8"));
  return getRemote(`/data/shards/${name}/${file}`);
}

async function loadShardRecords(name, key) {
  if (DATA_DIR) {
    return JSON.parse(await readFile(join(DATA_DIR, "shards", name, "shards", `${pyQuote(key)}.json`), "utf8")).records || [];
  }
  return (await getRemote(`/data/shards/${name}/shards/${hexName(key)}`)).records || [];
}

/* ---------------------------------------------------------- шарды/источники */

export const MAX_CHILDREN = 500;

export function buildShard(recs) {
  const byCode = new Map(), children = new Map();
  const normTexts = [], stemTexts = [], titleTokens = [];
  const df = new Map();
  for (const r of recs) {
    const code = r.code;
    if (!byCode.has(code)) byCode.set(code, []);
    byCode.get(code).push(r);
    const parent = r.parent || r.parent_hint || "";
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(code);
    const title = r.title || "", notes = r.note || r.notes || "";
    normTexts.push(norm(title) + " | " + norm(notes));
    const tt = new Set(tokens(title).map(stem));
    titleTokens.push(tt);
    stemTexts.push(tokens(title + " " + notes).map(stem).join(" "));
    for (const t of tt) df.set(t, (df.get(t) || 0) + 1);
  }
  const n = Math.max(recs.length, 1);
  const idf = {};
  for (const [t, c] of df) idf[t] = Math.log(1 + n / c);
  return { recs, byCode, children, normTexts, stemTexts, titleTokens, idf };
}

export class Source {
  constructor(def) {
    Object.assign(this, def);
    this.meta = null; this.vocab = null; this.idfGlobal = {}; this.topChildren = {};
    this.shardKeySet = new Set(); this.shards = new Map(); this.failed = "";
  }
  async ensureMeta() {
    if (this.meta || this.failed) return;
    try {
      this.meta = await loadJson(this.name, "meta");
      this.shardKeySet = new Set(this.meta.shard_keys || []);
      this.idfGlobal = this.meta.idf || {};
      this.topChildren = this.meta.top_children || {};
    } catch (e) { this.failed = `не удалось загрузить meta ${this.name}: ${e.message}`; }
  }
  async ensureVocab() {
    await this.ensureMeta();
    if (this.failed || this.vocab) return;
    try { this.vocab = await loadJson(this.name, "vocab"); }
    catch { this.vocab = {}; }
  }
  async shard(key) {
    let d = this.shards.get(key);
    if (d) return d;
    d = buildShard(await loadShardRecords(this.name, key));
    this.shards.set(key, d);
    return d;
  }
  get recordsCount() {
    return (this.meta && typeof this.meta.records === "number") ? this.meta.records : 0;
  }
}

export const SOURCES = [
  new Source({ name: "triumph", prio: 0 }),
  new Source({ name: "summary", prio: 1 }),
  new Source({ name: "teacode", prio: 2 }),
];
export const SOURCE_BY_NAME = Object.fromEntries(SOURCES.map((s) => [s.name, s]));

export async function resolveSources(source) {
  let wanted;
  if (source === "all") wanted = SOURCES;
  else if (SOURCE_BY_NAME[source]) wanted = [SOURCE_BY_NAME[source]];
  else return { err: `неизвестный источник: ${source} (доступны: all, triumph, summary, teacode)` };
  const ok = [];
  for (const s of wanted) {
    await s.ensureMeta();
    if (!s.failed) ok.push(s);
  }
  if (!ok.length) return { err: wanted.map((s) => `источник ${s.name}: ${s.failed}`).join("; ") };
  return { srcs: ok };
}

export function unavailableSources() {
  return SOURCES.filter((s) => s.failed).map((s) => `${s.name}: ${s.failed}`);
}

/* ----------------------------------------------------------------- lookup */

export async function firstRec(src, code) {
  await src.ensureMeta();
  if (src.failed) return null;
  const key = code.slice(0, 2);
  if (!src.shardKeySet.has(key)) return null;
  const sh = await src.shard(key);
  const lst = sh.byCode.get(code);
  return lst ? lst[0] : null;
}

export async function childrenOf(src, code) {
  await src.ensureMeta();
  if (src.failed) return [];
  if (code === "" || code.length <= 1) return src.topChildren[code] || [];
  const key = code.slice(0, 2);
  if (!src.shardKeySet.has(key)) return [];
  const sh = await src.shard(key);
  return sh.children.get(code) || [];
}

export async function ancestorsOf(src, code) {
  const chain = [];
  const seen = new Set([code]);
  let cur = await firstRec(src, code);
  while (cur && chain.length < 30) {
    const parent = cur.parent || cur.parent_hint || "";
    if (!parent || seen.has(parent)) break;
    const p = await firstRec(src, parent);
    if (!p) { chain.push({ code: parent, title: "" }); break; }
    chain.push({ code: parent, title: p.title || "" });
    seen.add(parent);
    cur = p;
  }
  return chain.reverse();
}

export function teacodeUrl(code) {
  const c = code.trim();
  if (!/^\d/.test(c)) return null;
  const sec = c.startsWith("00") ? "00" : ("12789".includes(c[0]) ? c[0] : c.slice(0, 2));
  return `http://teacode.com/online/udc/${sec}/${c}.html`;
}

/* ---------------------------------------------------------------- скоринг */

export function matchScore(data, i, qTokens, phrase, idfSum, idfMap) {
  const text = data.normTexts[i], stems = data.stemTexts[i], ttoks = data.titleTokens[i];
  let score = 0;
  for (const t of qTokens) {
    const st = stem(t);
    const w = idfMap[st] || data.idf[st] || 1.0;
    if (ttoks.has(st)) score += w * 1.3;
    else if (stems.includes(st)) score += w;
    else if (t.length >= 4 && text.includes(t)) score += w * 0.7;
  }
  if (idfSum > 0) score /= idfSum;
  if (phrase && phrase.length >= 4 && text.includes(phrase)) score += 0.5;
  return score;
}

export async function searchInSource(src, query, limit) {
  await src.ensureVocab();
  const out = { codeMatches: [], textMatches: [] };
  if (src.failed) return out;

  const qNorm = norm(query).replace(/\s+/g, "");
  if (qNorm) {
    const cut = qNorm.slice(0, 2);
    const found = [];
    for (const key of [...src.shardKeySet].filter((k) => k.startsWith(cut)).sort()) {
      const sh = await src.shard(key);
      for (const code of sh.byCode.keys()) {
        if (code === qNorm || code.startsWith(qNorm)) found.push(code);
      }
    }
    found.sort((a, b) => a.length - b.length || (a < b ? -1 : 1));
    out.codeMatches = found.slice(0, limit);
  }

  const qTokens = tokens(query);
  if (qTokens.length) {
    const stems = qTokens.map(stem);
    const idfSum = stems.reduce((s, st) => s + (src.idfGlobal[st] ?? 1.0), 0);
    const keys = new Set();
    for (const st of stems) for (const k of (src.vocab[st] || [])) keys.add(k);
    const phrase = norm(query);
    const scored = [];
    for (const key of [...keys].sort()) {
      const sh = await src.shard(key);
      for (let i = 0; i < sh.recs.length; i++) {
        const sc = matchScore(sh, i, qTokens, phrase, idfSum, src.idfGlobal);
        if (sc > 0) scored.push([sc, sh.recs[i].code, i, sh]);
      }
    }
    scored.sort((a, b) => b[0] - a[0] || a[1].length - b[1].length || (a[1] < b[1] ? -1 : 1));
    out.textMatches = scored.slice(0, limit).map(([sc, code, i, sh]) => {
      const r = sh.recs[i];
      return { sc, code, i, sh, r };
    });
  }
  return out;
}
