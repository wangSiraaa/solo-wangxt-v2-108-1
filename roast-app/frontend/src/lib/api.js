// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster.
const qs = (p) =>
  new URLSearchParams(
    Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== '')
  );

async function req(url, options) {
  const r = await fetch(url, options);
  if (!r.ok) {
    let payload = null;
    try {
      payload = await r.json();
    } catch {
      /* non-JSON error body */
    }
    const detail = payload?.detail;
    const msg =
      (detail && typeof detail === 'object' ? detail.message : detail) || r.statusText;
    const e = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    e.status = r.status;
    e.payload = payload;
    throw e;
  }
  return r.json();
}

const post = (body) => ({
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

export async function getBatches() {
  return req('/api/batches');
}

export async function seed() {
  return req('/api/seed', { method: 'POST' });
}

export async function getSeries(batchId, params = {}) {
  return req(`/api/batches/${batchId}/series?${qs(params)}`);
}

export async function getCompare(a, b, params = {}) {
  return req(`/api/compare?${qs({ a, b, ...params })}`);
}

export async function addEvent(batchId, ev) {
  return req(`/api/batches/${batchId}/events`, post(ev));
}

export async function listEvents(batchId, includeHistory = false) {
  return req(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`);
}

export async function exportBatch(batchId, params = {}) {
  return req(`/api/batches/${batchId}/export?${qs(params)}`);
}

export async function recompute(body) {
  return req('/api/recompute', post(body));
}

// ---------------------------------------------------------------------------
// calibration ledger
// ---------------------------------------------------------------------------

export async function listCalibrations(batchId) {
  return req(`/api/batches/${batchId}/calibrations`);
}

export async function createCalibration(batchId, body) {
  return req(`/api/batches/${batchId}/calibrations`, post(body));
}

// action: activate | withdraw | supersede
export async function calAction(calId, action, body = {}) {
  return req(`/api/calibrations/${calId}/${action}`, post(body));
}

export const CAL_STATUS_LABELS = {
  draft: '草稿',
  active: '启用中',
  withdrawn: '已撤回',
  superseded: '已被新版本取代',
};

export const CAL_CHANNEL_LABELS = { bean: '豆温', env: '环境温度' };

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
