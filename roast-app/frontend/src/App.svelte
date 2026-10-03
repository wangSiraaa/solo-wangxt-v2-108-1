<script>
  import { onMount } from 'svelte';
  import RoastChart from './lib/RoastChart.svelte';
  import {
    getBatches,
    seed,
    getSeries,
    getCompare,
    addEvent,
    listEvents,
    exportBatch,
    recompute,
    listCalibrations,
    createCalibration,
    activateCalibration,
    withdrawCalibration,
    CAL_STATUS_LABELS,
    EVENT_LABELS,
    fmtTime,
  } from './lib/api.js';

  let batches = [];
  let view = 'single'; // single | compare
  let selA = null;
  let selB = null;
  let dataA = null;
  let dataB = null;
  let comparePayload = null;
  let eventHistory = [];
  let loading = '';
  let error = '';

  // Analysis parameters — affect DERIVED traces only, never stored samples.
  let windowS = 30;
  let smoothS = 12;
  let maxGapFillS = 45;

  // event correction form
  let newEventType = 'turning_point';
  let newEventTime = '1:00';
  let newEventDamper = '';
  let showHistory = false;

  // export verification
  let verifyResult = null;

  // calibration ledger
  let calibrations = [];
  let calConflict = null; // 409 detail: {conflicts: [...]}
  let calForm = {
    channel: 'bean',
    start: '1:40',
    end: '8:20',
    scale: '1.05',
    offset: '2.0',
    by: '校准员',
    note: '',
    replaces_id: null,
  };

  const phaseKeys = [
    ['drying_s', '脱水期', '下豆 → 回温点'],
    ['maillard_s', '梅纳/反应期', '回温点 → 一爆开始'],
    ['development_s', '发展期', '一爆开始 → 出锅'],
    ['first_crack_window_s', '一爆持续', '一爆开始 → 一爆结束'],
    ['total_s', '总时长', '下豆 → 出锅'],
  ];

  const ANCHOR_LABELS = {
    charge: '下豆',
    turning_point: '回温点',
    first_crack_start: '一爆',
    first_crack_end: '一爆末',
    drop: '出锅',
  };

  onMount(loadBatches);

  async function loadBatches() {
    error = '';
    try {
      batches = await getBatches();
      if (batches.length) {
        selA = batches[0].id;
        selB = batches[batches.length - 1].id;
        await refresh();
      }
    } catch (e) {
      error = `无法连接后端：${e.message}`;
    }
  }

  async function doSeed() {
    loading = '正在生成合成批次…';
    error = '';
    try {
      await seed();
      await loadBatches();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function refresh() {
    if (!selA) return;
    loading = '加载曲线…';
    error = '';
    verifyResult = null;
    calConflict = null;
    const params = {
      window_s: windowS,
      display_smooth_s: smoothS,
      max_gap_fill_s: maxGapFillS,
    };
    try {
      const [series, cals] = await Promise.all([
        getSeries(selA, { ...params, include_history: showHistory }),
        listCalibrations(selA),
      ]);
      dataA = series;
      calibrations = cals;
      eventHistory = await listEvents(selA, showHistory);
      if (view === 'compare' && selB && selB !== selA) {
        comparePayload = await getCompare(selA, selB, params);
        dataB = comparePayload.batches[1];
      } else {
        comparePayload = null;
        dataB = null;
      }
    } catch (e) {
      if (e.status === 409 && e.detail?.error === 'calibration_conflict') {
        // Visible conflict: analysis is blocked until an explicit ruling.
        calConflict = e.detail;
        dataA = null;
        comparePayload = null;
        dataB = null;
        try {
          calibrations = await listCalibrations(selA);
        } catch {
          /* ledger list is best-effort here */
        }
      } else {
        error = e.message;
      }
    } finally {
      loading = '';
    }
  }

  function parseMMSS(str) {
    const m = /^(\d+):([0-5]?\d)$/.exec(str.trim());
    if (!m) return null;
    return Number(m[1]) * 60 + Number(m[2]);
  }

  async function submitEvent() {
    const t = parseMMSS(newEventTime);
    if (t === null) {
      error = '时间格式应为 m:ss，例如 1:05';
      return;
    }
    const body = {
      event_type: newEventType,
      t_s: t,
      source: 'manual',
      created_by: '操作员(界面)',
      label: `${EVENT_LABELS[newEventType] || newEventType} 人工修正`,
    };
    if (newEventType === 'damper_change') {
      const v = Number(newEventDamper);
      if (!Number.isFinite(v)) {
        error = '风门变化需要填写新风门开度 (%)';
        return;
      }
      body.value_num = v;
      body.label = `风门 → ${v}% 人工标记`;
    }
    loading = '保存修正…';
    error = '';
    try {
      await addEvent(selA, body);
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function submitCalibration() {
    const t0 = parseMMSS(calForm.start);
    const t1 = parseMMSS(calForm.end);
    const scale = Number(calForm.scale);
    const offset = Number(calForm.offset || '0');
    if (t0 === null || t1 === null) {
      error = '校准有效时间段格式应为 m:ss，例如 1:40';
      return;
    }
    if (!Number.isFinite(scale) || !Number.isFinite(offset)) {
      error = '校准参数 scale / offset 必须是数字';
      return;
    }
    loading = '保存校准草稿…';
    error = '';
    try {
      await createCalibration(selA, {
        channel: calForm.channel,
        t_start_s: t0,
        t_end_s: t1,
        formula: 'affine',
        scale,
        offset_c: offset,
        created_by: calForm.by || '校准员',
        note: calForm.note,
        replaces_id: calForm.replaces_id,
      });
      calForm.replaces_id = null;
      calForm.note = '';
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function calAction(id, action) {
    loading = action === 'activate' ? '启用校准…' : '撤回校准…';
    error = '';
    try {
      if (action === 'activate') await activateCalibration(id);
      else await withdrawCalibration(id);
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function startReplace(cal) {
    // prefill the form as the next version of `cal`
    calForm = {
      channel: cal.channel,
      start: fmtTime(cal.t_start_s),
      end: fmtTime(cal.t_end_s),
      scale: String(cal.scale),
      offset: String(cal.offset_c),
      by: calForm.by,
      note: `取代 v${cal.version}：`,
      replaces_id: cal.id,
    };
    error = '';
  }

  async function verifyExport() {
    loading = '导出并重算校验…';
    error = '';
    verifyResult = null;
    try {
      const ex = await exportBatch(selA, {
        window_s: windowS,
        display_smooth_s: smoothS,
      });
      const rc = await recompute({
        samples: ex.series.raw_points.map((p) => ({
          t_s: p.t_s,
          bean_temp_c: p.bean_temp_c,
          env_temp_c: p.env_temp_c,
        })),
        events: ex.events,
        params: ex.params,
        calibrations: ex.calibrations,
      });
      const keys = Object.keys(ex.metrics).filter(
        (k) => k.endsWith('_s') || k === 'development_ratio'
      );
      const rows = keys.map((k) => ({
        key: k,
        exported: ex.metrics[k],
        recomputed: rc.metrics[k],
        match: ex.metrics[k] === rc.metrics[k],
      }));
      // corrected series must reproduce exactly under the same versions
      const corrSig = (d) =>
        JSON.stringify(
          d.series.raw_points.map((p) => [p.t_s, p.bean_temp_corrected_c, p.ror_c_per_min])
        );
      const correctedSame = corrSig(ex) === corrSig(rc);
      const calVersionsSame =
        JSON.stringify((ex.calibrations || []).map((c) => [c.id, c.version])) ===
        JSON.stringify((rc.calibration.applied || []).map((c) => [c.id, c.version]));
      // Changing window/smoothing must leave every stored sample untouched.
      const alt = await getSeries(selA, {
        window_s: windowS * 2,
        display_smooth_s: smoothS === 0 ? 30 : 0,
        max_gap_fill_s: maxGapFillS,
      });
      const sig = (arr) =>
        JSON.stringify(arr.map((p) => [p.t_s, p.bean_temp_c, p.env_temp_c]));
      const rawSame = sig(ex.series.raw_points) === sig(alt.series.raw_points);
      verifyResult = { rows, rawSame, correctedSame, calVersionsSame, exportObj: ex };
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function downloadExport() {
    if (!verifyResult?.exportObj) return;
    const blob = new Blob([JSON.stringify(verifyResult.exportObj, null, 2)], {
      type: 'application/json',
    });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${verifyResult.exportObj.batch.name}-export.json`;
    a.click();
  }

  $: chartPayloads =
    view === 'compare' && comparePayload
      ? comparePayload.batches
      : dataA
        ? [dataA]
        : [];

  let refreshTimer;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }
</script>

<header style="padding:14px 20px;border-bottom:1px solid var(--line)">
  <h1>咖啡烘焙批次曲线 · 过程记录对比</h1>
  <div class="muted" style="margin-top:2px">
    豆温 / 环境温度 / 操作事件的过程视角 · 合成数据离线运行，<b>未连接真实烘焙机</b>
  </div>
</header>

<main style="padding:16px 20px;display:flex;flex-direction:column;gap:14px">
  {#if error}
    <div class="warn">⚠ {error}</div>
  {/if}

  {#if calConflict}
    <section class="panel" style="border-color:#e35d5d">
      <h2 style="color:#e35d5d">⛔ 校准冲突 — 分析与导出已阻断</h2>
      <div class="muted" style="margin-bottom:8px">
        同一通道存在有效时间段相交的<b>已启用</b>校准，系统不会静默挑选其一。
        请明确裁决：撤回其中一条，或以新版本取代。
      </div>
      {#each calConflict.conflicts as cf}
        <div class="warn" style="margin-bottom:6px">
          通道 <b>{cf.channel === 'bean' ? '豆温' : '环温'}</b>：
          相交区间 {fmtTime(cf.overlap_t_start_s)} – {fmtTime(cf.overlap_t_end_s)}
          （{cf.overlap_t_start_s.toFixed(0)}s – {cf.overlap_t_end_s.toFixed(0)}s）
          <div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">
            {#each cf.calibrations as c}
              <span class="tag">
                #{c.id} v{c.version} · {fmtTime(c.t_start_s)}–{fmtTime(c.t_end_s)}
              </span>
              <button class="ghost" on:click={() => calAction(c.id, 'withdraw')}>
                撤回 #{c.id} (v{c.version})
              </button>
              <button
                class="ghost"
                on:click={() => startReplace(calibrations.find((k) => k.id === c.id))}
              >
                以新版本取代 #{c.id}
              </button>
            {/each}
          </div>
        </div>
      {/each}
    </section>
  {/if}

  <section class="panel">
    <div class="row" style="align-items:flex-end">
      <div>
        <div class="muted">数据</div>
        {#if batches.length === 0}
          <button on:click={doSeed}>① 生成两个合成批次（含噪声/不均采样/探针缺测）</button>
        {:else}
          <button class="ghost" on:click={doSeed}>重新生成合成批次</button>
        {/if}
      </div>
      <div>
        <div class="muted">视图</div>
        <label class="inline">
          <input type="radio" bind:group={view} value="single" on:change={refresh} />单批次
        </label>
        <label class="inline">
          <input type="radio" bind:group={view} value="compare" on:change={refresh} />双批次对比
        </label>
      </div>
      <div>
        <div class="muted">批次 A</div>
        <select bind:value={selA} on:change={refresh}>
          {#each batches as b}
            <option value={b.id}>{b.name} · {b.bean}</option>
          {/each}
        </select>
      </div>
      {#if view === 'compare'}
        <div>
          <div class="muted">批次 B</div>
          <select bind:value={selB} on:change={refresh}>
            {#each batches as b}
              <option value={b.id}>{b.name} · {b.bean}</option>
            {/each}
          </select>
        </div>
      {/if}
    </div>

    <div class="row" style="margin-top:12px;align-items:flex-end">
      <label class="inline">
        RoR 回归窗口
        <input
          type="number"
          min="5"
          max="300"
          step="5"
          bind:value={windowS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        显示平滑（仅 RoR 曲线）
        <input
          type="number"
          min="0"
          max="180"
          step="3"
          bind:value={smoothS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        最大插值桥接
        <input
          type="number"
          min="5"
          max="600"
          step="5"
          bind:value={maxGapFillS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      {#if loading}<span class="muted">{loading}</span>{/if}
    </div>
    <div class="muted" style="margin-top:6px;font-size:12px">
      RoR 口径：在每个实测时刻，对居中 ±{(windowS / 2).toFixed(0)}s 时间窗内的<b>实测</b>豆温点做最小二乘直线拟合取斜率（°C/min），
      至少 4 个点且跨度 ≥10s 才出值；插值点不参与拟合，缺测宽缺口处 RoR 断档。调整窗口/平滑<b>只改变派生曲线，不改原始温度</b>。
    </div>
  </section>

  <section class="panel">
    <h2>探针校准账本（本地 · 只追加 · 不改写原始采样）</h2>
    <div class="muted" style="font-size:12px;margin-bottom:8px">
      公式：校正温度 = scale × 原始 + offset（°C），仅作用于有效时间段内的<b>实测点</b>；
      缺测保持缺测，插值段仍标记为非实测。记录经 草稿 → 启用 → 撤回/被取代 流转，全部历史可审计。
      {#if dataA?.calibration}
        当前视图口径：<b>{dataA.calibration.basis === 'corrected' ? '校正后' : '原始（无已启用校准）'}</b>
        {#each dataA.calibration.applied as c}
          <span class="tag" style="margin-left:6px">
            #{c.id} {c.channel === 'bean' ? '豆温' : '环温'} v{c.version} ·
            {fmtTime(c.t_start_s)}–{fmtTime(c.t_end_s)} · ×{c.scale} {c.offset_c >= 0 ? '+' : ''}{c.offset_c}
          </span>
        {/each}
      {/if}
    </div>

    <div class="row" style="align-items:flex-end;gap:8px;flex-wrap:wrap">
      <div>
        <div class="muted">通道</div>
        <select bind:value={calForm.channel}>
          <option value="bean">豆温</option>
          <option value="env">环境温度</option>
        </select>
      </div>
      <div>
        <div class="muted">起效 m:ss</div>
        <input bind:value={calForm.start} style="width:70px" />
      </div>
      <div>
        <div class="muted">失效 m:ss</div>
        <input bind:value={calForm.end} style="width:70px" />
      </div>
      <div>
        <div class="muted">scale</div>
        <input bind:value={calForm.scale} style="width:70px" />
      </div>
      <div>
        <div class="muted">offset °C</div>
        <input bind:value={calForm.offset} style="width:70px" />
      </div>
      <div>
        <div class="muted">创建人</div>
        <input bind:value={calForm.by} style="width:90px" />
      </div>
      <div>
        <div class="muted">备注</div>
        <input bind:value={calForm.note} style="width:180px" placeholder="维护后零点/比例漂移" />
      </div>
      <button on:click={submitCalibration}>
        {calForm.replaces_id ? `存为 #${calForm.replaces_id} 的新版本（草稿）` : '新建校准草稿'}
      </button>
      {#if calForm.replaces_id}
        <button class="ghost" on:click={() => (calForm.replaces_id = null)}>取消取代</button>
      {/if}
    </div>

    <table style="margin-top:10px">
      <tr>
        <th>#</th><th>通道</th><th>版本</th><th>有效时间段</th><th>公式与参数</th>
        <th>创建人</th><th>状态</th><th>沿革</th><th>操作</th>
      </tr>
      {#each calibrations as c}
        <tr style={c.status === 'active' ? '' : 'opacity:.55'}>
          <td>{c.id}</td>
          <td>{c.channel === 'bean' ? '豆温' : '环温'}</td>
          <td>v{c.version}</td>
          <td>{fmtTime(c.t_start_s)} – {fmtTime(c.t_end_s)}</td>
          <td style="font-size:11px">affine · ×{c.scale} {c.offset_c >= 0 ? '+' : ''}{c.offset_c} °C</td>
          <td>{c.created_by}</td>
          <td><span class="tag">{CAL_STATUS_LABELS[c.status] || c.status}</span></td>
          <td class="muted" style="font-size:11px">
            {#if c.replaces_id}取代 #{c.replaces_id}{/if}
            {#if c.superseded_by_id}被 #{c.superseded_by_id} 取代{/if}
          </td>
          <td style="white-space:nowrap">
            {#if c.status === 'draft'}
              <button class="ghost" on:click={() => calAction(c.id, 'activate')}>启用</button>
              <button class="ghost" on:click={() => calAction(c.id, 'withdraw')}>撤回</button>
            {:else if c.status === 'active'}
              <button class="ghost" on:click={() => calAction(c.id, 'withdraw')}>撤回</button>
              <button class="ghost" on:click={() => startReplace(c)}>新版本取代</button>
            {/if}
          </td>
        </tr>
      {/each}
      {#if calibrations.length === 0}
        <tr><td colspan="9" class="muted">暂无校准记录 — 上方可新建草稿</td></tr>
      {/if}
    </table>
  </section>

  {#if dataA}
    <section class="panel">
      <RoastChart {chartPayloads} {windowS} {smoothS} />
      <div class="row" style="margin-top:6px;font-size:12px">
        <span class="tag">圆点＝实测豆温</span>
        <span class="tag">虚线菱形＝线性插值（非实测）</span>
        <span class="tag">细点线＝环境温度</span>
        <span class="tag">金色竖虚线＝风门变化</span>
        <span class="tag">曲线断档＝缺测未桥接</span>
        {#if dataA?.calibration?.applied?.length}
          <span class="tag" style="color:#5fd08a">绿线/绿影＝校正后曲线与校准有效段</span>
          <span class="tag">虚线 RoR＝原始口径对照</span>
        {/if}
      </div>
      {#if comparePayload}
        <div class="warn" style="margin-top:8px">{comparePayload.interpretation}</div>
      {/if}
    </section>

    <section class="row">
      <div class="panel col">
        <h2>阶段指标（明确区间）</h2>
        <div class="row" style="gap:8px">
          {#each view === 'compare' && dataB ? [dataA, dataB] : [dataA] as pl, i}
            <div style="flex:1;min-width:260px">
              <div class="muted" style="margin-bottom:4px">
                {i === 0 ? 'A' : 'B'} · {pl.batch.name}
              </div>
              <table>
                <tr><th>阶段</th><th>区间定义</th><th>时长</th><th>来源</th></tr>
                {#each phaseKeys as [key, label, def]}
                  <tr>
                    <td>{label}</td>
                    <td class="muted" style="font-size:11px">{def}</td>
                    <td>{fmtTime(pl.metrics[key])}</td>
                    <td style="font-size:11px">
                      {#if key === 'drying_s'}
                        <span class="tag {pl.metrics.anchors.turning_point?.source}">
                          {pl.metrics.anchors.turning_point?.source || '—'}
                        </span>
                      {:else if key === 'maillard_s'}
                        <span class="tag {pl.metrics.anchors.first_crack_start?.source}">
                          {pl.metrics.anchors.first_crack_start?.source || '—'}
                        </span>
                      {:else if key === 'development_s' || key === 'first_crack_window_s'}
                        <span class="tag {pl.metrics.anchors.first_crack_start?.source}">
                          FC {pl.metrics.anchors.first_crack_start?.source || '—'}
                        </span>
                      {/if}
                    </td>
                  </tr>
                {/each}
                <tr>
                  <td><b>发展时间比 DTR</b></td>
                  <td class="muted" style="font-size:11px">发展期 / 总时长</td>
                  <td>
                    <b>
                      {pl.metrics.development_ratio !== null
                        ? (pl.metrics.development_ratio * 100).toFixed(1) + '%'
                        : '—'}
                    </b>
                  </td>
                  <td></td>
                </tr>
                {#if pl.metrics.anchor_temps}
                  <tr>
                    <td><b>锚点温度</b></td>
                    <td class="muted" style="font-size:11px">原始 → 校正（就近实测点）</td>
                    <td colspan="2" style="font-size:11px">
                      {#each Object.entries(pl.metrics.anchor_temps) as [k, v]}
                        {#if v}
                          <span class="tag" style="margin:1px">
                            {ANCHOR_LABELS[k] || k}: {v.bean_temp_raw_c.toFixed(1)} →
                            <b style={v.bean_temp_corrected_c !== v.bean_temp_raw_c ? 'color:#5fd08a' : ''}>
                              {v.bean_temp_corrected_c.toFixed(1)}
                            </b>°C
                          </span>
                        {/if}
                      {/each}
                    </td>
                  </tr>
                {/if}
              </table>
            </div>
          {/each}
        </div>
      </div>

      <div class="panel col">
        <h2>人工修正事件（批次 A）· 保留来源</h2>
        <div class="row" style="gap:8px;align-items:flex-end">
          <div>
            <div class="muted">类型</div>
            <select bind:value={newEventType}>
              {#each Object.entries(EVENT_LABELS) as [k, v]}
                {#if k !== 'charge'}<option value={k}>{v}</option>{/if}
              {/each}
            </select>
          </div>
          <div>
            <div class="muted">时间 m:ss</div>
            <input bind:value={newEventTime} placeholder="1:05" style="width:80px" />
          </div>
          {#if newEventType === 'damper_change'}
            <div>
              <div class="muted">新风门 %</div>
              <input bind:value={newEventDamper} type="number" min="0" max="100" style="width:80px" />
            </div>
          {/if}
          <button on:click={submitEvent}>提交修正</button>
          <label class="inline" style="align-self:center">
            <input type="checkbox" bind:checked={showHistory} on:change={refresh} />
            显示已被取代的旧值
          </label>
        </div>

        <table style="margin-top:10px">
          <tr><th>事件</th><th>时间</th><th>来源</th><th>备注</th><th>状态</th></tr>
          {#each eventHistory as e}
            <tr style={e.superseded ? 'opacity:.45' : ''}>
              <td>
                {EVENT_LABELS[e.event_type] || e.event_type}
                {e.value_num !== null && e.value_num !== undefined ? ` → ${e.value_num}%` : ''}
              </td>
              <td>{fmtTime(e.t_s)}</td>
              <td>
                <span class="tag {e.source}">{e.source === 'manual' ? '人工' : '自动建议'}</span>
                {e.created_by}
              </td>
              <td class="muted" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis">
                {e.label}
              </td>
              <td>{e.superseded ? '已被修正取代（保留）' : '当前'}</td>
            </tr>
          {/each}
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>缺测与插值审计 · 导出可复现</h2>
      <div class="row">
        <div style="flex:1;min-width:280px">
          <table>
            <tr><th>通道</th><th>起(s)</th><th>止(s)</th><th>缺测点</th><th>处理</th></tr>
            {#each dataA.series.missing_segments as g}
              <tr>
                <td>{g.channel === 'bean' ? '豆温' : '环境'}</td>
                <td>{g.t_start_s.toFixed(1)}</td>
                <td>{g.t_end_s.toFixed(1)}</td>
                <td>{g.n_missing}</td>
                <td>
                  {#if g.status === 'interpolated'}
                    <span style="color:#f3c98b">线性插值并标记（非实测）</span>
                  {:else if g.status === 'wide_unfilled'}
                    <span style="color:#e35d5d">缺口超 {maxGapFillS}s，不桥接（曲线断档）</span>
                  {:else}
                    端点缺测，不填充
                  {/if}
                </td>
              </tr>
            {/each}
          </table>
          <div class="muted" style="font-size:12px;margin-top:6px">
            实测豆温 {dataA.series.raw_points.filter((p) => p.bean_temp_c !== null).length} /
            总点 {dataA.series.raw_points.length}；
            插值点 {dataA.series.interpolated_t_s.length} 个，仅用于引导线，不写回原始采样表。
          </div>
        </div>
        <div style="flex:1;min-width:280px">
          <button on:click={verifyExport}>
            ② 导出 JSON 并用 /api/recompute 重算全部阶段指标
          </button>
          {#if verifyResult}
            <table style="margin-top:10px">
              <tr><th>指标</th><th>导出值</th><th>独立重算</th><th>一致</th></tr>
              {#each verifyResult.rows as r}
                <tr>
                  <td>{r.key}</td>
                  <td>{r.exported ?? '—'}</td>
                  <td>{r.recomputed ?? '—'}</td>
                  <td>{r.match ? '✅' : '❌'}</td>
                </tr>
              {/each}
            </table>
            <div style="margin-top:8px">
              <span class="{verifyResult.rawSame ? '' : 'warn'}">
                改变窗口/平滑后原始豆温/环温逐点比对：
                {verifyResult.rawSame ? '✅ 完全不变' : '❌ 被修改'}
              </span>
              <br />
              <span class="{verifyResult.correctedSame && verifyResult.calVersionsSame ? '' : 'warn'}">
                同一校准版本独立重算（校正曲线 + RoR + 指标）：
                {verifyResult.correctedSame && verifyResult.calVersionsSame
                  ? `✅ 一致（${(verifyResult.exportObj.calibrations || [])
                      .map((c) => `#${c.id} v${c.version}`)
                      .join(', ') || '无已启用校准，原始口径'}）`
                  : '❌ 不一致'}
              </span>
              <button class="ghost" style="margin-left:10px" on:click={downloadExport}>
                下载导出 JSON
              </button>
            </div>
          {/if}
        </div>
      </div>
    </section>
  {/if}
</main>
