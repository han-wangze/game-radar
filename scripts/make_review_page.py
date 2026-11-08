#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 tag_work/review.csv 变成一个自包含的本地抽检页 tag_work/review.html。

为什么要做这个
--------------
95 条抽检如果靠手填 CSV，要在一行 300 字符的描述和 7 列之间来回找位置，
既慢又容易填错列（`import-review` 只校验 verdict 与 human_value，列错位它查不出来）。
这个页面把「一条标签 + 它的证据 + 全文描述」放在同一屏，点三下就完成一条，
并把结果存进浏览器 localStorage，随时关掉再回来。

页面不联网、不引 CDN，所有数据内联。
导出的 CSV 与 `tag_apps.py import-review` 要求的列一一对应。

用法
----
  python scripts/make_review_page.py                 # 默认读 tag_work/review.csv
  python scripts/make_review_page.py --in x.csv --out y.html
然后双击打开 tag_work/review.html。
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "radar.db"

# 每个标签键要问的问题——不同键的「对」不是同一个意思
QUESTIONS = {
    "play_desc": "这句话准确描述了核心玩法吗？",
    "like_of": "这个玩法类型概括得准确吗？",
}

COLS = ["batch", "app_id", "name", "tag_key", "value", "evidence_strength",
        "need_review", "evidence", "descr_excerpt", "verdict", "human_value", "note"]


def load_domains() -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = {}
    for r in conn.execute(
            "SELECT tag_key, name_cn, definition, value_domain, max_len FROM dim_tag_def"):
        vals = json.loads(r["value_domain"]) if r["value_domain"] else None
        out[r["tag_key"]] = {
            "name": r["name_cn"],
            "definition": (r["definition"] or "")[:260],
            "values": vals,
            "max_len": r["max_len"],
        }
    conn.close()
    return out


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # CSV 里的 descr_excerpt 只有前 300 字，判 art / subgenre 时经常不够。
    # 直接从库里取全文补上，页面上「看完整描述」才是真的完整。
    #
    # 描述是**分店面**口径：dim_app.descr 自 2026-11-05 起冻结（存的是迁移时最后
    # 写入国那份，实际是韩文），拿它当判定依据会让 US/JP 的标签对着韩文介绍判。
    # 这里按 fact_tag.descr_country 取该标签当初真正依据的那份，
    # 取不到再按 us→cn→jp→kr 兜底。
    by_pair: dict[tuple[str, str], str] = {}
    used: dict[tuple[str, str], str] = {}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        for aid, cc, descr in conn.execute(
                "SELECT app_id, country, COALESCE(descr,'') FROM dim_app_country"):
            if descr.strip():
                by_pair[(aid, cc)] = descr
        used = {(aid, key): cc for aid, key, cc in conn.execute(
            "SELECT app_id, tag_key, descr_country FROM fact_tag")}
        conn.close()
    except sqlite3.Error:
        pass
    kept = []
    for r in rows:
        item = {c: (r.get(c) or "") for c in COLS}
        cc = used.get((item["app_id"], item["tag_key"]))
        text = by_pair.get((item["app_id"], cc)) if cc else None
        if not text:
            for cand in ("us", "cn", "jp", "kr"):
                text = by_pair.get((item["app_id"], cand))
                if text:
                    break
        item["descr_full"] = text or item["descr_excerpt"]
        item["descr_src"] = cc or "兜底"
        kept.append(item)
    return kept


HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>标签抽检</title>
<style>
:root{
  --bg:#f8f9fa; --panel:#fff; --line:#e3e6ea; --text:#1f2328; --muted:#6b7280;
  --accent:#2563eb; --ok:#16a34a; --bad:#dc2626; --warn:#d97706; --soft:#f1f3f5;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:22px 18px 80px}
h1{font-size:19px;margin:0 0 6px}
.sub{color:var(--muted);font-size:13px;margin-bottom:14px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.chip{border:1px solid var(--line);background:var(--panel);border-radius:999px;
 padding:5px 12px;font-size:13px;cursor:pointer;color:var(--text)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.spacer{flex:1}
.prog{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.track{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-bottom:16px}
.fill{height:100%;background:var(--accent);width:0;transition:width .18s}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px 22px}
.kicker{font-size:12.5px;color:var(--muted);letter-spacing:.02em;margin-bottom:4px}
.pname{font-size:17px;font-weight:650;margin-bottom:14px;word-break:break-all}
.q{font-size:13.5px;color:var(--muted);margin-bottom:8px}
.val{display:inline-block;font:15px/1.4 ui-monospace,Consolas,monospace;
 background:var(--soft);border:1px solid var(--line);border-radius:6px;
 padding:6px 10px;margin-bottom:14px;word-break:break-all}
.tags{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.tag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.tag.direct{color:var(--ok);border-color:#bbe5c8;background:#f2fbf5}
.tag.inferred{color:var(--warn);border-color:#f3ddb5;background:#fef9ef}
.tag.none{color:var(--bad);border-color:#f5c6c6;background:#fdf2f2}
.tag.rev{color:var(--bad);border-color:#f5c6c6;background:#fdf2f2}
.ev{background:var(--soft);border-left:3px solid var(--line);padding:9px 12px;
 font-size:13.5px;color:#374151;border-radius:0 6px 6px 0;margin-bottom:16px;word-break:break-word}
details{margin-bottom:16px}
summary{cursor:pointer;font-size:13px;color:var(--accent);outline:none}
.descr{margin-top:8px;background:var(--soft);border:1px solid var(--line);border-radius:6px;
 padding:11px 13px;font-size:13px;color:#374151;max-height:210px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.btns{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}
button{font:inherit;border-radius:7px;padding:9px 16px;cursor:pointer;border:1px solid var(--line);
 background:var(--panel);color:var(--text)}
button:hover{border-color:#c8cdd3}
button.ok{background:var(--ok);border-color:var(--ok);color:#fff}
button.bad{background:var(--bad);border-color:var(--bad);color:#fff}
button.uns{background:var(--warn);border-color:var(--warn);color:#fff}
button.ghost{color:var(--muted);font-size:13px;padding:6px 10px}
input[type=text],textarea,select{width:100%;font:inherit;font-size:13.5px;padding:8px 10px;
 border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--text)}
textarea{min-height:54px;resize:vertical}
.fix{margin-bottom:12px}
.fix label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:5px}
.hint{font-size:12.5px;color:var(--muted);margin-top:10px}
kbd{background:var(--soft);border:1px solid var(--line);border-bottom-width:2px;
 border-radius:4px;padding:0 5px;font:12px ui-monospace,Consolas,monospace}
.done{text-align:center;padding:34px 10px}
.done h2{font-size:17px;margin:0 0 8px}
.done p{color:var(--muted);font-size:13.5px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:18px 0}
.stat{background:var(--soft);border-radius:8px;padding:12px;text-align:center}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
.warnbar{background:#fef9ef;border:1px solid #f3ddb5;color:#8a5b00;border-radius:8px;
 padding:9px 12px;font-size:13px;margin-bottom:14px}
</style></head><body><div class="wrap">
<h1>标签抽检</h1>
<div class="sub">逐条判断机器给的标签对不对。判断结果存在本机浏览器里，随时关掉再回来；
完成后点「导出 CSV」，把文件交给脚本导入。</div>

<div class="bar" id="filters"></div>
<div class="prog" id="prog"></div>
<div class="track"><div class="fill" id="fill"></div></div>

<div id="stage"></div>

<div class="hint">快捷键：<kbd>1</kbd> 对　<kbd>2</kbd> 错　<kbd>3</kbd> 存疑　<kbd>←</kbd> 上一条　
<kbd>Enter</kbd> 保存并下一条</div>
</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const P = JSON.parse(document.getElementById('payload').textContent);
const ROWS = P.rows, DOM = P.domains, Q = P.questions, COLS = P.cols;
const KEY = 'gr_review_v1';
// file:// 下个别环境会禁用 localStorage，这里降级成内存存储，至少不会让整页报错。
// 代价是关掉浏览器就丢进度，所以导出 CSV 要及时。
let memOnly = false;
function loadStore(){
  try { return JSON.parse(localStorage.getItem(KEY) || '{}'); }
  catch (e) { memOnly = true; return {}; }
}
let store = loadStore();
let filter = 'all';
function save(){
  if (memOnly) return;
  try { localStorage.setItem(KEY, JSON.stringify(store)); }
  catch (e) { memOnly = true; }
}
function keyOf(r){ return r.app_id + '|' + r.tag_key; }
function verdictOf(r){ return (store[keyOf(r)] || {}).verdict || ''; }

function counts(){
  const total = ROWS.length;
  let done = 0;
  for (const r of ROWS) if (verdictOf(r)) done++;
  return {total, done};
}

function renderFilters(){
  const box = document.getElementById('filters');
  const keys = ['all', ...Object.keys(Q)];
  const c = counts();
  box.innerHTML = '';
  for (const k of keys){
    const n = k === 'all' ? ROWS.length : ROWS.filter(r => r.tag_key === k).length;
    const undone = k === 'all'
      ? ROWS.filter(r => !verdictOf(r)).length
      : ROWS.filter(r => r.tag_key === k && !verdictOf(r)).length;
    const b = document.createElement('button');
    b.className = 'chip' + (filter === k ? ' on' : '');
    b.textContent = (k === 'all' ? '全部' : DOM[k].name) + ' ' + (n - undone) + '/' + n;
    b.onclick = () => { filter = k; renderFilters(); render(); };
    box.appendChild(b);
  }
  const sp = document.createElement('div'); sp.className = 'spacer'; box.appendChild(sp);
  const exp = document.createElement('button');
  exp.className = 'chip'; exp.textContent = '导出 CSV（' + c.done + ' 条已判）';
  exp.onclick = exportCsv;
  box.appendChild(exp);
}

function pool(){
  return filter === 'all' ? ROWS : ROWS.filter(r => r.tag_key === filter);
}

function nextIndex(from){
  const p = pool();
  for (let i = from; i < p.length; i++) if (!verdictOf(p[i])) return i;
  return -1;
}

let cursor = 0;

function render(){
  const p = pool();
  const c = counts();
  document.getElementById('prog').textContent = '已完成 ' + c.done + ' / ' + c.total;
  document.getElementById('fill').style.width = (100 * c.done / c.total) + '%';

  if (cursor >= p.length) cursor = Math.max(0, nextIndex(0));
  const stage = document.getElementById('stage');

  if (!p.length){ stage.innerHTML = '<div class="done"><h2>这个分组没有条目</h2></div>'; return; }

  const i = nextIndex(cursor);
  if (i < 0){
    stage.innerHTML = `<div class="done"><h2>这一组都判完了</h2>
      <p>共 ${p.length} 条。可以切换分组，或导出 CSV 交给脚本。</p></div>`;
    return;
  }
  cursor = i;
  const r = p[i];
  // 抽检表可能是旧批次（subgenre / theme / art 等已停用的键），dim_tag_def 里没有
  // 对应定义。这里兜一个最小对象，页面照样能逐条判，不会因为一个 undefined 整页白屏。
  const d = DOM[r.tag_key] || {name: r.tag_key, definition: '', values: null, max_len: 60};
  const question = Q[r.tag_key] || '这条标签准确吗？';
  const st = store[keyOf(r)] || {};
  const closed = Array.isArray(d.values);

  let fixHtml = '';
  if (closed){
    fixHtml = `<div class="fix"><label>正确的值（判「错」时必填）</label>
      <select id="fixval"><option value="">— 选择 —</option>
      ${d.values.map(v => `<option ${st.human_value === v ? 'selected' : ''}>${v}</option>`).join('')}
      </select></div>`;
  } else {
    fixHtml = `<div class="fix"><label>改成什么（判「错」时必填，可直接改这句）</label>
      <textarea id="fixval" maxlength="${d.max_len || 60}">${esc(st.human_value || '')}</textarea></div>`;
  }

  stage.innerHTML = `<div class="card">
    <div class="kicker">第 ${i + 1} / ${p.length} 条　·　${esc(d.name)}</div>
    <div class="pname">${esc(r.name)}</div>
    <div class="q">${esc(question)}</div>
    <div class="val">${esc(r.value)}</div>
    <div class="tags">
      <span class="tag ${esc(r.evidence_strength)}">证据强度 ${esc(r.evidence_strength)}</span>
      ${r.need_review === '1' ? '<span class="tag rev">原标记：待复核</span>' : ''}
      <span class="tag">批次 ${esc(r.batch)}</span>
      <span class="tag">描述来源 ${esc(r.descr_src || '—')}</span>
    </div>
    <div class="ev"><b>模型给的证据</b><br>${esc(r.evidence || '（空）')}</div>
    <details><summary>看完整描述（判定依据）</summary>
      <div class="descr">${esc(r.descr_full || '（无描述）')}</div></details>
    <details><summary>看这个标签的判定标准</summary>
      <div class="descr">${esc(d.definition || '')}</div></details>
    ${fixHtml}
    <div class="fix"><label>备注（可选，判错时写清为什么）</label>
      <input type="text" id="note" value="${esc(st.note || '')}" placeholder="例如：原文只说消除，没提合成"></div>
    <div class="btns">
      <button class="ok" onclick="judge('correct')">✓ 对（1）</button>
      <button class="bad" onclick="judge('wrong')">✗ 错（2）</button>
      <button class="uns" onclick="judge('unsure')">? 存疑（3）</button>
      <button class="ghost" onclick="skip()">跳过 →</button>
    </div>
    <div class="hint">判「错」要在上面填正确值；「存疑」表示描述不足以判断，不必填。</div>
  </div>`;
}

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

function judge(v){
  const p = pool();
  const r = p[cursor];
  const fixEl = document.getElementById('fixval');
  const noteEl = document.getElementById('note');
  const hv = fixEl ? fixEl.value.trim() : '';
  if (v === 'wrong' && !hv){ alert('判「错」时必须填正确的值。'); return; }
  store[keyOf(r)] = {verdict: v, human_value: hv, note: noteEl ? noteEl.value.trim() : ''};
  save(); renderFilters(); cursor++; render();
}

function skip(){ cursor++; render(); }

function exportCsv(){
  const rows = [COLS];
  for (const r of ROWS){
    const s = store[keyOf(r)] || {};
    rows.push(COLS.map(c => {
      if (c === 'verdict') return s.verdict || '';
      if (c === 'human_value') return s.human_value || '';
      if (c === 'note') return s.note || '';
      return r[c] == null ? '' : r[c];
    }));
  }
  const csv = rows.map(r => r.map(x => {
    const t = String(x);
    return /[",\\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  }).join(',')).join('\\r\\n');
  const blob = new Blob(['\\ufeff' + csv], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  a.download = 'review_filled_' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate())
             + '_' + pad(d.getHours()) + pad(d.getMinutes()) + '.csv';
  a.click();
}
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
    if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); judge('correct'); }
    return;
  }
  if (e.key === '1') judge('correct');
  if (e.key === '2') judge('wrong');
  if (e.key === '3') judge('unsure');
  if (e.key === 'ArrowLeft'){ cursor = Math.max(0, cursor - 1); render(); }
});
renderFilters(); render();
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(ROOT / "tag_work" / "review.csv"))
    ap.add_argument("--out", dest="dst", default=str(ROOT / "tag_work" / "review.html"))
    args = ap.parse_args()

    rows = load_rows(Path(args.src))
    if not rows:
        print("抽检表是空的，先跑 tag_apps.py sample-review")
        return 1
    payload = {
        "rows": rows,
        "domains": load_domains(),
        "questions": QUESTIONS,
        "cols": COLS,
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = HTML.replace("__DATA__", data)
    out = Path(args.dst)
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}　（{len(rows)} 条，{out.stat().st_size // 1024} KB）")
    print("双击打开即可逐条判断；完成后点「导出 CSV」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
