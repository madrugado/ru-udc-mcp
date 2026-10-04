"use strict";
/* RuUDC web — клиентский поиск по УДК.
 * Порт udctext.py (нормализация/стеммер/токены) и поисковой логики server.py:
 * meta+vocab грузятся один раз на источник, шарды — лениво, по ключам из vocab.
 * Имена шард-файлов — hex от ключа (см. scripts/build_site.py).
 */

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
const STOP = new Set([...RU_STOP, ...EN_STOP]);

// латинские гомоглифы, вкраплённые в кириллицу ("Mедицина")
const HOMO = { a: "а", e: "е", y: "у", o: "о", p: "р", c: "с", k: "к", m: "м", x: "х" };

function norm(s) {
  s = String(s || "").toLowerCase().replace(/ё/g, "е").replace(/[aeyopckmx]/g, (ch) => HOMO[ch]);
  s = s.replace(/[^\p{L}\p{N}_\s:./+\-()]+/gu, " ");
  return s.replace(/\s+/g, " ").trim();
}

const STEM1 = ["иями", "ями", "ами", "иями", "иях", "ях", "ах", "ов", "ев", "ой", "ый", "ий",
  "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ому", "ему", "ого", "его",
  "ую", "ью", "ия", "ии", "ию", "ом", "ем", "ам", "ям", "ей", "а", "я", "ы",
  "и", "о", "е", "й", "ь", "у", "ю"];
const STEM2 = ["ическ", "ическ", "ческ", "енн", "нн", "изм", "ость", "ств", "ций", "тель"];

function stem(w) {
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

function tokens(s) {
  const out = [];
  for (let t of norm(s).split(/[\s/:+()\[\];,.]+/)) {
    t = t.replace(/^-+|-+$/g, "");
    if (!t || STOP.has(t)) continue;
    if (t.length >= 3 || (t.length === 2 && !/^\d+$/.test(t))) out.push(t);
  }
  return out;
}

function hexName(key) {
  const bytes = new TextEncoder().encode(key);
  let h = "";
  for (const b of bytes) h += b.toString(16).padStart(2, "0");
  return h + ".json";
}

/* --------------------------------------------------------------- данные */

const SOURCES = [
  { name: "triumph", label: "Триумф · УДК 2026", hint: "основной источник, современная редакция", prio: 0 },
  { name: "summary", label: "UDC Summary", hint: "официальный свод + определители", prio: 1 },
  { name: "teacode", label: "teacode", hint: "глубокое старое издание, местами устарел", prio: 2 },
];
const MAX_CHILDREN = 500;

let bytesLoaded = 0;
const jsonInflight = new Map();

/* base — путь без расширения: тянем .json.gz (хостинг сам не жмёт) и
 * распаковываем через DecompressionStream; при его отсутствии — .json */
async function fetchJson(base, gz) {
  const url = base + (gz ? ".json.gz" : ".json");
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  let text;
  if (gz) {
    text = await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).text();
  } else {
    text = await res.text();
  }
  bytesLoaded += Number(res.headers.get("content-length") || text.length);
  return JSON.parse(text);
}

async function getJson(base) {
  if (jsonInflight.has(base)) return jsonInflight.get(base);
  const p = (async () => {
    if (typeof DecompressionStream === "function") {
      try {
        return await fetchJson(base, true);
      } catch (e) {
        if (!String(e.message).includes("404")) throw e;
      }
    }
    return fetchJson(base, false);
  })();
  jsonInflight.set(base, p);
  p.catch(() => jsonInflight.delete(base));
  return p;
}

