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
  return `${colorLabel(c.label_color, c.color_name)}　#${c.label_color}（${COLOR_GROUP_TEXT[c.color_group]}）`;
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
  $('#filterPanel').innerHTML = `
    <details class="filter-group" open>
      <summary>公司編號顏色</summary>
      ${option('color', 'orange', '橙色（正常現行）', groups.orange.count, swatches(groups.orange.hexes))}
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
      renameColor(rename.dataset.hex);
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

async function init() {
  bindEvents();
  try {
    const { files, default: defaultFile } = await api('/api/files');
    const selectEl = $('#fileSelect');
    selectEl.innerHTML = files.map((f) => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join('');
    if (files.length) selectEl.value = defaultFile || files[0].name;
  } catch {
    // The company list still comes from MongoDB when the Excel folder cannot be listed.
  }
  try {
    await loadDatabase();
  } catch (err) {
    showMessage(`載入失敗：${err.message}`);
  }
}

init();
