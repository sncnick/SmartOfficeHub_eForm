'use strict';

const $ = (selector) => document.querySelector(selector);

const state = {
  data: null,
  source: null, // { file } or { upload, name }
  selectedId: null,
  filtered: [],
  collapsed: 0,
};

// Set while rendering the inside of a collapsed OUTDATED card: everything there is already
// outdated, so it is shown inline instead of being collapsed a second time.
const render = { inOutdated: false, company: null };

const SECTIONS = [
  ['directors', '董事 Directors'],
  ['shareholders', '股東 Shareholders（非董事）'],
  ['company_secretaries', '公司秘書 Com Sec'],
  ['other_parties', '其他標籤（原文顯示）'],
];
const STATUS_TEXT = { active: '現行', inactive: 'Inactive', resigned: 'Resigned', deregistered: 'Deregistered' };
const SEVERITY_TEXT = { error: '錯誤', warning: '警告', info: '提示' };
const REASON_TEXT = {
  blue: '藍色底', end_date: '有結束／辭任日期', block: '所屬資料已過期', former: '舊名稱', superseded: '已有較新的 RO',
  manual: '手動標示',
};
const FIELD_LABELS = {
  company_no: '公司編號',
  status: '狀態',
  'general.name_en': '英文名稱',
  'general.name_zh': '中文名稱',
  'general.incorporation_date': '成立日期',
  'general.former_names': '舊名稱',
  registered_offices: 'RO',
  'ro.from': 'RO 開始日期',
  'ro.to': 'RO 結束日期',
  'ro.status': 'RO 狀態',
  share_capital: '股本 Shares',
  'capital.date': '股本日期',
  'capital.status': '股本狀態',
  'capital.label': '股本標籤',
  shareholding_summary: '持股摘要',
  directors: '董事',
  company_secretaries: '公司秘書',
  'party.label': '人物標籤',
  undefined: '未定義',
  'party.appoint_date': '委任日期',
  'party.resign_date': '辭任日期',
  'party.date_of_birth': '出生日期',
  item: '地址／證件等資料行',
  'holding.label': '持股標籤',
  'holding.date': '持股日期',
  'holding.end_date': '持股結束日期',
  'holding.transfer': '轉讓',
  'holding.dt': 'DT',
  'holding.percentage_excel': '持股 %',
  'holding.consideration': '持股代價',
  'holding.total_no_of_shares': '累計股數',
  'branch.label': '分行標籤（A 欄）',
  'branch.from': '分行開始日期',
  'branch.to': '分行結束日期',
};
const searchCache = new WeakMap();

const COLOR_LABELS_KEY = 'cdd-viewer.color-labels';
const COLOR_GROUP_TEXT = { orange: '正常現行', red: 'Inactive', other: '非橙色', none: '沒有底色' };
const STATUS_OPTIONS = [
  ['active', '現行 Active'],
  ['resigned', 'Inactive · Resigned'],
  ['deregistered', 'Inactive · Deregistered'],
  ['inactive', 'Inactive（只有紅色，沒有註記）'],
];
const filters = { colors: new Set(), statuses: new Set(), onlyIssues: false };

// ---------------------------------------------------------------- formatting

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function isEmpty(value) {
  return value === null || value === undefined || value === '';
}

function fmtDate(value) {
  if (isEmpty(value)) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : String(value);
}

function fmtNum(value) {
  if (isEmpty(value)) return '';
  return typeof value === 'number' ? value.toLocaleString('en-US', { maximumFractionDigits: 4 }) : String(value);
}

function fmtPct(value) {
  if (typeof value !== 'number') return '';
  if (value < 0 || value > 1.0001) return `${fmtNum(value)}（?）`;
  return `${Number((value * 100).toFixed(2))}%`;
}

function dateRange(from, to) {
  if (from && to) return `${fmtDate(from)} – ${fmtDate(to)}`;
  if (from) return `${fmtDate(from)} 起`;
  if (to) return `至 ${fmtDate(to)}`;
  return '';
}

function reasonsText(reasons) {
  return (reasons || []).map((r) => REASON_TEXT[r] || r).join('、');
}

function statusText(c) {
  if (c.active) return '現行 Active';
  let text = c.status === 'inactive' ? 'Inactive' : `Inactive · ${STATUS_TEXT[c.status]}`;
  if (c.status_date) text += `（${fmtDate(c.status_date)}）`;
  if (c.red_label) text += '，公司編號為紅色';
  return text;
}

function statusPill(c) {
  return c.active ? '' : `<span class="pill inactive" title="${esc(statusText(c))}">Inactive</span>`;
}

function warnCount(company) {
  return company.counts.issues.error + company.counts.issues.warning;
}

function colorKey(c) {
  return c.color_group === 'other' ? `hex:${c.label_color}` : (c.color_group || 'none');
}

function customLabels() {
  try {
    return JSON.parse(localStorage.getItem(COLOR_LABELS_KEY) || '{}');
  } catch {
    return {};
  }
}

function orangeNote() {
  return customLabels()['group:orange'] || COLOR_GROUP_TEXT.orange;
}

function colorLabel(hex, fallback) {
  return customLabels()[hex] || fallback || `#${hex}`;
}

function colorField(c) {
  const ok = typeof c.label_color === 'string' && /^[0-9A-F]{6}$/i.test(c.label_color);
  const chip = `<span class="color-cell${ok ? '' : ' none'}" style="${ok ? `background:#${c.label_color}` : ''}"></span>`;
  return `<div class="field span-3"><label>公司編號顏色</label>`
    + `<div class="value color-value">${chip}<span>${esc(colorText(c))}</span></div></div>`;
}

function colorText(c) {
  if (!c.label_color) return '沒有底色（非橙色）';
  const group = c.color_group === 'orange' ? orangeNote() : COLOR_GROUP_TEXT[c.color_group];
  return `${colorLabel(c.label_color, c.color_name)}　#${c.label_color}（${group}）`;
}

function searchText(company) {
  if (!searchCache.has(company)) {
    const names = SECTIONS.flatMap(([key]) => company[key].flatMap((p) => [p.name_en, p.name_zh]));
    const g = company.general;
    searchCache.set(company, [company.company_no, g.name_en, g.name_zh, g.br_no, g.ci_no, ...names]
      .filter(Boolean).join(' ').toLowerCase());
  }
  return searchCache.get(company);
}

// ---------------------------------------------------------------- building blocks

function expandAll() {
  return $('#expandOutdated').checked;
}

function splitOutdated(items) {
  const all = items || [];
  if (render.inOutdated) return { current: all, outdated: [] };
  const outdated = all.filter((it) => it.outdated);
  if (!expandAll()) state.collapsed += outdated.length;
  return { current: all.filter((it) => !it.outdated), outdated };
}

function swatch(color, cls = '') {
  const ok = typeof color === 'string' && /^[0-9A-F]{6}$/i.test(color);
  const title = ok ? `A 欄底色 #${color}` : 'A 欄沒有底色';
  return `<span class="swatch ${cls}" style="background:${ok ? `#${color}` : 'transparent'}" title="${title}"></span>`;
}

function outdatedBadge(item) {
  const own = (item.outdated_reasons || []).filter((r) => r !== 'block');
  const text = reasonsText(own.length ? own : item.outdated_reasons);
  return `<span class="badge old" title="已過期，毋須理會">OUTDATED${text ? ` · ${esc(text)}` : ''}</span>`;
}

function stateBadge(item) {
  if (item.outdated) return render.inOutdated ? '' : outdatedBadge(item);
  return '<span class="badge ok">現行</span>';
}

// ----- problems shown beside the field they belong to

function issuesAt(fieldKey, row) {
  const rows = row instanceof Set ? row : (row == null ? null : new Set([row]));
  const c = render.company;
  if (!c) return [];
  return c.issues.filter((i) => i.field === fieldKey && (rows == null || rows.has(i.row)));
}

function issueMarks(issues) {
  if (!issues.length) return { cls: '', html: '' };
  const serious = issues.some((i) => i.severity !== 'info');
  return {
    cls: serious ? ' has-issue' : ' has-info',
    html: `<div class="field-issues">${issues.map((i) => `<div class="fi ${i.severity}">`
      + `<span class="sev ${i.severity}">${SEVERITY_TEXT[i.severity]}</span> ${esc(i.message)}</div>`).join('')}</div>`,
  };
}

function rowAttr(row) {
  if (row == null || row === '') return '';
  return ` data-row="${esc(row)}"`;
}

function locListAttr(pairs) {
  const keys = [];
  for (const [field, row] of pairs) {
    if (!field) continue;
    const key = `${field}:${row ?? ''}`;
    if (!keys.includes(key)) keys.push(key);
  }
  return keys.length ? ` data-loc="${esc(keys.join('|'))}"` : '';
}

function locsFrom(issues, extra = []) {
  const pairs = extra.slice();
  for (const issue of issues || []) {
    if (issue.field) pairs.push([issue.field, issue.row ?? '']);
  }
  return locListAttr(pairs);
}

function sectionIssues(fieldKey) {
  const issues = issuesAt(fieldKey);
  return issues.length
    ? `<div class="section-issues"${locsFrom(issues)}>${issueMarks(issues).html}</div>` : '';
}

function boxed(value, issues = []) {
  const mark = issueMarks(issues);
  const empty = isEmpty(value);
  return `<div class="value${empty ? ' empty' : ''}${mark.cls}"${locsFrom(issues)}>${empty ? '' : value}</div>${mark.html}`;
}

function cell(content, issues = [], cls = '') {
  if (!issues.length) return `<td class="${cls}">${content}</td>`;
  return `<td class="${cls}">${boxed(content, issues)}</td>`;
}

function issueRow(issues, colspan) {
  return issues.length ? `<tr class="issue-row"><td colspan="${colspan}">${issueMarks(issues).html}</td></tr>` : '';
}

function field(label, value, cls = '', issues = [], path = '') {
  const empty = isEmpty(value);
  const mark = issueMarks(issues);
  const control = path
    ? `<input class="value" data-path="${esc(path)}" value="${esc(empty ? '' : value)}" autocomplete="off">`
    : `<div class="value${empty ? ' empty' : ''}">${empty ? '' : esc(value)}</div>`;
  return `<div class="field ${cls}${mark.cls}"${locsFrom(issues)}><label>${esc(label)}</label>`
    + `${control}${mark.html}</div>`;
}

function dateField(label, value, sourceRow, ownRow, issues = [], path = '') {
  const note = value && sourceRow && sourceRow !== ownRow
    ? `<span class="src" title="這個日期寫在 Excel 第 ${sourceRow} 行（SH 持股行）">取自第 ${sourceRow} 行</span>` : '';
  const mark = issueMarks(issues);
  const shown = fmtDate(value);
  const resign = path.endsWith('/resign_date') ? ' data-rerender="resign"' : '';
  const control = path
    ? `<input class="value" data-path="${esc(path)}"${resign} value="${esc(shown)}" autocomplete="off">`
    : `<div class="value${shown ? '' : ' empty'}">${esc(shown)}</div>`;
  return `<div class="field${mark.cls}"${locsFrom(issues)}><label>${esc(label)}</label>`
    + `${control}${note ? `<div class="muted small">${note}</div>` : ''}${mark.html}</div>`;
}

function itemHtml(it, path = '') {
  const range = dateRange(it.from, it.to);
  const where = it.row ? `Excel 第 ${it.row} 行${it.column ? `（${it.column} 欄）` : ''}` : '';
  const issues = it.row == null ? [] : issuesAt('item', it.row);
  const mark = issueMarks(issues);
  const text = path
    ? `<input class="text" data-path="${esc(path)}" value="${esc(it.value || '')}" autocomplete="off">`
    : `<span class="text">${esc(it.value)}</span>`;
  return `<div class="value item${it.outdated ? ' outdated' : ''}${mark.cls}" title="${esc(where)}"${rowAttr(it.row)}${locsFrom(issues)}>`
    + text
    + `${range ? `<span class="dates">${esc(range)}</span>` : ''}`
    + `${it.outdated && !render.inOutdated ? outdatedBadge(it) : ''}${mark.html}</div>`;
}

function idTypeOf(item) {
  if (item?.id_type === 'hkid' || item?.id_type === 'passport') return item.id_type;
  const text = String(item?.value || '').trim();
  if (/^[A-Z]{1,2}\d{6}\s*\(?[0-9A]\)?$/i.test(text)) return 'hkid';
  if (/passport/i.test(text)) return 'passport';
  return '';
}

function idTypeTicks(chosen, path) {
  const box = (type, label) => {
    const checked = chosen === type ? ' checked' : '';
    const bound = path
      ? ` data-path="${esc(path)}" data-saved="${esc(chosen)}" data-id-choice="${type}"`
      : ` name="id_type" value="${type}" data-id-choice="${type}"`;
    return `<label class="id-tick"><input type="checkbox"${bound}${checked}> ${esc(label)}</label>`;
  };
  return `<span class="id-ticks">${box('hkid', 'HKID')}${box('passport', 'Passport')}</span>`;
}

function idRow(item, pathBase, index) {
  const issues = item.row == null ? [] : issuesAt('item', item.row);
  const mark = issueMarks(issues);
  const valuePath = pathBase ? `${pathBase}/${index}/value` : '';
  const typePath = pathBase ? `${pathBase}/${index}/id_type` : '';
  const text = valuePath
    ? `<input class="text" data-path="${esc(valuePath)}" data-saved="${esc(item.value || '')}" value="${esc(item.value || '')}" autocomplete="off" placeholder="證件號碼">`
    : `<span class="text">${esc(item.value || '')}</span>`;
  const range = dateRange(item.from, item.to);
  return `<div class="id-row${item.outdated ? ' outdated' : ''}${mark.cls}"${rowAttr(item.row)}${locsFrom(issues)}>${text}`
    + `${idTypeTicks(idTypeOf(item), typePath)}`
    + `${range ? `<span class="dates">${esc(range)}</span>` : ''}`
    + `${item.outdated && !render.inOutdated ? outdatedBadge(item) : ''}${mark.html}</div>`;
}

function idField(items, cls = '', issues = [], pathBase = '') {
  const list = items || [];
  const entries = list.map((item, index) => ({ item, index }));
  const current = entries.filter(({ item }) => !item.outdated || render.inOutdated);
  const filled = current.filter(({ item }) => String(item.value || '').trim());
  const chosen = (filled.length ? filled : current).slice(-1)[0];
  const visible = [];
  const old = [];
  if (render.inOutdated) {
    entries.forEach(({ item, index }) => visible.push(idRow(item, pathBase, index)));
    if (!visible.length && pathBase) visible.push(idRow({ value: '' }, pathBase, list.length));
  } else {
    entries.forEach(({ item, index }) => {
      if (item.outdated) old.push(idRow(item, pathBase, index));
    });
    if (chosen && !chosen.item.outdated) visible.push(idRow(chosen.item, pathBase, chosen.index));
    else if (pathBase) visible.push(idRow({ value: '' }, pathBase, list.length));
  }
  let body = visible.join('');
  if (old.length) {
    const flagged = list.filter((item) => item.outdated && issuesAt('item', item.row).some((issue) => issue.severity !== 'info')).length;
    body += `<details class="old-items"${expandAll() ? ' open' : ''}>`
      + `<summary>OUTDATED（${old.length} 項，已過期毋須理會）`
      + `${flagged ? ` <span class="badge issue">⚠ ${flagged} 個問題</span>` : ''}</summary>`
      + `${old.join('')}</details>`;
  }
  const mark = issueMarks(issues);
  return `<div class="field ${cls}${mark.cls}"${locsFrom(issues)}><label>身份證／證件 ID</label>${body}${mark.html}</div>`;
}

function listField(label, items, cls = '', issues = [], pathBase = '', extraBlank = false) {
  const list = items || [];
  const { current, outdated } = splitOutdated(list);
  const itemPath = (it) => (pathBase ? `${pathBase}/${Math.max(list.indexOf(it), 0)}/value` : '');
  const rows = current.map((it) => itemHtml(it, itemPath(it)));
  if (!current.length && pathBase) rows.push(itemHtml({ value: '', outdated: false, outdated_reasons: [] }, `${pathBase}/0/value`));
  if (!current.length && !pathBase) rows.push('<div class="value empty"></div>');
  if (extraBlank && pathBase) {
    const tail = current[current.length - 1];
    const tailBlank = tail && !String(tail.value || '').trim();
    if (!tailBlank) {
      const index = current.length ? list.length : 1;
      rows.push(itemHtml({ value: '', outdated: false, outdated_reasons: [] }, `${pathBase}/${index}/value`));
    }
  }
  let body = rows.join('');
  if (outdated.length) {
    const flagged = outdated.filter((it) => issuesAt('item', it.row).some((i) => i.severity !== 'info')).length;
    body += `<details class="old-items"${expandAll() ? ' open' : ''}>`
      + `<summary>OUTDATED（${outdated.length} 項，已過期毋須理會）`
      + `${flagged ? ` <span class="badge issue">⚠ ${flagged} 個問題</span>` : ''}</summary>`
      + `${outdated.map((it) => itemHtml(it, itemPath(it))).join('')}</details>`;
  }
  const mark = issueMarks(issues);
  return `<div class="field ${cls}${mark.cls}"${locsFrom(issues)}><label>${esc(label)}</label>${body}${mark.html}</div>`;
}