class Source {
  constructor(def) {
    Object.assign(this, def);
    this.meta = null; this.vocab = null; this.idfGlobal = {}; this.topChildren = {};
    this.shardKeySet = new Set(); this.shards = new Map(); this.failed = "";
  }
  async ensureMeta() {
    if (this.meta || this.failed) return;
    try {
      this.meta = await getJson(`data/shards/${this.name}/meta`);
      this.shardKeySet = new Set(this.meta.shard_keys || []);
      this.idfGlobal = this.meta.idf || {};
      this.topChildren = this.meta.top_children || {};
    } catch (e) { this.failed = "индекс недоступен: " + e.message; }
  }
  async ensureVocab() {
    await this.ensureMeta();
    if (this.failed || this.vocab) return;
    try { this.vocab = await getJson(`data/shards/${this.name}/vocab`); }
    catch { this.vocab = {}; } // без vocab текстовый поиск не работает, префиксный — да
  }
  async shard(key) {
    let d = this.shards.get(key);
    if (d) return d;
    const blob = await getJson(`data/shards/${this.name}/shards/${hexName(key).replace(/\.json$/, "")}`);
    d = buildShard(blob.records || []);
    this.shards.set(key, d);
    updateStatus();
    return d;
  }
  get recordsCount() {
    return (this.meta && typeof this.meta.records === "number") ? this.meta.records : 0;
  }
}

function buildShard(recs) {
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

const ALL_SOURCES = SOURCES.map((d) => new Source(d));
const byName = Object.fromEntries(ALL_SOURCES.map((s) => [s.name, s]));

/* ------------------------------------------------------------ lookup */

async function firstRec(src, code) {
  await src.ensureMeta();
  if (src.failed) return null;
  const key = code.slice(0, 2);
  if (!src.shardKeySet.has(key)) return null;
  const sh = await src.shard(key);
  const lst = sh.byCode.get(code);
  return lst ? lst[0] : null;
}

async function childrenOf(src, code) {
  await src.ensureMeta();
  if (src.failed) return [];
  if (code === "" || code.length <= 1) return src.topChildren[code] || [];
  const key = code.slice(0, 2);
  if (!src.shardKeySet.has(key)) return [];
  const sh = await src.shard(key);
  return sh.children.get(code) || [];
}

async function ancestorsOf(src, code) {
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

function teacodeUrl(code) {
  const c = code.trim();
  if (!/^\d/.test(c)) return null;
  const sec = c.startsWith("00") ? "00" : ("12789".includes(c[0]) ? c[0] : c.slice(0, 2));
  return `http://teacode.com/online/udc/${sec}/${c}.html`;
}

/* ------------------------------------------------------------ скоринг */

function matchScore(data, i, qTokens, phrase, idfSum, idfMap) {
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

/* -------------------------------------------------------------- поиск */

async function searchInSource(src, query, limit, minScore, withPhrase) {
  await src.ensureVocab();
  const out = { src, codeMatches: [], textMatches: [], error: src.failed };
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
    const phrase = withPhrase ? norm(query) : "";
    const scored = [];
    for (const key of [...keys].sort()) {
      const sh = await src.shard(key);
      for (let i = 0; i < sh.recs.length; i++) {
        const sc = matchScore(sh, i, qTokens, phrase, idfSum, src.idfGlobal);
        if (sc > 0 && sc >= minScore) scored.push([sc, sh.recs[i].code, i, sh]);
      }
    }
    scored.sort((a, b) => b[0] - a[0] || a[1].length - b[1].length || (a[1] < b[1] ? -1 : 1));
    out.textMatches = scored.slice(0, limit).map(([sc, code, i, sh]) => {
      const r = sh.recs[i];
      return {
        code, title: r.title || "", source: src.name, prio: src.prio,
        score: Math.round(sc * 1000) / 1000,
        hasChildren: (sh.children.get(code) || []).length > 0,
        ...(r.cancelled ? { cancelled: true } : {}),
      };
    });
  }
  return out;
}

function mergeTop(matchLists, limit) {
  const best = new Map();
  for (const list of matchLists) {
    for (const it of list) {
      const cur = best.get(it.code);
      const better = !cur ||
        it.prio < cur.prio ||
        (it.prio === cur.prio && it.score > cur.score);
      if (better) best.set(it.code, it);
    }
  }
  return [...best.values()]
    .sort((a, b) => b.score - a.score || a.prio - b.prio || a.code.length - b.code.length || (a.code < b.code ? -1 : 1))
    .slice(0, limit);
}

/* ------------------------------------------------------------- UI */

const $ = (sel) => document.querySelector(sel);
let mode = "search"; // search | suggest
let searching = false;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c);
  }
  return node;
}

