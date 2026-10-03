// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster.
const qs = (p) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null));

async function check(r) {
  if (r.ok) return r.json();
  const text = await r.text();
  let detail = null;
  try {
    detail = JSON.parse(text)?.detail ?? null;
  } catch {
    /* non-JSON error body */
  }
  const msg =
    (detail && (typeof detail === 'string' ? detail : detail.message)) || text || `HTTP ${r.status}`;
  const err = new Error(msg);
  err.status = r.status;
  err.detail = detail; // e.g. {error: 'calibration_conflict', conflicts: [...]}
  throw err;
}

const get = (url) => fetch(url).then(check);
const post = (url, body) =>
  fetch(
    url,
    body === undefined
      ? { method: 'POST' }
      : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
  ).then(check);

export function getBatches() {
  return get('/api/batches');
}

export function seed() {
  return post('/api/seed');
}

export function getSeries(batchId, params = {}) {
  return get(`/api/batches/${batchId}/series?${qs(params)}`);
}

export function getCompare(a, b, params = {}) {
  return get(`/api/compare?${qs({ a, b, ...params })}`);
}

export function addEvent(batchId, ev) {
  return post(`/api/batches/${batchId}/events`, ev);
}

export function listEvents(batchId, includeHistory = false) {
  return get(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`);
}

export function exportBatch(batchId, params = {}) {
  return get(`/api/batches/${batchId}/export?${qs(params)}`);
}

export function recompute(body) {
  return post('/api/recompute', body);
}

// --- calibration ledger -----------------------------------------------------

export function listCalibrations(batchId) {
  return get(`/api/batches/${batchId}/calibrations`);
}

export function createCalibration(batchId, body) {
  return post(`/api/batches/${batchId}/calibrations`, body);
}

export function activateCalibration(id) {
  return post(`/api/calibrations/${id}/activate`);
}

export function withdrawCalibration(id) {
  return post(`/api/calibrations/${id}/withdraw`);
}

export const CAL_STATUS_LABELS = {
  draft: '草稿',
  active: '已启用',
  withdrawn: '已撤回',
  superseded: '已被取代',
};

export const EVENT_LABELS = {
  charge: '下豆/开火',
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
  damper_change: '风门变化',
  custom: '自定义',
};

export function fmtTime(s) {
  if (s === null || s === undefined) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