function addressLines(item) {
  if (Array.isArray(item?.lines) && item.lines.length === 4) return item.lines;
  const parts = String(item?.address || item?.value || '')
    .replace(/\n/g, ',').replace(/，/g, ',').split(',').map((part) => part.trim()).filter(Boolean);
  if (parts.length > 4) parts.splice(0, parts.length - 3, parts.slice(0, -3).join(', '));
  while (parts.length < 4) parts.unshift('');
  return parts;
}

function addressGrid(item, fromIssues = [], toIssues = [], pathBase = '', showBadge = true) {
  const lines = item ? addressLines(item) : ['', '', '', ''];
  const names = ['地址 1', '地址 2', '地址 3', '地址 4'];
  const row = item?.row;
  const aliasRows = (item?.alias_rows || []).filter((alias) => alias != null && alias !== row);
  const aliasIssues = aliasRows.flatMap((alias) => ['item', 'address.from', 'address.to',
    'address.line.1', 'address.line.2', 'address.line.3', 'address.line.4']
    .flatMap((name) => issuesAt(name, alias)));
  const fromAll = [...fromIssues, ...(row == null ? [] : issuesAt('address.from', row))];
  const toAll = [...toIssues, ...(row == null ? [] : issuesAt('address.to', row))];
  const badge = showBadge && item?.outdated && !render.inOutdated ? `<div class="addr-flag">${outdatedBadge(item)}</div>` : '';
  const lineIssues = (index) => (row == null ? [] : issuesAt(`address.line.${index + 1}`, row));
  const linePath = (index) => (pathBase ? `${pathBase}/lines/${index}` : '');
  const boxes = names.map((name, index) => field(name, lines[index], '', lineIssues(index), linePath(index))).join('')
    + field('開始日期', fmtDate(item?.from), '', fromAll, pathBase ? `${pathBase}/from` : '')
    + field('結束日期', fmtDate(item?.to), '', toAll, pathBase ? `${pathBase}/to` : '');
  const itemIssues = row == null ? [] : issuesAt('item', row);
  const mark = issueMarks([...itemIssues, ...aliasIssues]);
  const where = item?.row ? `Excel 第 ${item.row} 行` : '';
  return `<div class="addr-grid${item?.outdated ? ' outdated' : ''}${mark.cls}" title="${esc(where)}"${rowAttr(row)}${locsFrom([...itemIssues, ...aliasIssues])}>`
    + `${badge}${boxes}${mark.html}</div>`;
}

function undefinedBlock(items, pathBase = '') {
  const list = items || [];
  const locFields = ['item', 'undefined', 'address.from', 'address.to',
    'address.line.1', 'address.line.2', 'address.line.3', 'address.line.4'];
  let storedIndex = 0;
  const body = list.map((item) => {
    const index = item?.parked ? null : storedIndex++;
    const textValue = String(item?.value || '').trim();
    if (!textValue) return '';
    const issues = item.row == null ? [] : locFields.flatMap((name) => issuesAt(name, item.row));
    const mark = issueMarks(issues);
    const path = pathBase && index != null ? `${pathBase}/${index}/value` : '';
    const text = path
      ? `<input class="text" data-path="${esc(path)}" value="${esc(item.value || '')}" autocomplete="off">`
      : `<span class="text">${esc(item.value || '')}</span>`;
    const range = dateRange(item.from, item.to);
    const locs = item.row == null ? '' : locListAttr(locFields.map((name) => [name, item.row]));
    const badge = item.outdated ? outdatedBadge(item) : '';
    return `<div class="value item${mark.cls}"${rowAttr(item.row)}${locs}>${text}`
      + `${range ? `<span class="dates">${esc(range)}</span>` : ''}${badge}${mark.html}</div>`;
  }).join('');
  if (!body) return '';
  return `<section class="undefined-block span-3"><h4>未定義</h4>${body}</section>`;
}

function chooseAddress(items) {
  const list = (items || []).filter((item) => item && typeof item === 'object');
  const filled = (item) => addressLines(item).every((line) => String(line || '').trim());
  const complete = list.filter(filled);
  const currentComplete = complete.filter((item) => !item.outdated);
  const current = list.filter((item) => !item.outdated);
  const pool = currentComplete.length ? currentComplete
    : complete.length ? complete
      : current.length ? current : list;
  return pool.length ? pool[pool.length - 1] : null;
}

function followsAddressFields(item) {
  if (!item) return true;
  const lines = Array.isArray(item.lines) && item.lines.length === 4
    ? item.lines.map((line) => String(line || '').trim())
    : addressLines(item).map((line) => String(line || '').trim());
  let seenEmpty = false;
  for (const line of lines) {
    if (!line) seenEmpty = true;
    else if (seenEmpty) return false;
  }
  return true;
}

function misplacedAddresses(items, already) {
  const rows = new Set((already || []).map((item) => item.row).filter((row) => row != null));
  return (items || []).filter((item) => item && !followsAddressFields(item)).filter((item) => {
    const lines = Array.isArray(item.lines) ? item.lines : [];
    return lines.some((line) => String(line || '').trim()) || String(item.value || item.address || '').trim();
  }).filter((item) => item.row == null || !rows.has(item.row)).map((item) => {
    const lines = (Array.isArray(item.lines) ? item.lines : []).map((line) => String(line || '').trim()).filter(Boolean);
    return {
      value: item.value || item.address || lines.join(', '),
      row: item.row,
      column: item.column,
      from: item.from,
      to: item.to,
      outdated: !!item.outdated,
      outdated_reasons: item.outdated_reasons || [],
      parked: true,
    };
  });
}
function addressField(items, pathBase = '', oneCurrent = false) {
  const source = items || [];
  const list = source.filter((item) => !item || followsAddressFields(item));
  const currentItems = list.filter((item) => item && !item.outdated);
  const chosen = oneCurrent ? chooseAddress(currentItems) : null;
  const current = [];
  const outdated = [];
  source.forEach((item, index) => {
    if (!item || !followsAddressFields(item)) return;
    if (oneCurrent && !item.outdated && item !== chosen) return;
    const grid = addressGrid(item, [], [], pathBase ? `${pathBase}/${index}` : '');
    if (item.outdated && !render.inOutdated) outdated.push(grid);
    else current.push(grid);
  });
  if (!current.length && (oneCurrent || !source.length)) {
    current.push(addressGrid(null, [], [], pathBase ? `${pathBase}/${source.length}` : ''));
  }
  let body = current.join('');
  if (outdated.length) {
    const flagged = list.filter((it) => it.outdated && ['item', 'address.from', 'address.to',
      'address.line.1', 'address.line.2', 'address.line.3', 'address.line.4']
      .some((key) => issuesAt(key, it.row).some((i) => i.severity !== 'info'))).length;
    body += `<details class="old-items"${expandAll() ? ' open' : ''}>`
      + `<summary>OUTDATED 地址（${outdated.length} 項，已過期毋須理會）`
      + `${flagged ? ` <span class="badge issue">⚠ ${flagged} 個問題</span>` : ''}</summary>`
      + `${outdated.join('')}</details>`;
  }
  return `<div class="span-3 address-field">${body}</div>`;
}

function section(title, body, meta = '', attrs = '') {
  return `<section class="card-section"${attrs}><header><h3>${esc(title)}</h3>`
    + `${meta ? `<span class="meta">${esc(meta)}</span>` : ''}</header>${body}</section>`;
}

function table(headers, currentRows, outdatedRows = [], cls = '') {
  if (!currentRows.length && !outdatedRows.length) return '<p class="muted">沒有資料</p>';
  const head = `<thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>`;
  const current = currentRows.length ? `<tbody>${currentRows.join('')}</tbody>` : '';
  const problems = outdatedRows.filter((row) => row.includes('cell-issue')).length;
  const old = outdatedRows.length
    ? `<tbody class="old-group${expandAll() ? ' open' : ''}"><tr class="group-toggle"><td colspan="${headers.length}">`
      + `<span class="caret"></span>OUTDATED 記錄（${outdatedRows.length} 項，已過期毋須理會）`
      + `${problems ? ` <span class="badge issue">⚠ ${problems} 個問題</span>` : ''}</td></tr>`
      + `${outdatedRows.join('')}</tbody>`
    : '';
  return `<div class="table-wrap"><table class="${cls}">${head}${current}${old}</table></div>`;
}

// ---------------------------------------------------------------- company sections

function branchRow(b) {
  const label = issuesAt('branch.label', b.row);
  const from = issuesAt('branch.from', b.row);
  const to = issuesAt('branch.to', b.row);
  const note = b.note ? `<div class="muted small">A 欄註記：${esc(b.note)}</div>` : '';
  const base = b.record_id ? `branches/${b.record_id}` : '';
  return `
    <tr class="${b.outdated ? 'outdated' : ''}"${rowAttr(b.row)}>
      ${cell(`${cellInput(b.label, base ? `${base}/label` : '')}${note}`, label)}
      ${addressLines(b).map((line, index) => `<td>${cellInput(line, base ? `${base}/lines/${index}` : '')}</td>`).join('')}
      <td>${cellInput(b.br_no, base ? `${base}/br_no` : '')}</td>
      <td>${boxed(cellInput(fmtDate(b.from), base ? `${base}/from` : ''), from)}</td>
      <td>${boxed(cellInput(fmtDate(b.to), base ? `${base}/to` : ''), to)}</td>
      ${cell(stateBadge(b))}<td class="muted">${b.row ?? ''}</td>
    </tr>${issueRow([...label, ...from, ...to], 10)}`;
}

function companyTypeTicks(general) {
  const chosen = general?.company_type === 'private' || general?.company_type === 'public' ? general.company_type : '';
  const box = (type, label) => {
    const checked = chosen === type ? ' checked' : '';
    return `<label class="id-tick"><input type="checkbox" data-path="general/company_type" data-saved="${esc(chosen)}" data-id-choice="${type}"${checked}> ${esc(label)}</label>`;
  };
  return `<div class="field span-3"><label>公司類別</label><span class="id-ticks">${box('private', '私人 Private')}${box('public', '公眾 Public')}</span></div>`;
}

function licenseFields(party, pathOf) {
  const on = party.license_not_required === true || party.license_not_required === 'true';
  const tickPath = pathOf('license_not_required');
  const bound = tickPath
    ? ` data-path="${esc(tickPath)}" data-bool="1" data-saved="${on ? 'true' : 'false'}"`
    : ' name="license_not_required" value="true"';
  return `<div class="field span-3"><label class="license-tick"><input type="checkbox"${bound}${on ? ' checked' : ''}> 無須領有牌照 Not Required to obtain a license</label></div>`
    + field('原因 Reason', party.license_reason, 'span-2', [], pathOf('license_reason'));
}
function generalSection(c) {
  const g = c.general;
  const branches = splitOutdated(c.branches);
  const body = `
    <div class="grid">
      ${field('公司編號', c.company_no || '', '', issuesAt('company_no'), 'company_no')}
      ${field('狀態', statusText(c), c.active ? '' : 'inactive', issuesAt('status'))}
      ${field('成立日期 Incorporation', fmtDate(g.incorporation_date), '', issuesAt('general.incorporation_date'), 'general/incorporation_date')}
      ${field('英文名稱', g.name_en, 'span-2', issuesAt('general.name_en'), 'general/name_en')}
      ${field('中文名稱', g.name_zh, '', issuesAt('general.name_zh'), 'general/name_zh')}
      ${field('CI', g.ci_no, '', [], 'general/ci_no')}
      ${field('BR', g.br_no, '', [], 'general/br_no')}
      ${field('已發行股數', c.issued_shares ?? '', '', [], 'issued_shares')}
      ${companyTypeTicks(g)}
      ${colorField(c)}
      ${c.status_note ? field('狀態註記', c.status_note, 'span-3') : ''}
      ${c.former_names.length ? listField('舊名稱', c.former_names, 'span-3', issuesAt('general.former_names'), 'former_names') : ''}
      ${c.business_names.length ? listField('Business name', c.business_names, 'span-3', [], 'business_names') : ''}
    </div>
    <h4>分行</h4>
    ${addRecordBox('BR', '新增分行', `${textInput('編號', 'label', '', '', '例如 BR001')}${addressInputs()}${textInput('BR', 'br_no')}${textInput('開始日期', 'from')}${textInput('結束日期', 'to')}`)}
    ${c.branches.length
    ? table(['標籤', '地址 1', '地址 2', '地址 3', '地址 4', 'BR', '開始日期', '結束日期', '狀態', 'Excel 行'],
      branches.current.map(branchRow), branches.outdated.map(branchRow))
    : ''}`;
  return section('公司資料 General', body, `Excel 第 ${c.row} 行`, rowAttr(c.row));
}

function roCard(e, pathBase = '') {
  const status = issuesAt('ro.status', e.row);
  const mark = issueMarks(status);
  return `<div class="addr-record"${rowAttr(e.row)}${locsFrom(status)}>`
    + `<div class="addr-meta">${stateBadge(e)}${outdatedButton(e.record_id, e.outdated)}`
    + `<span class="row-ref">${e.row ? `Excel 第 ${e.row} 行` : ''}</span></div>`
    + `${addressGrid(e, issuesAt('ro.from', e.row), issuesAt('ro.to', e.row), pathBase, false)}${mark.html}</div>`;
}

function roSection(c) {
  const banner = sectionIssues('registered_offices');
  const { current, outdated } = splitOutdated(c.registered_offices);
  const adder = addRecordBox('RO', '新增 RO', `${addressInputs()}${textInput('開始日期', 'from')}${textInput('結束日期', 'to')}`);
  const missing = current.length
    ? ''
    : field('現行註冊辦事處', '缺少', '', issuesAt('registered_offices').filter((i) => i.severity !== 'info'));
  if (!c.registered_offices.length) return section('註冊辦事處 RO', `${banner}${missing}${adder}`);
  const roPath = (entry) => (entry.record_id ? `registered_offices/${entry.record_id}` : `registered_offices/${c.registered_offices.indexOf(entry)}`);
  let body = adder + missing + current.map((entry) => roCard(entry, roPath(entry))).join('');
  if (outdated.length) {
    body += `<details class="old-items"${expandAll() ? ' open' : ''}>`
      + `<summary>OUTDATED 記錄（${outdated.length} 項，已過期毋須理會）</summary>`
      + `${outdated.map((entry) => roCard(entry, roPath(entry))).join('')}</details>`;
  }
  return section('註冊辦事處 RO', banner + body, '以最新開始日期的 RO 為現行');
}

function capitalRow(s) {
  const date = issuesAt('capital.date', s.row);
  const status = [...issuesAt('capital.status', s.row), ...issuesAt('capital.label', s.row)];
  const base = s.record_id ? `share_capital/${s.record_id}` : '';
  const box = (key, value) => cellInput(value, base ? `${base}/${key}` : '');
  return `
    <tr class="${s.outdated ? 'outdated' : ''}"${rowAttr(s.row)}>
      <td class="num">${box('no_of_shares', s.no_of_shares ?? '')}</td>
      <td class="num">${box('total_no_of_shares', s.total_no_of_shares ?? '')}</td>
      <td class="num">${box('consideration', s.consideration ?? '')}</td>
      <td class="num">${box('total_consideration', s.total_consideration ?? '')}</td>
      <td>${box('currency', s.currency ?? '')}</td><td>${box('class', s.class ?? '')}</td><td>${box('type', s.type ?? '')}</td>
      <td>${boxed(box('date', fmtDate(s.date)), date)}</td>
      ${cell(stateBadge(s), status)}<td class="muted">${s.row ?? ''}</td>
    </tr>${issueRow([...date, ...status], 10)}`;
}