function fmtInt(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u2009"); }

function updateStatus() {
  const mib = (bytesLoaded / 1048576).toFixed(1);
  let shards = 0;
  for (const s of ALL_SOURCES) shards += s.shards.size;
  $("#status").textContent = `шардов загружено: ${shards} (~${mib} МБ)`;
}

function sourceBadge(name) {
  const s = byName[name];
  return el("span", { class: `badge badge-${name}` }, s ? s.label.split(" ")[0] : name);
}

function activeSources() {
  return ALL_SOURCES.filter((s) => $(`#src-${s.name}`).checked);
}

function codeChip(code, extraClass) {
  return el("button", { class: `chip ${extraClass || ""}`, "data-code": code, onclick: () => openDetail(code) }, code);
}

async function init() {
  const box = $("#sources");
  for (const s of ALL_SOURCES) {
    box.append(el("label", { class: "srclist", title: s.hint },
      el("input", { type: "checkbox", id: `src-${s.name}`, checked: "checked", onchange: () => { renderRootSections(); rerun(); } }),
      el("span", { class: "srccolor srccolor-" + s.name }),
      el("span", {}, s.label, el("small", { class: "srccount", id: `cnt-${s.name}` }, "")),
    ));
  }
  $("#q").addEventListener("input", () => scheduleSearch());
  $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearchNow(); });
  for (const b of document.querySelectorAll("#mode button")) {
    b.addEventListener("click", () => {
      mode = b.dataset.mode;
      for (const x of document.querySelectorAll("#mode button")) x.classList.toggle("active", x === b);
      rerun();
    });
  }
  $("#detail-close").addEventListener("click", closeDetail);
  $("#detail").addEventListener("click", (e) => { if (e.target === $("#detail")) closeDetail(); });

  // мета всех источников: счётчики + корневые разделы
  await Promise.all(ALL_SOURCES.map((s) => s.ensureMeta()));
  for (const s of ALL_SOURCES) {
    const c = $(`#cnt-${s.name}`);
    if (s.failed) { c.textContent = " · недоступен"; c.classList.add("err"); }
    else c.textContent = ` · ${fmtInt(s.recordsCount)}`;
  }
  renderRootSections();
  updateStatus();

  const q = new URLSearchParams(location.search).get("q");
  if (q) { $("#q").value = q; runSearchNow(); }
}

function renderRootSections() {
  const nav = $("#rootSections");
  nav.innerHTML = "";
  // корневые разделы — от приоритетного активного источника (union трёх шумный)
  const src = activeSources()[0] || ALL_SOURCES[0];
  const codes = src.topChildren[""] || [];
  if (!codes.length) { nav.hidden = true; return; }
  nav.hidden = false;
  nav.append(el("span", { class: "rootlabel" }, "Разделы:"));
  for (const c of codes) nav.append(codeChip(c, "root"));
}

let searchTimer = null;
function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearchNow, 400);
}
function rerun() { if ($("#q").value.trim().length >= 2) runSearchNow(); }

function setLoading(on, what) {
  $("#loading").hidden = !on;
  $("#loading .what").textContent = what || "";
}

async function runSearchNow() {
  if (searching) return;
  const query = $("#q").value.trim();
  const url = new URL(location);
  if (query) url.searchParams.set("q", query); else url.searchParams.delete("q");
  history.replaceState(null, "", url);
  if (query.length < 2) { renderIdle(); return; }
  const srcs = activeSources();
  const results = $("#results");
  if (!srcs.length) {
    results.innerHTML = "";
    results.append(el("p", { class: "muted" }, "Выберите хотя бы один источник."));
    return;
  }
  searching = true;
  setLoading(true, "грузим индексы…");
  let per = [];
  try {
    per = await Promise.all(srcs.map((s) => searchInSource(s, query, 15, mode === "suggest" ? 0.15 : 0, mode === "search")));
  } finally {
    searching = false;
    setLoading(false);
    updateStatus();
  }
  renderResults(query, per);
}

function renderIdle() {
  $("#results").innerHTML = "";
  $("#results").append(el("p", { class: "muted" }, "Введите тему (например, «нейронные сети») или код (например, «621.37»)."));
}

function renderResults(query, per) {
  const results = $("#results");
  results.innerHTML = "";
  results.append(el("h2", {}, mode === "suggest" ? "Коды по теме" : "Результаты"));

  const merged = mergeTop(per.map((p) => p.textMatches), 12);
  if (merged.length) {
    const sec = el("section", { class: "group" }, el("h3", {}, "Лучшие совпадения"));
    for (const it of merged) sec.append(resultRow(it, mode === "suggest"));
    results.append(sec);
  }

  for (const p of per) {
    if (p.error) {
      results.append(el("section", { class: "group" },
        el("h3", {}, sourceBadge(p.src.name), el("span", { class: "muted" }, " — " + p.error))));
      continue;
    }
    const sec = el("section", { class: "group" }, el("h3", {}, sourceBadge(p.src.name)));
    let any = false;
    if (p.codeMatches.length) {
      any = true;
      const row = el("div", { class: "codelist" }, el("span", { class: "muted" }, "по коду: "));
      for (const c of p.codeMatches) row.append(codeChip(c));
      sec.append(row);
    }
    if (p.textMatches.length) {
      any = true;
      for (const it of p.textMatches) sec.append(resultRow(it, mode === "suggest"));
    }
    if (!any) sec.append(el("p", { class: "muted" }, "ничего не найдено"));
    results.append(sec);
  }
  if (!merged.length && per.every((p) => !p.codeMatches.length && !p.textMatches.length && !p.error)) {
    results.append(el("p", { class: "muted" },
      "Ничего не найдено. Попробуйте более общие термины или другую форму слова (в УДК часто единственное число: «нейронная сеть»)."));
  }
}

function resultRow(it, withScore) {
  const row = el("div", { class: "result" + (it.cancelled ? " cancelled" : "") },
    codeChip(it.code),
    el("span", { class: "title", onclick: () => openDetail(it.code) }, it.title || "(без названия)"),
    it.cancelled ? el("span", { class: "badge badge-cancelled" }, "исключён") : null,
    it.hasChildren ? el("span", { class: "muted", title: "есть дочерние коды" }, "▾") : null,
  );
  const meta = el("span", { class: "resultmeta" }, sourceBadge(it.source));
  if (withScore) meta.append(el("span", { class: "muted" }, ` ${it.score}`));
  row.append(meta);
  return row;
}

/* ------------------------------------------------------------- карточка */

function bigramSim(a, b) {
  const grams = (s) => {
    const out = new Set();
    for (let i = 0; i < s.length - 1; i++) out.add(s.slice(i, i + 2));
    return out;
  };
  const ga = grams(a.toLowerCase()), gb = grams(b.toLowerCase());
  if (!ga.size || !gb.size) return 0;
  let inter = 0;
  for (const g of ga) if (gb.has(g)) inter++;
  return (2 * inter) / (ga.size + gb.size);
}

const COMBINE_SPLIT_RE = /[+:\[\]]/g;