const SHARE_FIELDS = [
  ['no_of_shares', 'No. of shares'],
  ['total_no_of_shares', 'Total no. of shares'],
  ['consideration', 'Consideration'],
  ['total_consideration', 'Total consideration'],
  ['currency', 'Currency'],
  ['class', 'Class'],
  ['type', 'Type'],
  ['date', '日期'],
];
function shareField(label, value, required, missing, issues = [], extraLocs = [], path = '') {
  const all = missing
    ? [...issues, { severity: 'warning', message: `${label} 必須填寫` }]
    : issues;
  const mark = issueMarks(all);
  const shown = missing ? '' : value;
  const control = path
    ? `<input class="value" data-path="${esc(path)}" value="${esc(shown ?? '')}" autocomplete="off">`
    : `<div class="value${isEmpty(shown) ? ' empty' : ''}">${missing ? '⚠ 缺少' : esc(shown)}</div>`;
  return `<div class="field${mark.cls}"${locsFrom(issues, extraLocs)}><label>${esc(label)}${required ? ' <span class="req">*</span>' : ''}</label>`
    + `${control}${mark.html}</div>`;
}

function capitalSection(c) {
  const current = (c.current_share_record_id && c.share_capital.find((e) => e.record_id === c.current_share_record_id))
    || c.share_capital.find((e) => e.row === c.current_share_capital_row)
    || null;
  const missing = new Set(c.share_capital_missing || []);
  const required = new Set(['no_of_shares', 'total_no_of_shares', 'consideration', 'total_consideration', 'currency']);
  const banner = sectionIssues('share_capital');
  const fields = SHARE_FIELDS.map(([key, label]) => {
    const raw = current ? current[key] : null;
    const issues = key === 'date' && current ? issuesAt('capital.date', current.row) : [];
    const extra = [];
    if (key === 'date' && current) extra.push(['capital.date', current.row]);
    if (missing.has(key)) extra.push(['share_capital', (current || c).row]);
    const index = current ? Math.max(c.share_capital.indexOf(current), 0) : 0;
    const shown = key === 'date' ? fmtDate(raw) : (raw ?? '');
    return shareField(label, shown, required.has(key), missing.has(key), issues, extra, `share_capital/${index}/${key}`);
  }).join('');
  let history = '';
  if (c.share_capital.length > 1 || (c.share_capital.length && !current)) {
    const { current: rows, outdated } = splitOutdated(c.share_capital);
    history = `<h4>股本記錄</h4>${table(
      ['No. of shares', 'Total no. of shares', 'Consideration', 'Total consideration', 'Currency', 'Class', 'Type',
        '日期', '狀態', 'Excel 行'],
      rows.map(capitalRow),
      outdated.map(capitalRow),
    )}`;
  }
  const meta = current ? `現行股本：Excel 第 ${current.row ?? '新'} 行` : '必須填寫';
  const adder = addRecordBox('SHARES', '新增股本記錄',
    ['no_of_shares', 'No. of shares', 'total_no_of_shares', 'Total no. of shares', 'consideration', 'Consideration',
      'total_consideration', 'Total consideration', 'currency', 'Currency', 'class', 'Class', 'type', 'Type', 'date', '日期']
      .reduce((html, item, index, list) => (index % 2 ? html : html + textInput(list[index + 1], item)), ''));
  return section('股本 Shares', `${banner}${adder}<div class="grid grid-4">${fields}</div>${history}`, meta);
}

const PIE_COLORS = ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7',
  '#9c755f', '#bab0ac'];
const REMAINDER_COLOR = '#d1d5db';

function moneyText(s, c) {
  if (typeof s.consideration !== 'number') return '沒有資料';
  const text = `${c.currency ? `${c.currency} ` : ''}${fmtNum(s.consideration)}`;
  return s.consideration_source === 'estimated' ? `約 ${text}（按股本比例估算）` : text;
}

function pieSlices(c) {
  const slices = c.shareholding_summary.filter((s) => s.counted_in_total && s.current_shares > 0)
    .map((s, i) => ({ ...s, color: PIE_COLORS[i % PIE_COLORS.length] }));
  const held = slices.reduce((n, s) => n + s.current_shares, 0);
  if (c.issued_shares && held < c.issued_shares) {
    const rest = c.issued_shares - held;
    slices.push({
      name: '未能對應的股數', label: '', current_shares: rest, percentage: rest / c.issued_shares,
      consideration: null, color: REMAINDER_COLOR, remainder: true,
    });
  }
  return slices;
}

function sliceCaption(s) {
  if (s.remainder) return ['未能對應', fmtPct(s.percentage)];
  const dir = (String(s.label || '').match(/DIR\d+/) || [])[0] || s.label || '';
  return [dir, s.name || '', fmtPct(s.percentage)].filter(Boolean);
}

function pieLabel(lines, x, y, anchor) {
  const top = y - ((lines.length - 1) * 12) / 2;
  return `<text text-anchor="${anchor}" font-size="11" font-weight="700">`
    + `${lines.map((line, i) => `<tspan x="${x.toFixed(1)}" y="${(top + i * 12).toFixed(1)}">${esc(line)}</tspan>`).join('')}`
    + '</text>';
}

function pieSvg(slices) {
  const total = slices.reduce((n, s) => n + s.current_shares, 0);
  if (!total) return '';
  const cx = 240;
  const cy = 170;
  const r = 86;
  const open = '<svg class="pie" viewBox="0 0 480 340" role="img" aria-label="現時持股圓餅圖">';
  let angle = -Math.PI / 2;
  const at = (a, radius) => [cx + radius * Math.cos(a), cy + radius * Math.sin(a)];
  const groups = [];
  slices.forEach((s, i) => {
    const sweep = (s.current_shares / total) * Math.PI * 2;
    const mid = angle + sweep / 2;
    const [x1, y1] = at(angle, r);
    angle += sweep;
    const [x2, y2] = at(angle, r);
    const shape = slices.length === 1
      ? `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${s.color}" data-slice="${i}"></circle>`
      : `<path d="M${cx},${cy} L${x1.toFixed(2)},${y1.toFixed(2)} A${r},${r} 0 ${sweep > Math.PI ? 1 : 0} 1 `
        + `${x2.toFixed(2)},${y2.toFixed(2)} Z" fill="${s.color}" data-slice="${i}"></path>`;
    const lines = sliceCaption(s).map((line) => (line.length > 18 ? `${line.slice(0, 17)}…` : line));
    const inside = sweep >= Math.PI * 0.42 || slices.length === 1;
    let label;
    if (inside) {
      const [lx, ly] = at(mid, slices.length === 1 ? 0 : r * 0.52);
      label = pieLabel(lines, lx, ly, 'middle');
    } else {
      const [ox, oy] = at(mid, r + 8);
      const right = Math.cos(mid) >= 0;
      const endX = ox + (right ? 28 : -28);
      label = `<polyline points="${at(mid, r * 0.72).map((n) => n.toFixed(1)).join(',')}`
        + ` ${ox.toFixed(1)},${oy.toFixed(1)} ${endX.toFixed(1)},${oy.toFixed(1)}"`
        + ` fill="none" stroke="#64748b" stroke-width="1"></polyline>`
        + pieLabel(lines, endX + (right ? 4 : -4), oy, right ? 'start' : 'end');
    }
    groups.push(`<g data-slice="${i}">${shape}${label}</g>`);
  });
  return `${open}${groups.join('')}</svg>`;
}

function sliceTip(s, c) {
  return `<div class="tip-title"><span class="swatch" style="background:${s.color}"></span>`
    + `${esc(s.name || '（未有名稱）')}${s.label ? ` <span class="muted">${esc(s.label)}</span>` : ''}</div>`
    + `<div>股數：${esc(fmtNum(s.current_shares))} 股</div>`
    + `<div>持股：${esc(fmtPct(s.percentage))}</div>`
    + (s.remainder
      ? '<div class="muted">已發行股數中未能對應持有人的部分</div>'
      : `<div>代價：${esc(moneyText(s, c))}</div>`);
}

function summarySection(c) {
  if (!c.shareholding_summary.length && c.issued_shares === null) return '';
  const slices = pieSlices(c);
  render.slices = slices;
  const rows = c.shareholding_summary.map((s) => {
    const i = s.counted_in_total ? slices.findIndex((x) => !x.remainder && x.row === s.row) : -1;
    return `
    <tr class="${s.counted_in_total ? '' : 'muted'}"${i >= 0 ? ` data-slice="${i}"` : ''}>
      <td>${i >= 0 ? `<span class="swatch" style="background:${slices[i].color}"></span>` : ''}</td>
      <td>${esc(s.name || '')}</td><td>${esc(s.label)}</td>
      <td class="num">${esc(fmtNum(s.current_shares))}</td><td class="num">${esc(fmtPct(s.percentage))}</td>
      <td class="num">${esc(moneyText(s, c))}</td>
      <td>${s.counted_in_total ? '' : '不計入總數'}</td>
    </tr>`;
  });
  const rest = slices.findIndex((s) => s.remainder);
  if (rest >= 0) {
    rows.push(`
    <tr class="muted" data-slice="${rest}">
      <td><span class="swatch" style="background:${REMAINDER_COLOR}"></span></td><td>未能對應的股數</td><td></td>
      <td class="num">${esc(fmtNum(slices[rest].current_shares))}</td><td class="num">${esc(fmtPct(slices[rest].percentage))}</td>
      <td></td><td>持股加總少於已發行股數</td>
    </tr>`);
  }
  let total = '';
  if (c.issued_shares !== null) {
    const held = c.holdings_total ?? 0;
    const ok = Math.abs(held - c.issued_shares) < 1e-6;
    total = `<p class="total ${ok ? 'ok' : 'bad'}">現行持股合計 ${fmtNum(held)} 股／已發行 ${fmtNum(c.issued_shares)} 股`
      + `${ok ? '（相符）' : '（不符，請檢查）'}</p>`;
  }
  const mismatchIssues = issuesAt('shareholding_summary').filter((i) => i.severity !== 'info');
  const mismatch = mismatchIssues.length
    ? field('現行持股合計', c.issued_shares == null ? '' : `${fmtNum(c.holdings_total ?? 0)}／${fmtNum(c.issued_shares)}`, '', mismatchIssues)
    : '';
  const chart = slices.length
    ? `<div class="pie-wrap">${pieSvg(slices)}<div class="muted small">圖上為 DIR 編號、人名及持股 %；滑鼠移上可看股數及代價</div></div>`
    : '';
  const holders = table(['', '持有人', '標籤', '現時股數', '持股 %', `代價${c.currency ? `（${c.currency}）` : ''}`, '備註'], rows);
  return section('現時持股摘要（系統按股數計算）', sectionIssues('shareholding_summary')
    + `${mismatch}<div class="summary-layout">${chart}<div class="summary-table">${holders}${total}</div></div>`);
}

function showSliceTip(el, event) {
  const s = render.slices?.[Number(el.dataset.slice)];
  if (!s) return;
  const tip = $('#pieTip');
  tip.innerHTML = sliceTip(s, render.company);
  tip.classList.remove('hidden');
  moveSliceTip(event);
  document.querySelectorAll(`#main [data-slice="${el.dataset.slice}"]`).forEach((node) => node.classList.add('active'));
}

function moveSliceTip(event) {
  const tip = $('#pieTip');
  const x = event.clientX + 16 + tip.offsetWidth > window.innerWidth ? event.clientX - tip.offsetWidth - 12 : event.clientX + 16;
  const y = event.clientY + 16 + tip.offsetHeight > window.innerHeight ? event.clientY - tip.offsetHeight - 12 : event.clientY + 16;
  tip.style.left = `${Math.max(4, x)}px`;
  tip.style.top = `${Math.max(4, y)}px`;
}

function hideSliceTip() {
  $('#pieTip').classList.add('hidden');
  document.querySelectorAll('#main [data-slice].active').forEach((node) => node.classList.remove('active'));
}

function transferText(t) {
  if (!t) return '';
  const parts = [
    fmtDate(t.date),
    t.from || t.to ? `${t.from ?? '?'} → ${t.to ?? '?'}` : '',
    isEmpty(t.no_of_shares) ? '' : `${fmtNum(t.no_of_shares)} 股`,
    isEmpty(t.consideration) ? '' : `代價 ${fmtNum(t.consideration)}`,
  ];
  return esc(parts.filter(Boolean).join('；'));
}

function trustText(trust) {
  if (!trust) return '';
  const parts = [
    trust.bo ? `BO：${trust.bo}` : '',
    trust.trustee ? `Trustee：${trust.trustee}` : '',
    fmtDate(trust.date),
    isEmpty(trust.shares) ? '' : `${fmtNum(trust.shares)} 股`,
  ];
  return esc(parts.filter(Boolean).join('；'));
}

function holdingRow(h, p, key) {
  const badges = [];
  const isCalc = p.current_holding_row != null && h.row === p.current_holding_row;
  if (h.pre_transfer || (!h.outdated && !isCalc && p.current_holding_row != null)) {
    badges.push('<span class="badge pre" title="轉讓前的記錄，不用作計算現時持股">轉讓前</span>');
  }
  if (h.outdated && !render.inOutdated) badges.push(outdatedBadge(h));
  if (!h.outdated && !h.pre_transfer && isCalc) {
    badges.push('<span class="badge ok" title="用這一行計算現時持股">現行（用作計算）</span>');
  } else if (!h.outdated && !h.pre_transfer && !isCalc && p.current_holding_row == null) {
    badges.push('<span class="badge ok">現行</span>');
  }
  const at = (issueKey) => issuesAt(issueKey, h.row);
  const base = p.party_id && h.record_id ? `${key}/${p.party_id}/shareholdings/${h.record_id}` : '';
  const box = (field, value) => cellInput(value, base ? `${base}/${field}` : '');
  const label = at('holding.label');
  const total = at('holding.total_no_of_shares');
  const pct = at('holding.percentage_excel');
  const dates = [...at('holding.date'), ...at('holding.end_date')];
  const transfer = at('holding.transfer');
  const trust = at('holding.dt');
  const money = at('holding.consideration');
  return `
    <tr class="${h.outdated ? 'outdated' : ''}${h.pre_transfer ? ' pre' : ''}"${rowAttr(h.row)}>
      ${cell(box('label', h.label || ''), label)}
      <td>${badges.join(' ')}</td>
      <td class="num">${box('no_of_shares', h.no_of_shares ?? '')}</td>
      ${cell(box('total_no_of_shares', h.total_no_of_shares ?? ''), total, 'num')}
      ${cell(esc(fmtPct(h.percentage_excel)), pct, 'num')}
      <td class="num">${box('consideration', h.consideration ?? '')}</td>
      ${cell(box('total_consideration', h.total_consideration ?? ''), money, 'num')}
      <td>${box('class', h.class ?? '')}</td><td>${box('type', h.type ?? '')}</td>
      <td>${boxed(box('date', fmtDate(h.date)), at('holding.date'))}${boxed(box('end_date', fmtDate(h.end_date)), at('holding.end_date'))}</td>
      <td class="transfer-picks"><span>由 ${shSelect(h.transfer?.from, base ? `${base}/transfer/from` : '')}</span>
        <span>至 ${shSelect(h.transfer?.to, base ? `${base}/transfer/to` : '')}</span></td>
      <td>${cellInput(h.declaration_of_trust?.bo, base ? `${base}/declaration_of_trust/bo` : '')}
        ${cellInput(h.declaration_of_trust?.trustee, base ? `${base}/declaration_of_trust/trustee` : '')}</td>
      <td class="muted">${h.row ?? ''}</td>
    </tr>${issueRow([...label, ...total, ...pct, ...money, ...dates, ...transfer, ...trust], 13)}`;
}

function holdingHasContent(holding) {
  if (!holding) return false;
  const filled = ['label', 'no_of_shares', 'total_no_of_shares', 'consideration', 'total_consideration',
    'class', 'type', 'date', 'end_date'].some((key) => holding[key] != null && String(holding[key]).trim() !== '');
  const transfer = holding.transfer || {};
  const trust = holding.declaration_of_trust || {};
  const moved = ['date', 'from', 'to', 'no_of_shares', 'consideration'].some((key) => transfer[key] != null && String(transfer[key]).trim() !== '');
  const trusted = ['bo', 'trustee', 'date', 'shares'].some((key) => trust[key] != null && String(trust[key]).trim() !== '');
  return filled || moved || trusted;
}

function holdingsTable(p, key) {
  const holdings = (p.shareholdings || []).filter((holding) => holdingHasContent(holding));
  if (!holdings.length) return '';
  let shown = holdings;
  let old = [];
  if (!render.inOutdated) {
    shown = holdings.filter((h) => !h.outdated || h.pre_transfer);
    old = holdings.filter((h) => h.outdated && !h.pre_transfer);
    if (!expandAll()) state.collapsed += old.length;
  }
  const labels = p.roles.filter((r) => r.startsWith('SH')).join('、');
  return `<h4>持股記錄${labels ? `（${esc(labels)}）` : ''}</h4>` + table(
    ['標籤', '狀態', 'No. of shares', 'Total no. of shares', '持股 %（Excel）', 'Consideration',
      'Total consideration', 'Class', 'Type', '日期', '轉讓 Transfer', 'DT', 'Excel 行'],
    shown.map((h) => holdingRow(h, p, key)),
    old.map((h) => holdingRow(h, p, key)),
    'holdings',
  );
}

function sharesNote(p) {
  if (!p.shareholdings.length || p.outdated) return '';
  const pct = p.percentage !== null ? `（${fmtPct(p.percentage)}）` : '';
  const transferred = p.shareholdings.some((h) => h.pre_transfer);
  const basis = p.current_holding_row
    ? `，按第 ${p.current_holding_row} 行${transferred ? '轉讓後記錄' : ''}計算` : '';
  return `<span class="shares">現時持股 ${esc(fmtNum(p.current_shares))} 股${esc(pct)}${esc(basis)}</span>`;
}

function partyRows(p) {
  const rows = new Set([p.row, ...p.shareholdings.map((h) => h.row)]);
  for (const key of ['id_numbers', 'addresses', 'emails', 'phones', 'other']) p[key].forEach((it) => rows.add(it.row));
  return rows;
}

function partyProblemCount(p) {
  const rows = partyRows(p);
  return render.company.issues.filter((i) => i.severity !== 'info' && rows.has(i.row)
    && /^(party|holding|item)/.test(i.field || '')).length;
}

function partyBody(p, key) {
  const person = ['directors', 'shareholders', 'company_secretaries'].includes(key);
  const corporate = person || p.ci_no || p.br_no;
  const base = p.party_id ? `${key}/${p.party_id}` : '';
  const at = (name) => (base ? `${base}/${name}` : '');
  return `
    <div class="grid">
      ${field('英文名稱 Name', p.name_en, 'span-2', [], at('name_en'))}
      ${field('中文名稱', p.name_zh, '', [], at('name_zh'))}
      ${field('出生日期 Date of birth', fmtDate(p.date_of_birth), '', issuesAt('party.date_of_birth', partyRows(p)), at('date_of_birth'))}
      ${idField(p.id_numbers, '', [], at('id_numbers'))}
      <div class="field spacer"></div>
      ${corporate ? `${field('CI', p.ci_no, '', [], at('ci_no'))}${field('BR', p.br_no, '', [], at('br_no'))}<div class="field spacer"></div>` : ''}
      ${dateField('委任日期 Appoint date', p.appoint_date, p.appoint_date_row, p.row, issuesAt('party.appoint_date', partyRows(p)), at('appoint_date'))}
      ${dateField('辭任日期 Resign date', p.resign_date, p.resign_date_row, p.row, issuesAt('party.resign_date', partyRows(p)), at('resign_date'))}
      ${key === 'company_secretaries' ? licenseFields(p, at) : ''}
      <div class="field spacer"></div>
      ${addressField(p.addresses, at('addresses'), person)}
      ${undefinedBlock([...(p.other || []), ...misplacedAddresses(p.addresses, p.other)], at('other'))}
      ${listField('Email', p.emails, '', [], at('emails'))}
      ${listField('電話 Tel', p.phones, '', [], at('phones'), true)}
      <div class="field spacer"></div>
    </div>
    ${holdingsTable(p, key)}`;
}

function partyTitle(p, key) {
  return key === 'other_parties' ? p.label_raw : p.label;
}

function rowRef(p) {
  if (p.row) return `<span class="row-ref">Excel 第 ${p.row} 行</span>`;
  if (p.added) return '<span class="row-ref">新增</span>';
  return '<span class="row-ref"></span>';
}

function blockOutdatedButton(key, party) {
  if (!['directors', 'shareholders', 'company_secretaries'].includes(key)) return '';
  return outdatedButton(party.party_id, party.outdated);
}

function outdatedButton(id, outdated) {
  if (!id) return '';
  const action = outdated ? '0' : '1';
  const label = outdated ? '取消 OUTDATED' : '標為 OUTDATED';
  return `<button type="button" class="btn small" data-outdated="${action}" data-party-id="${esc(id)}">${label}</button>`;
}

function cellInput(value, path) {
  if (!path) return esc(value ?? '');
  return `<input class="cell-input" data-path="${esc(path)}" value="${esc(value ?? '')}" autocomplete="off">`;
}

function isShareholder(party, section) {
  if (section === 'shareholders') return true;
  return [party.label, party.label_raw, ...(party.roles || [])]
    .some((text) => /\bSH\d*\b/i.test(String(text || '')));
}

function companyShareholders() {
  const people = [];
  for (const [section] of [['directors'], ['shareholders'], ['company_secretaries'], ['other_parties']]) {
    for (const party of render.company?.[section] || []) {
      if (isShareholder(party, section)) people.push(party);
    }
  }
  return people;
}

function shOptionLabel(party) {
  const name = [party.name_en, party.name_zh].filter(Boolean).join('　');
  const where = party.row ? `第${party.row}行` : (party.added ? '新增' : '');
  return [party.label || 'SH', name, where].filter(Boolean).join('　');
}

function shSelect(current, path, name = '') {
  const holders = companyShareholders();
  const ids = new Set(holders.map((party) => party.party_id).filter(Boolean));
  const extra = current && !ids.has(current)
    ? `<option value="${esc(current)}">${esc(current)}</option>` : '';
  const options = holders.map((party) => `<option value="${esc(party.party_id)}"${party.party_id === current ? ' selected' : ''}>${esc(shOptionLabel(party))}</option>`).join('');
  const named = name ? ` name="${esc(name)}"` : '';
  const bound = path ? ` data-path="${esc(path)}" data-saved="${esc(current || '')}"` : '';
  return `<select class="cell-input"${named}${bound}><option value="">—</option>${extra}${options}</select>`;
}

const ADDABLE = { directors: 'DIR', shareholders: 'SH', company_secretaries: 'CS' };

function textInput(label, name, cls = '', value = '', placeholder = '') {
  const hint = placeholder ? ` placeholder="${esc(placeholder)}"` : '';
  return `<div class="field ${cls}"><label>${esc(label)}</label><input name="${esc(name)}" value="${esc(value)}"${hint} autocomplete="off"></div>`;
}

function addressInputs() {
  return ['地址 1', '地址 2', '地址 3', '地址 4']
    .map((label, index) => textInput(label, `line${index + 1}`, index === 0 ? 'span-2' : '')).join('');
}

function addRecordBox(kind, title, inner, attrs = '') {
  return `<details class="add-party"><summary>${esc(title)}</summary>
    <form data-add-record="${esc(kind)}"${attrs}><div class="grid">${inner}</div>
    <button type="submit" class="btn">加入</button></form></details>`;
}

function addPartyBox(key) {
  const kind = ADDABLE[key];
  if (!kind || !render.company) return '';
  const title = kind === 'CS' ? '新增 Com Sec' : `新增 ${kind}`;
  const placeholder = kind === 'CS' ? '例如 CS1' : `例如 ${kind}1`;
  const corporate = kind === 'CS'
    ? '<div class="field"><label>CI</label><input name="ci_no" autocomplete="off"></div><div class="field"><label>BR</label><input name="br_no" autocomplete="off"></div>'
    : '';
  const shares = kind === 'SH'
    ? '<div class="field"><label>股數</label><input name="shares" autocomplete="off"></div>'
    : '';
  const ubo = kind === 'SH' || kind === 'CS'
    ? '<label class="ubo-tick"><input type="checkbox" name="ubo" value="true"> 是否 UBO</label>'
    : '';
  const license = kind === 'CS'
    ? '<label class="license-tick"><input type="checkbox" name="license_not_required" value="true"> 無須領有牌照 Not Required to obtain a license</label>'
      + textInput('原因 Reason', 'license_reason', 'span-2')
    : '';
  return `<details class="add-party"><summary>${esc(title)}</summary>
    <form data-add-party="${kind}">
      <div class="grid">
        ${textInput('編號', 'label', '', '', placeholder)}
        <div class="field span-2"><label>英文名稱 Name</label><input name="name_en" autocomplete="off"></div>
        <div class="field"><label>中文名稱</label><input name="name_zh" autocomplete="off"></div>
        <div class="field"><label>出生日期</label><input name="date_of_birth" autocomplete="off"></div>
        <div class="field"><label>身份證／證件</label><input name="id_number" autocomplete="off">${idTypeTicks('', '')}</div>
        <div class="field spacer"></div>
        ${corporate}
        <div class="field"><label>委任日期</label><input name="appoint_date" autocomplete="off"></div>
        <div class="field"><label>辭任日期</label><input name="resign_date" autocomplete="off"></div>
        <div class="field spacer"></div>
        <div class="field span-2"><label>地址 1</label><input name="line1" autocomplete="off"></div>
        <div class="field"><label>地址 2</label><input name="line2" autocomplete="off"></div>
        <div class="field"><label>地址 3</label><input name="line3" autocomplete="off"></div>
        <div class="field"><label>地址 4</label><input name="line4" autocomplete="off"></div>
        ${shares}
        ${ubo}
        ${license}
      </div>
      <button type="submit" class="btn">加入</button>
    </form></details>`;
}

function uboTick(party, key) {
  if (key !== 'shareholders' && key !== 'company_secretaries' && !isShareholder(party, key)) return '';
  const on = party.ubo === true || party.ubo === 'true';
  const path = party.party_id ? `${key}/${party.party_id}/ubo` : '';
  const bound = path
    ? ` data-path="${esc(path)}" data-bool="1" data-saved="${on ? 'true' : 'false'}"`
    : ' name="ubo" value="true"';
  return `<label class="ubo-tick"><input type="checkbox"${bound}${on ? ' checked' : ''}> 是否 UBO</label>`;
}

function partyCard(p, key) {
  const raw = key !== 'other_parties' && p.label_raw !== p.label
    ? `<span class="raw">原文：${esc(p.label_raw)}</span>` : '';
  const labelIssues = issuesAt('party.label', p.row);
  const headerIssues = issueMarks(labelIssues);
  return `
  <article class="party"${rowAttr(p.row)}>
    <header>
      <span class="tag${headerIssues.cls}"${locsFrom(labelIssues)}>${esc(partyTitle(p, key))}</span>${raw}
      ${uboTick(p, key)}
      ${stateBadge(p)}${sharesNote(p)}
      ${blockOutdatedButton(key, p)}
      ${rowRef(p)}
    </header>
    ${headerIssues.html ? `<div class="party-issues">${headerIssues.html}</div>` : ''}
    ${partyBody(p, key)}
  </article>`;
}

function outdatedPartyCard(p, key) {
  const name = [p.name_en, p.name_zh].filter(Boolean).join('　');
  const period = dateRange(p.appoint_date, p.resign_date);
  const problems = partyProblemCount(p);
  const labelIssues = issuesAt('party.label', p.row);
  const headerIssues = issueMarks(labelIssues);
  render.inOutdated = true;
  const body = partyBody(p, key);
  render.inOutdated = false;
  return `
  <details class="party outdated"${expandAll() ? ' open' : ''}${rowAttr(p.row)}${locsFrom(labelIssues)}>
    <summary>
      <span class="tag">${esc(partyTitle(p, key))}</span>${outdatedBadge(p)}
      ${uboTick(p, key)}
      ${problems ? `<span class="badge issue" title="展開查看有問題的欄位">⚠ ${problems} 個問題</span>` : ''}
      <span class="who">${esc(name)}</span>${period ? `<span class="dates">${esc(period)}</span>` : ''}
      ${blockOutdatedButton(key, p)}
      ${rowRef(p)}
    </summary>
    <div class="party-body">${headerIssues.html ? `<div class="party-issues">${headerIssues.html}</div>` : ''}${body}</div>
  </details>`;
}

function partySection(key, title, parties) {
  const required = key === 'directors' || key === 'company_secretaries';
  const missing = required && !parties.some((p) => !p.outdated)
    ? field(key === 'directors' ? '現任董事' : '現任公司秘書', '缺少', '',
      issuesAt(key).filter((i) => i.severity !== 'info'))
    : '';
  const adder = key === 'other_parties'
    ? addRecordBox('OTHER', '新增其他標籤', `${textInput('編號', 'label', 'span-2', '', '例如 MB1')}${textInput('英文名稱', 'name_en', 'span-2')}${textInput('中文名稱', 'name_zh')}`)
    : addPartyBox(key);
  if (!parties.length) return (missing || adder) ? section(title, missing + adder, missing ? '必須最少一個' : '') : '';
  const current = parties.filter((p) => !p.outdated);
  const outdated = parties.filter((p) => p.outdated);
  if (!expandAll()) state.collapsed += outdated.length;
  const body = adder + missing + current.map((p) => partyCard(p, key)).join('')
    + outdated.map((p) => outdatedPartyCard(p, key)).join('');
  return section(title, body, `現任 ${current.length}／共 ${parties.length}`);
}

function notesSection(c) {
  if (!c.notes.length) return '';
  const body = c.notes.map((n) => `<div class="note"${rowAttr(n.row)}><span class="muted">第 ${n.row} 行</span>　${esc(n.value)}</div>`).join('');
  return section('其他行／備註', body);
}

function issueRows(issues) {
  return issues.map((i) => `
    <tr class="link jump" data-field="${esc(i.field || '')}" data-row="${i.row ?? ''}" title="跳到這個問題的欄位">
      <td class="muted">${i.row ?? ''}</td>
      <td>${esc(FIELD_LABELS[i.field] || '—')}</td>
      <td><span class="sev ${i.severity}">${SEVERITY_TEXT[i.severity]}</span></td>
      <td>${esc(i.message)}</td></tr>`);
}

function issuesSection(c) {
  if (!c.issues.length) return section('資料問題', '<p class="muted">沒有發現問題</p>');
  const main = c.issues.filter((i) => i.severity !== 'info');
  const info = c.issues.filter((i) => i.severity === 'info');
  const headers = ['Excel 行', '欄位', '程度', '說明'];
  const hint = '<p class="muted small">按一行會跳到該問題的欄位；若資料收在 OUTDATED 內，會先自動展開。</p>';
  const body = hint
    + (main.length ? table(headers, issueRows(main)) : '<p class="muted">沒有錯誤或警告</p>')
    + (info.length
      ? `<details class="info-issues"><summary>提示（${info.length}）</summary>${table(headers, issueRows(info))}</details>`
      : '');
  return section('資料問題（附 Excel 行號）', body, `${main.length} 個錯誤／警告，${info.length} 個提示`);
}

function goToIssue(field, row) {
  const main = $('#main');
  if (!main) return false;
  const rowKey = row == null || row === '' ? '' : String(row);
  const want = field ? `${field}:${rowKey}` : '';
  let el = null;
  if (want) {
    const hits = [...main.querySelectorAll('[data-loc]')].filter((node) =>
      node.dataset.loc.split('|').includes(want) && !node.closest('tr.jump'));
    const preferUndefined = field === 'undefined'
      || (field && field.startsWith('address.') && hits.some((node) => node.closest('.undefined-block'))
        && !hits.some((node) => node.classList.contains('field') || node.classList.contains('addr-grid')));
    el = hits.find((node) => (preferUndefined ? node.closest('.undefined-block') : (
      node.classList.contains('field') || node.classList.contains('value')
      || node.classList.contains('tag') || node.classList.contains('addr-grid')))) || hits[0] || null;
    if (el && el.classList.contains('section-issues') && rowKey) {
      const note = [...main.querySelectorAll('.note')].find((node) => node.dataset.row === rowKey);
      if (note) el = note;
    }
  }
  if (!el && rowKey) {
    const hits = [...main.querySelectorAll('[data-row]')].filter((node) =>
      node.dataset.row === rowKey && !node.closest('tr.jump'));
    el = hits.find((node) => node.classList.contains('has-issue')
      || node.querySelector('.has-issue, .cell-issue, .fi')) || hits[0] || null;
  }
  if (!el) return false;
  for (let node = el; node && node !== document.body; node = node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true;
    if (node.classList.contains('old-group')) node.classList.add('open');
  }
  main.querySelectorAll('.jump-target').forEach((node) => node.classList.remove('jump-target'));
  el.classList.add('jump-target');
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  clearTimeout(goToIssue.timer);
  goToIssue.timer = setTimeout(() => el.classList.remove('jump-target'), 2400);
  return true;
}

// ---------------------------------------------------------------- rendering

function showMessage(text) {
  $('#main').innerHTML = `<div class="placeholder">${esc(text)}</div>`;
}