async function openDetail(code) {
  const dlg = $("#detail");
  const body = $("#detail-body");
  $("#detail-title").textContent = code;
  body.innerHTML = "";
  dlg.hidden = false;
  body.append(el("p", { class: "muted" }, "загрузка…"));
  const srcs = activeSources().length ? activeSources() : ALL_SOURCES;
  const entries = [];
  for (const s of srcs) {
    const e = await buildEntry(s, code);
    if (e) entries.push(e);
  }
  body.innerHTML = "";

  if (!entries.length) {
    const parts = [...new Set((code.match(COMBINE_SPLIT_RE) ? code.split(COMBINE_SPLIT_RE) : [code]).map((p) => p.trim()).filter(Boolean))];
    if (parts.length > 1) {
      body.append(el("p", { class: "note" },
        "Это составной код УДК («:» — отношение тем, «+» — объединение). Составляющие:"));
      for (const part of parts) {
        const row = el("div", { class: "codelist" }, codeChip(part));
        for (const s of srcs) {
          const r = await firstRec(s, part);
          if (r) row.append(el("span", { class: "partmatch" }, sourceBadge(s.name), " " + (r.title || "")));
        }
        body.append(row);
      }
    } else {
      body.append(el("p", { class: "muted" }, `код ${code} не найден — проверьте написание поиском`));
    }
    return;
  }

  // предупреждение о расхождении источников (как в udc_get)
  const warnings = [];
  for (let i = 0; i < entries.length; i++) {
    for (let j = i + 1; j < entries.length; j++) {
      const a = entries[i], b = entries[j];
      if (a.title && b.title && bigramSim(a.title, b.title) < 0.5) {
        warnings.push(`Источники «${byName[a.source].label}» и «${byName[b.source].label}» по-разному раскрывают этот код. Основной — triumph (современная редакция).`);
      }
    }
  }
  for (const w of [...new Set(warnings)]) body.append(el("p", { class: "warn" }, w));

  for (const e of entries) {
    const card = el("section", { class: "card" },
      el("h4", {}, sourceBadge(e.source), " ", e.title || "(без названия)",
        e.cancelled ? el("span", { class: "badge badge-cancelled" }, "исключён") : null),
    );
    if (e.ancestors.length) {
      const bc = el("div", { class: "crumbs" });
      for (const a of e.ancestors) {
        bc.append(codeChip(a.code, "crumb"), el("span", { class: "muted" }, " › "));
      }
      bc.append(el("span", { class: "crumbhere" }, e.code));
      card.append(bc);
    }
    if (e.note) card.append(el("p", { class: "note" }, e.note));
    if (e.refs) card.append(el("p", { class: "refs" }, "см. также: ", e.refs));
    if (e.url) card.append(el("p", {}, el("a", { href: e.url, target: "_blank", rel: "noopener" }, e.url)));
    const kids = e.children || [];
    if (kids.length) {
      const wrap = el("div", { class: "children" }, el("span", { class: "muted" }, `дочерние коды (${fmtInt(kids.length)}): `));
      const list = el("div", { class: "codelist" });
      const show = Math.min(kids.length, 100);
      for (const c of kids.slice(0, show)) list.append(codeChip(c));
      wrap.append(list);
      if (kids.length > show) {
        const more = el("button", { class: "linklike" }, `показать ещё ${fmtInt(kids.length - show)}`);
        more.addEventListener("click", () => {
          for (const c of kids.slice(show)) list.append(codeChip(c));
          more.remove();
        });
        wrap.append(more);
      }
      card.append(wrap);
    }
    body.append(card);
  }
}

async function buildEntry(src, code) {
  const r = await firstRec(src, code);
  if (!r) return null;
  const e = { code, title: r.title || "", source: src.name };
  const note = r.note || r.notes;
  if (note) e.note = note;
  if (r.refs) e.refs = r.refs;
  if (r.cancelled) e.cancelled = true;
  if (src.name === "teacode" && r.linked) e.url = teacodeUrl(code);
  e.ancestors = await ancestorsOf(src, code);
  e.children = await childrenOf(src, code);
  return e;
}

function closeDetail() { $("#detail").hidden = true; }

init();