function renderSourceBar() {
  const d = state.data;
  const s = d.source;
  const counts = d.summary.issues;
  const sheetWarnings = (d.issues || []).filter((i) => i.severity !== 'info')
    .map((i) => `<span class="warn">⚠ ${esc(i.message)}</span>`).join('');
  const headerRows = Array.isArray(s.header_rows) ? s.header_rows : [];
  $('#sourceBar').innerHTML = `資料庫：<b>MongoDB</b>　已儲存 ${d.summary.companies} 間公司，重新整理會直接讀取，不用再上載`
    + (s.file ? `　來源檔：<b>${esc(s.file)}</b>` : '')
    + `　工作表：${esc(s.sheet || '')}　表頭：第 ${esc(headerRows.join('–'))} 行`
    + `　問題：錯誤 ${counts.error}、警告 ${counts.warning}、提示 ${counts.info}`
    + `　匯入時間：${esc(s.parsed_at ? new Date(s.parsed_at).toLocaleString('zh-HK') : '')}${sheetWarnings}`;
  $('#issueCount').textContent = counts.error + counts.warning;
}

// ---------------------------------------------------------------- advanced filter (tick boxes)

function colorOptions() {
  const groups = { orange: { count: 0, hexes: new Set() }, red: { count: 0, hexes: new Set() }, none: { count: 0 } };
  const others = new Map();
  for (const c of state.data.companies) {
    if (c.color_group === 'other') {
      const entry = others.get(c.label_color) || { hex: c.label_color, name: c.color_name, count: 0 };
      entry.count += 1;
      others.set(c.label_color, entry);
    } else {
      const group = groups[c.color_group || 'none'];
      group.count += 1;
      if (group.hexes && c.label_color) group.hexes.add(c.label_color);
    }
  }
  return { groups, others: [...others.values()].sort((a, b) => b.count - a.count) };
}

function nonOrangeKeys() {
  const { groups, others } = colorOptions();
  return [
    ...(groups.red.count ? ['red'] : []),
    ...others.map((o) => `hex:${o.hex}`),
    ...(groups.none.count ? ['none'] : []),
  ];
}

function resetFilters() {
  filters.colors = new Set(state.data.companies.map(colorKey));
  filters.statuses = new Set(STATUS_OPTIONS.map(([key]) => key));
  filters.onlyIssues = false;
}

function filtersActive() {
  const allColors = new Set(state.data.companies.map(colorKey));
  return filters.onlyIssues
    || [...allColors].some((key) => !filters.colors.has(key))
    || STATUS_OPTIONS.some(([key]) => !filters.statuses.has(key));
}

function option(kind, key, label, count, extra = '') {
  return `<label class="opt${count ? '' : ' empty'}">`
    + `<input type="checkbox" data-${kind}="${esc(key)}"${count ? '' : ' disabled'}>`
    + `${extra}<span class="lbl">${label}</span><span class="n">${count}</span></label>`;
}

function renderFilterPanel() {
  const companies = state.data.companies;
  const { groups, others } = colorOptions();
  const nonOrangeCount = companies.filter((c) => c.color_group !== 'orange').length;
  const swatches = (hexes) => [...hexes].slice(0, 4).map((h) => swatch(h)).join('');
  const otherRows = others.map((o) => option('color', `hex:${o.hex}`,
    `${esc(colorLabel(o.hex, o.name))} <code>#${esc(o.hex)}</code>`
    + ` <button type="button" class="rename" data-hex="${esc(o.hex)}" title="為這個顏色改名">✎</button>`,
    o.count, swatch(o.hex))).join('');
  const statusRows = STATUS_OPTIONS.map(([key, label]) => option('status', key, esc(label),
    companies.filter((c) => c.status === key).length)).join('');
  const orangeLabel = `橙色（${esc(orangeNote())}）`;
  $('#filterPanel').innerHTML = `
    <details class="filter-group" open>
      <summary>公司編號顏色</summary>
      <div class="opt-line">
        ${option('color', 'orange', orangeLabel, groups.orange.count, swatches(groups.orange.hexes))}
        <button type="button" class="rename" data-rename="orange" title="改「${esc(orangeNote())}」這段標註">✎</button>
      </div>
      <label class="opt parent${nonOrangeCount ? '' : ' empty'}">
        <input type="checkbox" id="nonOrange"${nonOrangeCount ? '' : ' disabled'}>
        <span class="lbl">非橙色（全部，需檢查／更新）</span><span class="n">${nonOrangeCount}</span>
      </label>
      <div class="children">
        ${groups.red.count ? option('color', 'red', '紅色（Inactive）', groups.red.count, swatches(groups.red.hexes)) : ''}
        ${otherRows}
        ${groups.none.count ? option('color', 'none', '沒有底色', groups.none.count, swatch(null)) : ''}
      </div>
    </details>
    <details class="filter-group" open>
      <summary>狀態</summary>
      ${statusRows}
    </details>
    <details class="filter-group" open>
      <summary>資料問題</summary>
      <label class="opt"><input type="checkbox" id="issueFilter">
        <span class="lbl">只看有錯誤／警告</span><span class="n">${companies.filter((c) => warnCount(c) > 0).length}</span>
      </label>
    </details>
    <div class="filter-actions">
      <button type="button" class="btn small" data-preset="all">全部顯示</button>
      <button type="button" class="btn small" data-preset="orange">只看橙色</button>
      <button type="button" class="btn small" data-preset="non-orange">只看非橙色</button>
    </div>`;
  syncPanel();
}

function syncPanel() {
  const panel = $('#filterPanel');
  panel.querySelectorAll('input[data-color]').forEach((box) => { box.checked = filters.colors.has(box.dataset.color); });
  panel.querySelectorAll('input[data-status]').forEach((box) => { box.checked = filters.statuses.has(box.dataset.status); });
  $('#issueFilter').checked = filters.onlyIssues;
  const keys = nonOrangeKeys();
  const ticked = keys.filter((key) => filters.colors.has(key)).length;
  const parent = $('#nonOrange');
  parent.checked = keys.length > 0 && ticked === keys.length;
  parent.indeterminate = ticked > 0 && ticked < keys.length;
}

function applyPreset(preset) {
  resetFilters();
  if (preset === 'orange') filters.colors = new Set(['orange']);
  if (preset === 'non-orange') filters.colors = new Set(nonOrangeKeys());
}

function renameOrangeNote() {
  const labels = customLabels();
  const current = orangeNote();
  const name = window.prompt('橙色括號裡的標註可以改成其他文字。留空即還原為「正常現行」。', current);
  if (name === null) return;
  const text = name.trim();
  if (!text || text === COLOR_GROUP_TEXT.orange) delete labels['group:orange'];
  else labels['group:orange'] = text;
  localStorage.setItem(COLOR_LABELS_KEY, JSON.stringify(labels));
  renderFilterPanel();
  renderCompany();
}

function renameColor(hex) {
  const labels = customLabels();
  const fallback = state.data.companies.find((c) => c.label_color === hex)?.color_name || '';
  const name = window.prompt(`為顏色 #${hex} 命名（留空即還原為「${fallback}」）`, labels[hex] || fallback);
  if (name === null) return;
  if (name.trim() && name.trim() !== fallback) labels[hex] = name.trim();
  else delete labels[hex];
  localStorage.setItem(COLOR_LABELS_KEY, JSON.stringify(labels));
  renderFilterPanel();
  renderList();
  renderCompany();
}

function onFilterChange(e) {
  const box = e.target;
  if (box.id === 'nonOrange') {
    nonOrangeKeys().forEach((key) => (box.checked ? filters.colors.add(key) : filters.colors.delete(key)));
  } else if (box.dataset.color) {
    if (box.checked) filters.colors.add(box.dataset.color);
    else filters.colors.delete(box.dataset.color);
  } else if (box.dataset.status) {
    if (box.checked) filters.statuses.add(box.dataset.status);
    else filters.statuses.delete(box.dataset.status);
  } else if (box.id === 'issueFilter') {
    filters.onlyIssues = box.checked;
  } else {
    return;
  }
  syncPanel();
  renderList(true);
}

function renderList(followFilter = false) {
  const q = $('#search').value.trim().toLowerCase();
  state.filtered = state.data.companies.filter((c) => (!q || searchText(c).includes(q))
    && filters.colors.has(colorKey(c))
    && filters.statuses.has(c.status)
    && (!filters.onlyIssues || warnCount(c) > 0));
  if (followFilter) {
    const visibleNow = state.filtered.some((c) => c.id === state.selectedId);
    if (!state.filtered.length) {
      showMessage('沒有符合篩選條件的公司');
    } else if (!visibleNow || !document.querySelector('#main .company-head')) {
      if (!visibleNow) state.selectedId = state.filtered[0].id;
      renderCompany();
    }
  }
  $('#companyCount').textContent = `顯示 ${state.filtered.length}／${state.data.companies.length} 間公司`
    + `${filtersActive() ? '（已套用篩選）' : ''}`;
  $('#companyList').innerHTML = state.filtered.map((c) => `
    <li class="${c.id === state.selectedId ? 'active' : ''}${c.active ? '' : ' is-inactive'}" data-id="${esc(c.id)}">
      ${swatch(c.label_color)}
      <span class="no">${esc(c.company_no ?? '—')}</span>
      <span class="name">${esc(c.general.name_en || c.general.name_zh || '')}</span>
      ${statusPill(c)}
      ${warnCount(c) ? `<span class="pill warn" title="錯誤及警告">${warnCount(c)}</span>` : ''}
    </li>`).join('');
}

function renderCompany() {
  const c = state.data?.companies.find((x) => x.id === state.selectedId);
  if (!c) {
    showMessage('沒有符合條件的公司');
    return;
  }
  state.collapsed = 0;
  render.company = c;
  hideSliceTip();
  const body = [
    generalSection(c),
    roSection(c),
    capitalSection(c),
    summarySection(c),
    ...SECTIONS.map(([key, title]) => partySection(key, title, c[key])),
    notesSection(c),
    issuesSection(c),
  ].join('');
  const idx = state.filtered.findIndex((x) => x.id === c.id);
  const main = $('#main');
  main.innerHTML = `
    <div class="company-head">
      <button type="button" class="btn" data-nav="-1" ${idx <= 0 ? 'disabled' : ''}>◀ 上一間</button>
      <div class="title">
        ${swatch(c.label_color, 'big')}
        <div>
          <h2>${esc(c.company_no ?? '—')}　${esc(c.general.name_en || '')}</h2>
          <div class="sub">${esc(c.general.name_zh || '')}</div>
        </div>
        ${c.active ? '' : `<span class="pill inactive">${esc(statusText(c))}</span>`}
      </div>
      <button type="button" class="btn" data-nav="1" ${idx < 0 || idx >= state.filtered.length - 1 ? 'disabled' : ''}>下一間 ▶</button>
    </div>
    ${state.collapsed
    ? `<div class="hint">已收起 ${state.collapsed} 項 OUTDATED（已過期）資料，毋須理會；如需查看可逐項按開，或勾選「展開所有 OUTDATED」。</div>`
    : ''}
    ${body}
    <details class="json"><summary>查看此公司的 JSON</summary><pre>${esc(JSON.stringify(c, null, 2))}</pre></details>`;
  main.scrollTop = render.keepScroll ? render.scrollTop : 0;
  render.keepScroll = false;
  if (state.selectedId) localStorage.setItem('cdd-viewer.company', state.selectedId);
  refreshExcelLiveValues();
}

function renderIssues() {
  const severity = $('#severityFilter').value;
  const q = $('#issueSearch').value.trim().toLowerCase();
  const all = [
    ...state.data.issues.map((i) => ({ ...i, company: null })),
    ...state.data.companies.flatMap((c) => c.issues.map((i) => ({ ...i, company: c }))),
  ].filter((i) => {
    if (severity === 'error' && i.severity !== 'error') return false;
    if (severity === 'warning' && i.severity === 'info') return false;
    return !q || i.message.toLowerCase().includes(q) || (i.company && searchText(i.company).includes(q));
  });
  const limit = 2000;
  const rows = all.slice(0, limit).map((i) => `
    <tr class="${i.company ? 'link jump' : ''}" ${i.company ? `data-id="${esc(i.company.id)}" data-field="${esc(i.field || '')}" data-row="${i.row ?? ''}"` : ''}>
      <td>${i.company ? `${esc(i.company.company_no ?? '—')}　${esc(i.company.general.name_en || '')}` : '（整張表）'}</td>
      <td class="muted">${i.row ?? ''}</td>
      <td><span class="sev ${i.severity}">${SEVERITY_TEXT[i.severity]}</span></td>
      <td>${esc(i.message)}</td>
    </tr>`);
  $('#issuesBody').innerHTML = `<p class="muted">共 ${all.length} 項${all.length > limit ? `（只顯示首 ${limit} 項）` : ''}；按一行會開啟該公司，並跳到相關欄位。</p>`
    + table(['公司', 'Excel 行', '程度', '說明'], rows, [], 'issues');
}

function renderAll() {
  renderSourceBar();
  resetFilters();
  renderFilterPanel();
  renderList();
  renderCompany();
}

// ---------------------------------------------------------------- actions

async function api(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function setData(data, source) {
  state.data = data;
  state.source = source;
  const fromHash = decodeURIComponent(window.location.hash.replace(/^#/, ''));
  const ids = new Set(data.companies.map((c) => c.id));
  state.selectedId = ids.has(fromHash) ? fromHash : (data.companies[0]?.id ?? null);
  renderAll();
}

async function loadDatabase() {
  const data = await api('/api/database');
  if (!data.companies.length) {
    state.data = data;
    state.source = { file: data.source?.file || '' };
    renderSourceBar();
    showMessage('資料庫還沒有公司。請在上方選擇 Excel，或按「上載 Excel」。這是一次性匯入；匯入後可以直接改欄位，也可以新增 DIR、SH、Com Sec，以及把該筆資料標為 OUTDATED。');
    return;
  }
  setData(data, { file: data.source?.file || '', database: true });
}

async function importNamed(name, replace = false) {
  try {
    await api(`/api/import?file=${encodeURIComponent(name)}&replace=${replace ? 'true' : 'false'}`, { method: 'POST' });
  } catch (err) {
    if (!replace && String(err.message).includes('取代')
      && confirm('資料庫已有公司。再匯入會取代現有資料，已新增的 DIR、SH、Com Sec 會消失。確定匯入？')) {
      return importNamed(name, true);
    }
    throw err;
  }
  await loadDatabase();
}

async function importUpload(file, replace = false) {
  const form = new FormData();
  form.append('file', file);
  try {
    await api(`/api/import/upload?replace=${replace ? 'true' : 'false'}`, { method: 'POST', body: form });
  } catch (err) {
    if (!replace && String(err.message).includes('取代')
      && confirm('資料庫已有公司。再匯入會取代現有資料，已新增的 DIR、SH、Com Sec 會消失。確定匯入？')) {
      return importUpload(file, true);
    }
    throw err;
  }
  await loadDatabase();
}

function replaceCompany(company) {
  const index = state.data.companies.findIndex((item) => item.id === company.id);
  if (index >= 0) state.data.companies[index] = company;
  render.scrollTop = $('#main').scrollTop;
  render.keepScroll = true;
  renderList();
  renderCompany();
}

async function submitRecord(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.kind = form.dataset.addRecord;
  if (form.dataset.partyId) payload.party_id = form.dataset.partyId;
  const company = await api(`/api/companies/${encodeURIComponent(state.selectedId)}/records`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  replaceCompany(company);
}

async function submitParty(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.kind = form.dataset.addParty;
  const company = await api(`/api/companies/${encodeURIComponent(state.selectedId)}/parties`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  replaceCompany(company);
}

let fieldSave = Promise.resolve();
let typingTimer = 0;

function setSave(text, bad = false) {
  let node = document.querySelector('#saveState');
  const bar = document.querySelector('#sourceBar');
  if (!bar) return;
  if (!node) {
    node = document.createElement('span');
    node.id = 'saveState';
    bar.appendChild(node);
  }
  node.className = bad ? 'warn' : '';
  node.textContent = text ? `　${text}` : '';
}

function controlValue(input) {
  if (input?.dataset?.bool) return input.checked ? 'true' : 'false';
  if (input?.dataset?.idChoice) return input.checked ? input.dataset.idChoice : '';
  return input.value;
}

function saveField(input) {
  const value = controlValue(input);
  const previous = input?.dataset?.saved != null ? input.dataset.saved : input?.defaultValue;
  if (!input?.dataset?.path || !state.selectedId || value === previous) return fieldSave;
  const path = input.dataset.path.split('/');
  setSave('正在寫入 MongoDB…');
  const rerender = input.dataset.rerender === 'resign';
  fieldSave = fieldSave.then(async () => {
    const company = await api(`/api/companies/${encodeURIComponent(state.selectedId)}/field`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, value }),
    });
    const index = state.data.companies.findIndex((item) => item.id === company.id);
    if (index >= 0) state.data.companies[index] = company;
    render.company = company;
    input.dataset.saved = value;
    if (input.dataset.idChoice) {
      input.closest('.id-row, .id-ticks')?.querySelectorAll('input[data-id-choice]').forEach((box) => {
        box.dataset.saved = value;
      });
    } else if ('defaultValue' in input) input.defaultValue = value;
    const head = document.querySelector('.company-head h2');
    if (head) head.textContent = `${company.company_no || '—'}　${company.general?.name_en || ''}`;
    renderList();
    setSave('已寫入 MongoDB');
    if (rerender) {
      render.scrollTop = $('#main').scrollTop;
      render.keepScroll = true;
      renderCompany();
    }
  }).catch((err) => setSave(`儲存失敗：${err.message}`, true));
  return fieldSave;
}

async function markOutdated(button) {
  const company = await api(`/api/companies/${encodeURIComponent(state.selectedId)}/outdated`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ party_id: button.dataset.partyId, outdated: button.dataset.outdated === '1' }),
  });
  replaceCompany(company);
}

function select(id) {
  state.selectedId = id;
  window.history.replaceState(null, '', `#${encodeURIComponent(id)}`);
  renderList();
  renderCompany();
  document.querySelector('#companyList li.active')?.scrollIntoView({ block: 'nearest' });
}

function navigate(step) {
  const idx = state.filtered.findIndex((c) => c.id === state.selectedId);
  const next = state.filtered[idx + step];
  if (next) select(next.id);
}

function openIssues() {
  if (!state.data) return;
  renderIssues();
  $('#issuesOverlay').classList.remove('hidden');
}

function closeIssues() {
  $('#issuesOverlay').classList.add('hidden');
}

function bindEvents() {
  const layout = document.querySelector('.layout');
  const sidebarToggle = $('#sidebarToggle');
  const applySidebar = (collapsed) => {
    layout.classList.toggle('sidebar-collapsed', collapsed);
    sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    sidebarToggle.title = collapsed ? '展開側欄' : '收起側欄';
    sidebarToggle.textContent = collapsed ? '⟩' : '⟨';
  };
  applySidebar(localStorage.getItem('cdd-viewer.sidebar') === 'closed');
  sidebarToggle.addEventListener('click', () => {
    const collapsed = !layout.classList.contains('sidebar-collapsed');
    applySidebar(collapsed);
    localStorage.setItem('cdd-viewer.sidebar', collapsed ? 'closed' : 'open');
  });
  $('#importFileBtn').addEventListener('click', async () => {
    const name = $('#fileSelect').value;
    if (!name) {
      alert('請先選擇 Excel。');
      return;
    }
    const hasData = (state.data?.companies || []).length > 0;
    const message = hasData
      ? `資料庫已有公司。再匯入「${name}」會覆蓋現有資料和已做的修改。確定？`
      : `把「${name}」匯入 MongoDB？之後重新整理會直接讀取，不用再上載。`;
    if (!confirm(message)) return;
    try {
      await importNamed(name, hasData);
    } catch (err) {
      alert(err.message);
    }
  });
  $('#uploadInput').addEventListener('change', async (e) => {
    const [file] = e.target.files;
    e.target.value = '';
    if (!file) return;
    const hasData = (state.data?.companies || []).length > 0;
    const message = hasData
      ? `資料庫已有公司。再上載「${file.name}」會覆蓋現有資料和已做的修改。確定？`
      : `把「${file.name}」匯入 MongoDB？之後重新整理會直接讀取，不用再上載。`;
    if (!confirm(message)) return;
    try {
      await importUpload(file, hasData);
    } catch (err) {
      alert(err.message);
    }
  });
  $('#downloadBtn').addEventListener('click', () => {
    window.location.href = '/api/export';
  });
  $('#expandOutdated').addEventListener('change', () => state.data && renderCompany());
  $('#search').addEventListener('input', () => state.data && renderList(true));
  $('#filterPanel').addEventListener('change', (e) => state.data && onFilterChange(e));
  $('#filterPanel').addEventListener('click', (e) => {
    const rename = e.target.closest('.rename');
    if (rename) {
      e.preventDefault();
      e.stopPropagation();
      if (rename.dataset.rename === 'orange') renameOrangeNote();
      else renameColor(rename.dataset.hex);
      return;
    }
    const preset = e.target.closest('[data-preset]');
    if (preset) {
      applyPreset(preset.dataset.preset);
      syncPanel();
      renderList(true);
    }
  });
  $('#companyList').addEventListener('click', (e) => {
    const li = e.target.closest('li[data-id]');
    if (li) select(li.dataset.id);
  });
  $('#main').addEventListener('mouseover', (e) => {
    const slice = e.target.closest('[data-slice]');
    if (slice) showSliceTip(slice, e);
  });
  $('#main').addEventListener('mousemove', (e) => {
    if (e.target.closest('[data-slice]')) moveSliceTip(e);
  });
  $('#main').addEventListener('mouseout', (e) => {
    if (e.target.closest('[data-slice]')) hideSliceTip();
  });
  $('#main').addEventListener('input', (e) => {
    if (!e.target.matches('input[data-path], textarea[data-path]') || e.target.matches('[data-id-choice], [data-bool]')) return;
    clearTimeout(typingTimer);
    const field = e.target;
    typingTimer = setTimeout(() => saveField(field), 500);
  });
  $('#main').addEventListener('focusout', (e) => {
    if (!e.target.matches('[data-path]') || e.target.matches('[data-id-choice], [data-bool]')) return;
    clearTimeout(typingTimer);
    saveField(e.target);
  });
  $('#main').addEventListener('change', (e) => {
    if (e.target.matches('input[data-bool]')) {
      saveField(e.target);
      return;
    }
    const choice = e.target.closest('input[data-id-choice]');
    if (choice) {
      if (choice.checked) {
        choice.closest('.id-row, .id-ticks')?.querySelectorAll('input[data-id-choice]').forEach((other) => {
          if (other !== choice) other.checked = false;
        });
      }
      if (choice.dataset.path) saveField(choice);
      return;
    }
    if (e.target.matches('select[data-path]')) saveField(e.target);
  });
  $('#main').addEventListener('submit', (e) => {
    const form = e.target.closest('form[data-add-party], form[data-add-record]');
    if (!form) return;
    e.preventDefault();
    const action = form.dataset.addRecord ? submitRecord(form) : submitParty(form);
    action.catch((err) => alert(err.message));
  });
  $('#main').addEventListener('click', (e) => {
    const outdatedBtn = e.target.closest('[data-outdated]');
    if (outdatedBtn) {
      e.preventDefault();
      e.stopPropagation();
      markOutdated(outdatedBtn).catch((err) => alert(err.message));
      return;
    }
    const toggle = e.target.closest('.group-toggle');
    if (toggle) {
      toggle.closest('tbody').classList.toggle('open');
      return;
    }
    const jump = e.target.closest('tr.jump');
    if (jump) {
      goToIssue(jump.dataset.field, jump.dataset.row);
      return;
    }
    const btn = e.target.closest('[data-nav]');
    if (btn && !btn.disabled) navigate(Number(btn.dataset.nav));
  });
  $('#issuesBtn').addEventListener('click', openIssues);
  $('#closeIssues').addEventListener('click', closeIssues);
  $('#severityFilter').addEventListener('change', renderIssues);
  $('#issueSearch').addEventListener('input', renderIssues);
  $('#issuesOverlay').addEventListener('click', (e) => {
    if (e.target.id === 'issuesOverlay') closeIssues();
    const tr = e.target.closest('tr[data-id]');
    if (tr) {
      const field = tr.dataset.field;
      const row = tr.dataset.row;
      closeIssues();
      $('#search').value = '';
      resetFilters();
      syncPanel();
      select(tr.dataset.id);
      goToIssue(field, row);
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeIssues();
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName)) return;
    if (e.key === 'ArrowRight') navigate(1);
    if (e.key === 'ArrowLeft') navigate(-1);
  });
}

const excelView = { start: 1, limit: 60, rowCount: 0, loaded: false };
let fillingSheets = false;

function excelBorderStyle(border) {
  if (!border) return '';
  return [['t', 'top'], ['r', 'right'], ['b', 'bottom'], ['l', 'left']].flatMap(([key, side]) => (
    border[key] ? [`border-${side}:${border[key]}`] : []
  )).join(';');
}

const EXCEL_MAPS_KEY = 'cdd-viewer.excel-maps';
const EXCEL_ID_OPTIONS = [{ value: 'hkid', label: 'HKID' }, { value: 'passport', label: 'Passport' }];
const EXCEL_TYPE_OPTIONS = [{ value: 'private', label: '私人 Private' }, { value: 'public', label: '公眾 Public' }];
const EXCEL_COMPANY_FIELDS = [
  ['no', '公司編號', 'text'], ['name_en', '英文名稱', 'text'], ['name_zh', '中文名稱', 'text'],
  ['ci', 'CI', 'text'], ['br', 'BR', 'text'], ['incorporation', '成立日期', 'date'],
  ['issued_shares', '已發行股數', 'number'], ['type', '公司類別', 'choice'],
];
const EXCEL_BASIC_FIELDS = [
  ['name_en', '英文名稱', 'text'], ['name_zh', '中文名稱', 'text'], ['dob', '出生日期', 'date'],
  ['id', '證件號碼', 'text'], ['id_type', '證件種類', 'choice'], ['ci', 'CI', 'text'], ['br', 'BR', 'text'],
  ['email', 'Email', 'text'], ['tel', '電話', 'text'], ['appoint', '委任日期', 'date'],
  ['resign', '辭任日期', 'date'], ['ubo', '是否 UBO', 'bool'],
  ['shares', '股數', 'number'], ['share_total', '累計股數', 'number'], ['consideration', '代價', 'number'],
  ['total_consideration', '總代價', 'number'], ['share_class', '股份類別', 'text'], ['share_type', '股份種類', 'text'],
  ['share_date', '持股日期', 'date'], ['share_end', '持股結束日期', 'date'],
];
const EXCEL_TRANSFER_FIELDS = [
  ['transfer.date', '轉讓日期', 'date'], ['transfer.from', '轉讓自', 'text'], ['transfer.to', '轉讓至', 'text'],
  ['transfer.shares', '轉讓股數', 'number'], ['transfer.consideration', '轉讓代價', 'number'],
];
const EXCEL_TRUST_FIELDS = [
  ['trust.bo', 'DT BO', 'text'], ['trust.trustee', 'DT Trustee', 'text'],
  ['trust.date', 'DT 日期', 'date'], ['trust.shares', 'DT 股數', 'number'],
];
const EXCEL_CAPITAL_FIELDS = [
  ['no_of_shares', '股數', 'number'], ['total_no_of_shares', '累計股數', 'number'],
  ['consideration', '代價', 'number'], ['total_consideration', '總代價', 'number'],
  ['currency', '貨幣', 'text'], ['class', '類別', 'text'], ['type', '種類', 'text'], ['date', '日期', 'date'],
];

function loadExcelMaps() {
  try {
    const parsed = JSON.parse(localStorage.getItem(EXCEL_MAPS_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (err) {
    return {};
  }
}

function excelMapId(file, sheet, row, col) {
  return `${file}\t${sheet}\t${row}\t${col}`;
}

function usableExcelMap(spec) {
  return Boolean(spec && spec.key && spec.label && spec.kind && spec.kind !== 'pick');
}

function excelSpec(key, label, kind, options, group) {
  return { key, label, kind, options: options || [], group };
}

function canonExcelOwner(value) {
  let text = String(value || '').replace(/\s+/g, '').toUpperCase().replace(/!/g, '1');
  if (text === '公司' || text === 'COMPANY') return 'company';
  if (text === '註冊地址' || text === 'RO') return 'RO';
  if (text === '股本' || text === 'SHARES') return 'SHARES';
  if (text.startsWith('COMSEC')) text = `CS${text.slice(6)}`;
  if (text.startsWith('DR') && !text.startsWith('DIR')) text = `DIR${text.slice(2)}`;
  if (/^(?:DIR|SH|CS)(?:\d+(?:-\d+(?:\.\d+)?)?)?$/.test(text) || /^BR[0-9A-Z-]+$/.test(text)) return text;
  return '';
}

function ownersFromLabel(value) {
  const text = String(value || '').trim();
  if (!text || isUnmappedMark(text)) return [];
  if (/^\d+$/.test(text) || text === '?' || text === '??') return ['company'];
  if (/^ro$/i.test(text)) return ['RO'];
  if (/^shares$/i.test(text)) return ['SHARES'];
  const branch = text.match(/^br\s*([0-9a-z-]+)/i);
  if (branch && text.replace(/^br\s*[0-9a-z-]+/i, '').trim() === '') return [`BR${branch[1].toUpperCase()}`];
  const upper = text.toUpperCase().replace(/!/g, '1');
  const tokens = [...upper.matchAll(/(COM\s?SEC|DIR|DR|SH|CS)\s*(\d+(?:[-.]\d+)*)?/g)];
  if (!tokens.length || upper.replace(/(COM\s?SEC|DIR|DR|SH|CS)\s*(\d+(?:[-.]\d+)*)?/g, '').replace(/[\s,&/+.-]/g, '')) return [];
  const shared = tokens.map((token) => token[2]).find(Boolean) || '';
  const owners = [];
  tokens.forEach((token) => {
    const name = token[1].startsWith('COM') ? 'CS' : (token[1] === 'DR' ? 'DIR' : token[1]);
    const owner = canonExcelOwner(name + (token[2] || shared));
    if (owner && !owners.includes(owner)) owners.push(owner);
  });
  return owners;
}

function isUnmappedMark(value) {
  return /^\s*\[\[\s*\]\]\s*$/.test(String(value || '')) || /^\s*［［\s*］］\s*$/.test(String(value || ''));
}

function fieldsForOwner(owner) {
  const canon = canonExcelOwner(owner);
  if (!canon) return [];
  if (canon === 'company') {
    return EXCEL_COMPANY_FIELDS.map(([key, label, kind]) => excelSpec(
      `company.${key}`, label, kind, kind === 'choice' ? EXCEL_TYPE_OPTIONS : [], '公司',
    ));
  }
  if (canon === 'RO') {
    return [1, 2, 3, 4].map((line) => excelSpec(`RO.address.${line}`, `RO 地址 ${line}`, 'text', [], '註冊地址'))
      .concat([
        excelSpec('RO.from', 'RO 開始日期', 'date', [], '註冊地址'),
        excelSpec('RO.to', 'RO 結束日期', 'date', [], '註冊地址'),
      ]);
  }
  if (canon === 'SHARES') {
    return EXCEL_CAPITAL_FIELDS.map(([key, label, kind]) => excelSpec(`SHARES.${key}`, `股本 ${label}`, kind, [], '股本'));
  }
  if (canon.startsWith('BR')) {
    return [1, 2, 3, 4].map((line) => excelSpec(`${canon}.address.${line}`, `${canon} 地址 ${line}`, 'text', [], '分行'))
      .concat([
        excelSpec(`${canon}.from`, `${canon} 開始日期`, 'date', [], '分行'),
        excelSpec(`${canon}.to`, `${canon} 結束日期`, 'date', [], '分行'),
        excelSpec(`${canon}.br`, `${canon} BR`, 'text', [], '分行'),
      ]);
  }
  const basic = EXCEL_BASIC_FIELDS.map(([key, label, kind]) => excelSpec(
    `${canon}.${key}`, `${canon} ${label}`, kind, kind === 'choice' ? EXCEL_ID_OPTIONS : [], '人物',
  ));
  const address = [1, 2, 3, 4].map((line) => excelSpec(`${canon}.address.${line}`, `${canon} 地址 ${line}`, 'text', [], '人物'))
    .concat([
      excelSpec(`${canon}.address.from`, `${canon} 地址開始日期`, 'date', [], '人物'),
      excelSpec(`${canon}.address.to`, `${canon} 地址結束日期`, 'date', [], '人物'),
    ]);
  const transfer = EXCEL_TRANSFER_FIELDS.map(([key, label, kind]) => excelSpec(`${canon}.${key}`, `${canon} ${label}`, kind, [], '人物'));
  const trust = EXCEL_TRUST_FIELDS.map(([key, label, kind]) => excelSpec(`${canon}.${key}`, `${canon} ${label}`, kind, [], '人物'));
  const license = canon.startsWith('CS') ? [
    excelSpec(`${canon}.license`, `${canon} 無須領有牌照`, 'bool', [], '人物'),
    excelSpec(`${canon}.license_reason`, `${canon} 牌照原因`, 'text', [], '人物'),
  ] : [];
  return basic.concat(address, transfer, trust, license);
}

function suggestExcelFields(query, owners) {
  const text = String(query || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const hint = [];
  (owners || []).forEach((owner) => {
    const canon = canonExcelOwner(owner);
    if (canon && !hint.includes(canon)) hint.push(canon);
  });
  const typed = [];
  const words = [];
  const pattern = /com\s*sec(?:\s*\d+(?:-\d+(?:\.\d+)?)?)?|公司|註冊地址|股本|\bcompany\b|\bshares\b|br\s*[0-9A-Za-z-]+|(?:dir|dr|sh|cs)\s*\d+(?:-\d+(?:\.\d+)?)?|\bro\b|\b(?:dir|dr|sh|cs)\b/gi;
  let position = 0;
  let found = pattern.exec(text);
  while (found) {
    const gap = text.slice(position, found.index).replace(/^[\s.]+|[\s.]+$/g, '');
    if (gap) words.push(...gap.split(/\s+/));
    const owner = canonExcelOwner(found[0]);
    if (owner && !typed.includes(owner)) typed.push(owner);
    position = found.index + found[0].length;
    found = pattern.exec(text);
  }
  const tail = text.slice(position).replace(/^[\s.]+|[\s.]+$/g, '');
  if (tail) words.push(...tail.split(/\s+/).filter(Boolean));
  const targets = typed.length ? typed : hint;
  const poolOwners = words.length ? targets.slice() : (targets.length ? targets : ['company', 'RO', 'SHARES']);
  if (words.length && !typed.length) {
    ['company', 'RO', 'SHARES'].forEach((extra) => {
      if (!poolOwners.includes(extra)) poolOwners.push(extra);
    });
  }
  const seen = new Set();
  const pool = [];
  poolOwners.forEach((owner) => {
    fieldsForOwner(owner).forEach((spec) => {
      if (seen.has(spec.key)) return;
      seen.add(spec.key);
      pool.push(spec);
    });
  });
  const filtered = words.length ? pool.filter((spec) => {
    const hay = `${spec.group} ${spec.label} ${spec.key} ${(spec.options || []).map((item) => item.label).join(' ')}`.toLowerCase();
    return words.every((word) => hay.includes(word.toLowerCase()));
  }) : pool;
  const ranked = filtered.map((spec, index) => {
    const owner = spec.key.split('.')[0];
    let score = owner && typed.includes(owner) ? 5 : 0;
    if (!score && hint.includes(owner)) score = 3;
    if (words.length && words.every((word) => spec.label.toLowerCase().includes(word.toLowerCase()))) score += 2;
    if (text.toLowerCase() === spec.key.toLowerCase()) score += 10;
    return { spec, index, score };
  });
  ranked.sort((a, b) => b.score - a.score || a.index - b.index);
  return ranked.slice(0, 40).map((item) => item.spec);
}

function selectedCompany() {
  return state.data?.companies?.find((item) => item.id === state.selectedId) || null;
}

function showFieldValue(value) {
  if (value === true) return '是';
  if (value === false) return '否';
  if (isEmpty(value)) return '';
  if (typeof value === 'number') return fmtNum(value);
  if (/^\d{4}-\d{2}-\d{2}/.test(String(value))) return fmtDate(String(value).slice(0, 10));
  return String(value);
}

function currentRecord(list) {
  return (list || []).find((item) => item && !item.outdated) || null;
}

function partyFor(company, owner) {
  const kind = owner.startsWith('DIR') ? 'DIR' : owner.startsWith('SH') ? 'SH' : owner.startsWith('CS') ? 'CS' : '';
  const sections = kind === 'DIR' ? ['directors']
    : kind === 'SH' ? ['shareholders', 'directors']
    : kind === 'CS' ? ['company_secretaries'] : [];
  const wanted = owner.toUpperCase();
  const normal = (text) => String(text || '').toUpperCase().replace(/\s+/g, '').replace('COMSEC', 'CS').replace(/^DR(?=\d|$)/, 'DIR');
  const matches = sections.flatMap((key) => company[key] || []).filter((party) => (
    [party.label, party.label_raw, ...(party.roles || [])].some((text) => {
      const token = normal(text);
      return token === wanted || (wanted === 'CS' && token === 'CS');
    })
  ));
  return matches.find((party) => !party.outdated) || matches[0] || null;
}

function partyFieldText(party, field) {
  const address = currentRecord(party.addresses);
  const holding = (party.shareholdings || []).find((item) => item && !item.outdated && !item.pre_transfer)
    || (party.shareholdings || []).find((item) => item && !item.outdated)
    || null;
  const line = field.match(/^address\.([1-4])$/);
  if (line) return addressLine(address, Number(line[1]) - 1);
  if (field === 'address.from') return showFieldValue(address?.from);
  if (field === 'address.to') return showFieldValue(address?.to);
  if (field.startsWith('transfer.')) {
    const name = { date: 'date', from: 'from', to: 'to', shares: 'no_of_shares', consideration: 'consideration' }[field.slice(9)];
    return showFieldValue(holding?.transfer?.[name]);
  }
  if (field.startsWith('trust.')) {
    const name = { bo: 'bo', trustee: 'trustee', date: 'date', shares: 'shares' }[field.slice(6)];
    return showFieldValue(holding?.declaration_of_trust?.[name]);
  }
  const direct = {
    name_en: party.name_en,
    name_zh: party.name_zh,
    dob: party.date_of_birth,
    id: currentRecord(party.id_numbers)?.value,
    id_type: currentRecord(party.id_numbers)?.id_type,
    ci: party.ci_no,
    br: party.br_no,
    appoint: party.appoint_date,
    resign: party.resign_date,
    email: currentRecord(party.emails)?.value,
    tel: currentRecord(party.phones)?.value,
    ubo: party.ubo,
    shares: party.current_shares,
    share_total: holding?.total_no_of_shares,
    consideration: holding?.consideration,
    total_consideration: holding?.total_consideration,
    share_class: holding?.class,
    share_type: holding?.type,
    share_date: holding?.date,
    share_end: holding?.end_date,
    license: party.license_not_required,
    license_reason: party.license_reason,
  };
  if (field === 'id_type') {
    return direct.id_type === 'passport' ? 'Passport' : direct.id_type === 'hkid' ? 'HKID' : '';
  }
  return showFieldValue(direct[field]);
}

function addressLine(record, index) {
  if (!record) return '';
  const lines = record.lines || (typeof addressLines === 'function' ? addressLines(record) : []);
  return lines[index] || '';
}

function companyFieldText(key, company = selectedCompany()) {
  if (!company || !key) return '';
  const general = company.general || {};
  if (key.startsWith('company.')) {
    const field = key.slice('company.'.length);
    const values = {
      no: company.company_no,
      name_en: general.name_en,
      name_zh: general.name_zh,
      ci: general.ci_no,
      br: general.br_no,
      incorporation: general.incorporation_date,
      issued_shares: company.issued_shares,
      type: general.company_type === 'public' ? '公眾 Public' : general.company_type === 'private' ? '私人 Private' : '',
    };
    return showFieldValue(values[field]);
  }
  if (key.startsWith('RO.')) {
    const office = currentRecord(company.registered_offices);
    const field = key.slice(3);
    const line = field.match(/^address\.([1-4])$/);
    if (line) return addressLine(office, Number(line[1]) - 1);
    if (field === 'from' || field === 'to') return showFieldValue(office?.[field]);
    return '';
  }
  if (key.startsWith('SHARES.')) {
    const capital = (company.current_share_record_id && (company.share_capital || []).find((item) => item.record_id === company.current_share_record_id))
      || currentRecord(company.share_capital);
    const field = key.slice('SHARES.'.length);
    return capital ? showFieldValue(capital[field]) : '';
  }
  const branchKey = key.match(/^(BR[0-9A-Z-]+)\.(.+)$/i);
  if (branchKey) {
    const label = branchKey[1].toUpperCase();
    const branches = company.branches || [];
    const branch = branches.find((item) => String(item.label || '').replace(/\s+/g, '').toUpperCase() === label && !item.outdated)
      || branches.find((item) => String(item.label || '').replace(/\s+/g, '').toUpperCase() === label);
    const field = branchKey[2].toLowerCase();
    const line = field.match(/^address\.([1-4])$/);
    if (line) return addressLine(branch, Number(line[1]) - 1);
    if (field === 'br') return showFieldValue(branch?.br_no);
    if (field === 'from' || field === 'to') return showFieldValue(branch?.[field]);
    return '';
  }
  const person = key.match(/^((?:DIR|SH|CS)(?:\d+(?:-\d+(?:\.\d+)?)?)?)\.(.+)$/i);
  if (!person) return '';
  const party = partyFor(company, person[1].toUpperCase());
  return party ? partyFieldText(party, person[2].toLowerCase()) : '';
}

function fieldChecked(key) {
  return /^(1|true|yes|on|y|是)$/i.test(companyFieldText(key));
}

function refreshExcelLiveValues() {
  document.querySelectorAll('.excel-live[data-excel-field]').forEach((node) => {
    const text = companyFieldText(node.dataset.excelField);
    const solo = !node.classList.contains('excel-inline-value');
    node.textContent = text || (solo ? '（沒有資料）' : '');
    node.classList.toggle('empty', !text);
  });
  document.querySelectorAll('.excel-tick input[data-excel-field]').forEach((node) => {
    node.checked = fieldChecked(node.dataset.excelField);
  });
}

function excelSelectHtml(fields) {
  const empty = '<option value="">選擇欄位</option>';
  if (!fields.length) return `${empty}<option value="" disabled>沒有相符欄位</option>`;
  return empty + fields.map((field) => {
    const spec = JSON.stringify({
      key: field.key, label: field.label, kind: field.kind, options: field.options || [], group: field.group || '',
    });
    const text = field.group ? `${field.group} · ${field.label}` : field.label;
    return `<option data-map-spec="${esc(spec)}">${esc(text)}</option>`;
  }).join('');
}

function shiftPendingMaps(delta) {
  const meta = $('#excelMeta');
  const match = meta.textContent.match(/(\d+) 格未對應/);
  if (!match) return;
  const next = Number(match[1]) + delta;
  meta.textContent = next > 0
    ? meta.textContent.replace(`${match[1]} 格未對應`, `${next} 格未對應`)
    : meta.textContent.replace(/[，、]?\d+ 格未對應/, '');
}

function commitExcelMap(cell, field) {
  if (!cell || !usableExcelMap(field)) return;
  const row = cell.dataset.excelRow;
  const col = cell.dataset.excelCol;
  const maps = loadExcelMaps();
  maps[excelMapId($('#excelFile').value, $('#excelSheet').value, row, col)] = {
    key: field.key,
    label: field.label,
    kind: field.kind,
    options: field.options || [],
    group: field.group || '',
  };
  localStorage.setItem(EXCEL_MAPS_KEY, JSON.stringify(maps));
  cell.innerHTML = excelMappedControl(field, row, col, true);
  closeExcelMapMenu();
  shiftPendingMaps(-1);
}

let excelMapMenu = null;
let excelMapActive = 0;

function closeExcelMapMenu() {
  if (!excelMapMenu) return;
  excelMapMenu.hidden = true;
  excelMapMenu._input = null;
}

function ensureExcelMapMenu() {
  if (excelMapMenu) return excelMapMenu;
  excelMapMenu = document.createElement('div');
  excelMapMenu.className = 'excel-map-menu';
  excelMapMenu.hidden = true;
  document.body.appendChild(excelMapMenu);
  excelMapMenu.addEventListener('mousedown', (event) => {
    const button = event.target.closest('[data-map-index]');
    if (!button || !excelMapMenu._input) return;
    event.preventDefault();
    const field = excelMapMenu._results[Number(button.dataset.mapIndex)];
    commitExcelMap(excelMapMenu._input.closest('td'), field);
  });
  return excelMapMenu;
}

function placeExcelMapMenu(input) {
  const menu = ensureExcelMapMenu();
  const rect = input.getBoundingClientRect();
  const height = Math.min(menu.offsetHeight || 220, 220);
  const below = window.innerHeight - rect.bottom;
  const top = below < height + 8 && rect.top > height ? rect.top - height - 2 : rect.bottom + 2;
  menu.style.left = `${Math.max(8, rect.left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  menu.style.width = `${Math.max(rect.width, 220)}px`;
}

function showExcelMapMenu(input) {
  const cell = input.closest('td');
  const owners = (cell?.dataset.excelOwners || '').split(',').filter(Boolean);
  const fields = suggestExcelFields(input.value, owners).slice(0, 12);
  const menu = ensureExcelMapMenu();
  menu._input = input;
  menu._results = fields;
  excelMapActive = 0;
  menu.innerHTML = fields.length
    ? fields.map((field, index) => {
      const live = companyFieldText(field.key);
      return `<button type="button" data-map-index="${index}" class="${index === 0 ? 'active' : ''}">`
        + `<span>${esc(field.group || '')} ${esc(field.label)}</span>`
        + `<span class="key">${esc(live || '（沒有資料）')}</span></button>`;
    }).join('')
    : '<div class="excel-map-empty">沒有相符欄位</div>';
  menu.hidden = false;
  placeExcelMapMenu(input);
}

function excelPickControl(owners) {
  const ownerName = { company: '公司', RO: '註冊地址', SHARES: '股本' };
  const names = owners.map((owner) => ownerName[owner] || owner);
  const hint = names.length ? `搜尋 ${names.join('、')}` : '搜尋欄位';
  return `<input type="search" class="excel-map-query" placeholder="${esc(hint)}" aria-label="${esc(hint)}" autocomplete="off">`;
}

function isTickToken(token) {
  return /^(?:\.{3,}|…+|⋯+|．{3,})$/.test(String(token || '').replace(/\s+/g, ''));
}

function excelBareTick() {
  return '<label class="excel-tick"><input type="checkbox" aria-label="勾選"></label>';
}

function excelTick(field) {
  const name = field.group ? `${field.group} · ${field.label}` : field.label;
  const on = fieldChecked(field.key);
  return `<label class="excel-tick" title="${esc(name)}"><input type="checkbox" data-excel-field="${esc(field.key)}"${on ? ' checked' : ''} aria-label="${esc(name)}"></label>`;
}

function excelInlineValue(field) {
  const name = field.group ? `${field.group} · ${field.label}` : field.label;
  const text = companyFieldText(field.key);
  return `<span class="excel-live excel-inline-value${text ? '' : ' empty'}" data-excel-field="${esc(field.key)}" title="${esc(name)}">${esc(text)}</span>`;
}

function resolveInlineField(token) {
  const wanted = String(token || '').trim().toLowerCase();
  if (!wanted) return null;
  return suggestExcelFields(wanted, []).find((item) => item.key.toLowerCase() === wanted) || null;
}

function inlinePartsFromText(text) {
  const raw = String(text || '');
  if (!/\[\[|［［/.test(raw) || isUnmappedMark(raw)) return null;
  const pattern = /\[\[\s*([^\[\]]*?)\s*\]\]/g;
  const parts = [];
  let position = 0;
  let found = false;
  let match = pattern.exec(raw);
  const addText = (value) => {
    if (!value) return;
    const last = parts[parts.length - 1];
    if (last && Object.keys(last).length === 1 && last.text !== undefined) last.text += value;
    else parts.push({ text: value });
  };
  while (match) {
    found = true;
    addText(raw.slice(position, match.index));
    const token = match[1].trim();
    const field = token && !isTickToken(token) ? resolveInlineField(token) : null;
    if (isTickToken(token)) parts.push({ tick: true });
    else if (!token) parts.push({ pick: true });
    else if (field) parts.push({ field });
    else addText(match[0]);
    position = match.index + match[0].length;
    match = pattern.exec(raw);
  }
  if (!found) return null;
  addText(raw.slice(position));
  if (parts.length === 1 && parts[0].field) return null;
  return parts;
}

function excelInlineHtml(parts) {
  if (!parts || !parts.length) return '';
  return `<span class="excel-inline">${parts.map((part) => {
    if (part.tick) return excelBareTick();
    if (part.field) return part.field.kind === 'bool' ? excelTick(part.field) : excelInlineValue(part.field);
    if (part.pick) return excelPickControl([]);
    return esc(part.text || '');
  }).join('')}</span>`;
}

function excelMappedControl(field) {
  if (field.kind === 'bool') return excelTick(field);
  const name = field.group ? `${field.group} · ${field.label}` : field.label;
  const text = companyFieldText(field.key);
  return `<button type="button" class="excel-live excel-live-solo${text ? '' : ' empty'}" data-excel-remap="1" data-excel-field="${esc(field.key)}" title="對應：${esc(name)}。按一下可改對應">${esc(text || '（沒有資料）')}</button>`;
}

function excelFillControl(field, value, rowNumber, col) {
  const common = ` class="excel-fill" data-excel-field="${esc(field.key)}" data-excel-row="${rowNumber}" data-excel-col="${esc(col)}" aria-label="${esc(field.label)}"`;
  if (field.kind === 'bool') {
    const on = /^(1|true|yes|on|y|是)$/i.test(String(value || '').trim());
    return `<label class="excel-fill-bool"><input type="checkbox"${common}${on ? ' checked' : ''}> ${esc(field.label)}</label>`;
  }
  if (field.kind === 'choice') {
    const options = (field.options || []).map((opt) => {
      const selected = opt.value === value || opt.label === value ? ' selected' : '';
      return `<option value="${esc(opt.value)}"${selected}>${esc(opt.label)}</option>`;
    }).join('');
    return `<select${common}><option value="">${esc(field.label)}</option>${options}</select>`;
  }
  return `<input type="text"${common} value="${esc(value || '')}" placeholder="${esc(field.label)}" autocomplete="off">`;
}

function renderExcelTable(data) {
  const scroll = $('#excelScroll');
  const end = data.rows.length ? data.rows[data.rows.length - 1].n : data.start - 1;
  const notes = [];
  if (data.truncated_rows) notes.push('只顯示前 8000 行');
  if (data.truncated_columns) notes.push('只顯示前 40 欄');
  const savedMaps = loadExcelMaps();
  const mapId = (row, col) => excelMapId(data.file, data.sheet, row, col);
  let fillCount = 0;
  let pendingCount = 0;
  data.rows.forEach((row) => data.columns.forEach((col, index) => {
    let field = row.fields && row.fields[index];
    if (isUnmappedMark(row.cells && row.cells[index])) field = { kind: 'pick' };
    if (!field) return;
    fillCount += 1;
    const saved = savedMaps[mapId(row.n, col)];
    if (field.kind === 'pick' && !usableExcelMap(saved)) pendingCount += 1;
  }));
  if (fillCount) notes.push(`可填 ${fillCount} 格`);
  if (pendingCount) notes.push(`${pendingCount} 格未對應`);
  $('#excelMeta').textContent = data.row_count
    ? `${data.sheet}　第 ${data.start}–${end} 行，共 ${data.row_count} 行${notes.length ? `　${notes.join('，')}` : ''}`
    : `${data.sheet}　沒有內容`;
  $('#excelPage').textContent = data.row_count ? `${data.start}–${end}` : '';
  $('#excelPrev').disabled = data.start <= 1;
  $('#excelNext').disabled = !data.row_count || end >= data.row_count;
  if (!data.rows.length) {
    scroll.innerHTML = '<p class="muted small">這個工作表沒有內容。</p>';
    return;
  }
  const pageStart = data.start || data.rows[0].n;
  const pageEnd = data.rows[data.rows.length - 1].n;
  const colCount = data.columns.length;
  const covered = new Set();
  const spanAt = new Map();
  (data.merges || []).forEach((merge) => {
    const r1 = Math.max(merge.r, pageStart);
    const r2 = Math.min(merge.r + merge.rs - 1, pageEnd);
    const c1 = Math.max(merge.c, 1);
    const c2 = Math.min(merge.c + merge.cs - 1, colCount);
    if (r1 > r2 || c1 > c2) return;
    spanAt.set(`${r1}:${c1}`, { rs: r2 - r1 + 1, cs: c2 - c1 + 1 });
    for (let row = r1; row <= r2; row += 1) {
      for (let col = c1; col <= c2; col += 1) {
        if (row !== r1 || col !== c1) covered.add(`${row}:${col}`);
      }
    }
  });
  const rowByNumber = new Map(data.rows.map((row) => [row.n, row]));
  const sideOf = (rowNumber, colIndex, side) => {
    const row = rowByNumber.get(rowNumber);
    const border = row && row.borders && row.borders[colIndex];
    return border && border[side];
  };
  const head = data.columns.map((col) => `<th>${esc(col)}</th>`).join('');
  const cols = ['<col style="width:42px">'].concat((data.widths || []).map((width) => (
    `<col style="width:${Math.max(0, width || 0)}px">`
  ))).join('');
  const tableWidth = 42 + (data.widths || []).reduce((sum, width) => sum + Math.max(0, width || 0), 0);
  const body = data.rows.map((row) => {
    const cells = data.columns.map((col, index) => {
      const colNumber = index + 1;
      const key = `${row.n}:${colNumber}`;
      if (covered.has(key)) return '';
      const span = spanAt.get(key);
      const style = [];
      const font = row.fonts && row.fonts[index];
      if (font) {
        if (font.s) style.push(`font-size:${font.s}pt`);
        if (font.b) style.push('font-weight:700');
        if (font.i) style.push('font-style:italic');
        if (font.f) style.push(`font-family:${esc(`"${font.f}"`)}, Calibri, sans-serif`);
        if (font.c) style.push(`color:#${font.c}`);
        if (font.w) style.push('white-space:normal');
      }
      const fill = row.fills && row.fills[index];
      if (fill) style.push(`background:#${fill}`);
      if (span) {
        const top = sideOf(row.n, index, 't');
        const left = sideOf(row.n, index, 'l');
        const right = sideOf(row.n, index + span.cs - 1, 'r');
        const bottom = sideOf(row.n + span.rs - 1, index, 'b');
        const border = excelBorderStyle({ t: top, r: right, b: bottom, l: left });
        if (border) style.push(border);
      } else {
        const border = excelBorderStyle(row.borders && row.borders[index]);
        if (border) style.push(border);
      }
      const spanAttr = span ? ` rowspan="${span.rs}" colspan="${span.cs}"` : '';
      let rawField = row.fields && row.fields[index];
      let value = row.cells[index] || '';
      if (isUnmappedMark(value)) {
        rawField = { key: '', label: '選擇欄位', kind: 'pick', options: [] };
        value = '';
      }
      const saved = rawField && rawField.kind === 'pick' ? savedMaps[mapId(row.n, col)] : null;
      const field = usableExcelMap(saved) ? saved : rawField;
      if (field) style.push('background:#f0fdf4');
      const styleAttr = style.length ? ` style="${style.join(';')}"` : '';
      const rowOwners = (row.owners && row.owners.length) ? row.owners : ownersFromLabel((row.cells || [])[0]);
      const inline = excelInlineHtml((row.parts && row.parts[index]) || inlinePartsFromText(value));
      let content = esc(value);
      if (inline) content = inline;
      else if (field && field.kind === 'pick') content = excelPickControl(rowOwners, value);
      else if (field) content = excelMappedControl(field, value, row.n, col, Boolean(rawField && rawField.kind === 'pick'));
      const marked = Boolean(field || inline);
      const owners = marked ? ` data-excel-owners="${esc(rowOwners.join(','))}" data-excel-value="${esc(value)}"` : '';
      return `<td class="${marked ? 'excel-fill-cell' : ''}" data-excel-row="${row.n}" data-excel-col="${esc(col)}"${owners}${spanAttr}${styleAttr}>${content}</td>`;
    }).join('');
    const height = row.height || data.default_row_height || 20;
    return `<tr style="height:${height}px"><th>${row.n}</th>${cells}</tr>`;
  }).join('');
  const gridClass = data.gridlines === false ? 'excel-grid no-gridlines' : 'excel-grid';
  const fontSize = data.default_font_size || 11;
  closeExcelMapMenu();
  scroll.innerHTML = `<table class="${gridClass}" style="width:${tableWidth}px;font-size:${fontSize}pt">`
    + `<colgroup>${cols}</colgroup><thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function loadExcelGrid() {
  const file = $('#excelFile').value;
  if (!file) {
    $('#excelMeta').textContent = '請先選擇 Excel。';
    return;
  }
  $('#excelMeta').textContent = '正在讀取…';
  const query = new URLSearchParams({
    file,
    start: String(excelView.start),
    limit: String(excelView.limit),
  });
  const sheet = $('#excelSheet').value;
  if (sheet) query.set('sheet', sheet);
  const data = await api(`/api/excel/grid?${query.toString()}`);
  excelView.rowCount = data.row_count;
  excelView.loaded = true;
  const sheetSelect = $('#excelSheet');
  fillingSheets = true;
  sheetSelect.innerHTML = data.sheets.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  sheetSelect.value = data.sheet;
  fillingSheets = false;
  renderExcelTable(data);
}

function bindExcelPane() {
  const layout = document.querySelector('.layout');
  const toggle = $('#excelToggle');
  const popButton = $('#excelPop');
  const excelParams = new URLSearchParams(location.search);
  const excelWindow = excelParams.get('view') === 'excel';
  const savedWidth = Number(localStorage.getItem('cdd-viewer.excel-width'));
  if (savedWidth >= 280) layout.style.setProperty('--excel-width', `${savedWidth}px`);

  const setExcelWidth = (px) => {
    const total = layout.getBoundingClientRect().width;
    const sidebar = document.querySelector('.sidebar').getBoundingClientRect().width;
    const width = Math.round(Math.min(Math.max(280, total - sidebar - 280), Math.max(280, px)));
    layout.style.setProperty('--excel-width', `${width}px`);
    localStorage.setItem('cdd-viewer.excel-width', String(width));
  };
  const handle = $('#excelResize');
  handle.addEventListener('pointerdown', (event) => {
    if (layout.classList.contains('excel-collapsed')) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = $('#excelPane').getBoundingClientRect().width;
    document.body.classList.add('excel-resizing');
    const move = (pointer) => setExcelWidth(startWidth + startX - pointer.clientX);
    const stop = () => {
      document.body.classList.remove('excel-resizing');
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', stop);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', stop);
  });
  handle.addEventListener('dblclick', () => {
    layout.style.removeProperty('--excel-width');
    localStorage.removeItem('cdd-viewer.excel-width');
  });

  popButton.addEventListener('click', () => {
    const params = new URLSearchParams({ view: 'excel' });
    const file = $('#excelFile').value;
    const sheet = $('#excelSheet').value;
    if (file) params.set('file', file);
    if (sheet) params.set('sheet', sheet);
    if (state.selectedId) params.set('company', state.selectedId);
    const opened = window.open(
      `/?${params.toString()}`,
      'cdd-excel-preview',
      'popup=yes,width=1280,height=860,resizable=yes,scrollbars=yes',
    );
    if (!opened) {
      $('#excelMeta').textContent = '瀏覽器擋住了彈出視窗。請允許這個網站開啟視窗後再按一次。';
      return;
    }
    opened.focus();
  });
  if (excelWindow) document.body.classList.add('excel-window');
  const apply = (collapsed) => {
    layout.classList.toggle('excel-collapsed', collapsed);
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggle.title = collapsed ? '展開 Excel' : '收起 Excel';
    toggle.textContent = collapsed ? 'Excel' : '⟩';
  };
  apply(excelWindow ? false : localStorage.getItem('cdd-viewer.excel') !== 'open');
  toggle.addEventListener('click', () => {
    const collapsed = !layout.classList.contains('excel-collapsed');
    apply(collapsed);
    localStorage.setItem('cdd-viewer.excel', collapsed ? 'closed' : 'open');
  });
  $('#excelRead').addEventListener('click', () => {
    excelView.start = 1;
    loadExcelGrid().catch((err) => { $('#excelMeta').textContent = err.message; });
  });
  $('#excelSheet').addEventListener('change', () => {
    if (!excelView.loaded || fillingSheets) return;
    excelView.start = 1;
    loadExcelGrid().catch((err) => { $('#excelMeta').textContent = err.message; });
  });
  $('#excelFile').addEventListener('change', () => {
    excelView.loaded = false;
    excelView.start = 1;
    excelView.rowCount = 0;
    $('#excelSheet').innerHTML = '';
    $('#excelScroll').innerHTML = '';
    $('#excelPage').textContent = '';
    $('#excelPrev').disabled = true;
    $('#excelNext').disabled = true;
    $('#excelMeta').textContent = '選擇檔案後按讀取。儲存格寫 [[]] 後，該格會變成可搜尋的下拉選單。這不會改資料庫。';
  });
  const scroll = $('#excelScroll');
  scroll.addEventListener('mousedown', (event) => {
    if (event.target.closest('.excel-tick input[data-excel-field]')) event.preventDefault();
  });
  scroll.addEventListener('focusin', (event) => {
    const input = event.target.closest('.excel-map-query');
    if (input) showExcelMapMenu(input);
  });
  scroll.addEventListener('input', (event) => {
    const input = event.target.closest('.excel-map-query');
    if (input) showExcelMapMenu(input);
  });
  scroll.addEventListener('keydown', (event) => {
    const input = event.target.closest('.excel-map-query');
    if (!input || !excelMapMenu || excelMapMenu.hidden) return;
    const buttons = [...excelMapMenu.querySelectorAll('[data-map-index]')];
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!buttons.length) return;
      excelMapActive = (excelMapActive + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons.forEach((button, index) => button.classList.toggle('active', index === excelMapActive));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const field = excelMapMenu._results[excelMapActive];
      if (field) commitExcelMap(input.closest('td'), field);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeExcelMapMenu();
    }
  });
  scroll.addEventListener('scroll', () => closeExcelMapMenu());
  document.addEventListener('mousedown', (event) => {
    if (!excelMapMenu || excelMapMenu.hidden) return;
    if (event.target.closest('.excel-map-menu') || event.target.closest('.excel-map-query')) return;
    closeExcelMapMenu();
  });
  window.addEventListener('storage', (event) => {
    if (event.key !== 'cdd-viewer.company' || !document.body.classList.contains('excel-window')) return;
    if (!state.data?.companies?.some((item) => item.id === event.newValue)) return;
    state.selectedId = event.newValue;
    refreshExcelLiveValues();
  });
  scroll.addEventListener('click', (event) => {
    const button = event.target.closest('[data-excel-remap]');
    if (!button) return;
    const cell = button.closest('td');
    const maps = loadExcelMaps();
    delete maps[excelMapId($('#excelFile').value, $('#excelSheet').value, cell.dataset.excelRow, cell.dataset.excelCol)];
    localStorage.setItem(EXCEL_MAPS_KEY, JSON.stringify(maps));
    const owners = (cell.dataset.excelOwners || '').split(',').filter(Boolean);
    cell.innerHTML = excelPickControl(owners, cell.dataset.excelValue || '');
    const input = cell.querySelector('.excel-map-query');
    if (input) input.focus();
    shiftPendingMaps(1);
  });
  $('#excelPrev').addEventListener('click', () => {
    excelView.start = Math.max(1, excelView.start - excelView.limit);
    loadExcelGrid().catch((err) => { $('#excelMeta').textContent = err.message; });
  });
  $('#excelNext').addEventListener('click', () => {
    excelView.start += excelView.limit;
    loadExcelGrid().catch((err) => { $('#excelMeta').textContent = err.message; });
  });
}

async function init() {
  bindEvents();
  bindExcelPane();
  try {
    const { files, default: defaultFile } = await api('/api/files');
    const selectEl = $('#fileSelect');
    const options = files.map((f) => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join('');
    selectEl.innerHTML = options;
    $('#excelFile').innerHTML = options;
    if (files.length) {
      selectEl.value = defaultFile || files[0].name;
      $('#excelFile').value = selectEl.value;
    }
  } catch {
    // The company list still comes from MongoDB when the Excel folder cannot be listed.
  }
  try {
    await loadDatabase();
  } catch (err) {
    if (new URLSearchParams(location.search).get('view') !== 'excel') showMessage(`載入失敗：${err.message}`);
  }
  const params = new URLSearchParams(location.search);
  if (params.get('view') === 'excel') {
    const wanted = params.get('file');
    if (wanted && [...$('#excelFile').options].some((option) => option.value === wanted)) {
      $('#excelFile').value = wanted;
    }
    const companyId = params.get('company');
    if (companyId && state.data?.companies?.some((item) => item.id === companyId)) {
      state.selectedId = companyId;
    }
    const sheet = params.get('sheet');
    if (sheet) {
      $('#excelSheet').innerHTML = `<option value="${esc(sheet)}">${esc(sheet)}</option>`;
      $('#excelSheet').value = sheet;
    }
    if ($('#excelFile').value) {
      loadExcelGrid().catch((err) => { $('#excelMeta').textContent = err.message; });
    }
  }
}

init();
