// Exercise the real frontend state transitions with small DOM/Leaflet stand-ins.
// Network completion is controlled explicitly to cover races during navigation.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function element() {
  let value = '';
  return {
    get value() { return value; }, set value(next) { value = String(next); },
    textContent: '', hidden: false, disabled: false, open: false, checked: false,
    style: {}, children: [], attributes: {}, dataset: {}, listeners: {},
    classList: {toggle() {}},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); },
    dispatch(type, event = {}) { for (const callback of this.listeners[type] || []) callback(event); },
    focus() {}, select() {},
    showModal() { this.open = true; }, close() { this.open = false; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return {left: 0, top: 0, right: 100, bottom: 100}; },
  };
}

function harness() {
  const nodes = new Map(), groups = [], deferredTicks = [], requests = [];
  const el = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const map = {
    setView() { return this; }, on() { return this; }, fitBounds() {},
    removeLayer() {}, closePopup() {}, invalidateSize() {},
    getCenter() { return {lat: 32.05, lng: 118.79}; }, getZoom() { return 12; },
  };
  let serverJobs = [], fetchRoute = null;
  const response = value => ({ok: true, json: async () => value});
  const leaflet = {
    map: () => map, canvas: () => ({}),
    tileLayer: () => ({addTo() { return this; }, on() { return this; }, redraw() {}}),
    layerGroup: () => { const group = {features: [], addTo() { return this; }, clearLayers() { this.features = []; }}; groups.push(group); return group; },
    control: {layers: () => ({addTo() { return this; }, getContainer: () => el('layers-control')}), scale: () => ({addTo() {}})},
    drawLocal: {draw: {handlers: {rectangle: {tooltip: {}}, simpleshape: {tooltip: {}}}}},
    Draw: {Rectangle: function () { this.enable = () => {}; this.disable = () => {}; }},
    draw: {},
    rectangle: () => ({addTo() { return this; }, getBounds() { return {}; }}),
    geoJSON: feature => ({addTo(group) { group.features.push(feature); }}),
  };
  leaflet.Draw.Event = {CREATED: 'created'};
  const context = vm.createContext({
    document: {getElementById: el, createElement: element, querySelector: el},
    location: {hash: '#token=test'}, L: leaflet,
    localStorage: {getItem: () => null, setItem() {}},
    window: {addEventListener() {}}, URLSearchParams, AbortController,
    ResizeObserver: class { observe() {} },
    setTimeout: (callback, delay) => { if (delay === 0) deferredTicks.push(callback); return 1; }, clearTimeout() {},
    fetch: async (url, options) => {
      requests.push({url, options});
      if (fetchRoute) { const result = fetchRoute(url, options); if (result !== undefined) return result; }
      if (url === '/api/jobs') return response(serverJobs);
      throw Error('Unexpected test request: ' + url);
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);
  el('export-kind').value = 'bundle';
  return {
    el, groups, requests, response,
    run: code => vm.runInContext(code, context),
    jobs: value => { serverJobs = value; }, route: handler => { fetchRoute = handler; },
    flush: async () => { deferredTicks.splice(0).forEach(callback => callback()); await new Promise(setImmediate); },
  };
}

const complete = id => ({id, name: `项目 ${id}`, status: 'complete', progress: 100, stage: '完成',
  created_at: '2026-10-05', bbox: [118.745, 32.010, 118.835, 32.090], files: ['geojson', 'bundle', 'log'], stats: {nodes: 2}});
const feature = {type: 'Feature', properties: {kind: 'pipe'}, geometry: {type: 'LineString', coordinates: []}};

test('state-save warnings keep real progress and cancellation available, and clear on recovery', async () => {
  const h = harness();
  const running = {...complete('run'), status: 'running', progress: 25, stage: '正在下载道路',
    persistence_warning: '任务状态已保存到恢复文件，正在重试更新历史记录。'};
  h.jobs([running]); await h.run('refresh()');
  assert.match(h.el('stage').textContent, /正在下载道路.*恢复文件/);
  assert.equal(h.el('percent').textContent, '25%');
  assert.equal(h.el('cancel').hidden, false);
  assert.equal(h.el('cancel').disabled, false);
  h.jobs([{...running, progress: 35, persistence_warning: undefined}]); await h.run('refresh()');
  assert.equal(h.el('stage').textContent, '正在下载道路');
  assert.equal(h.el('percent').textContent, '35%');
});

test('manual coordinates above the former 500 km² limit submit unchanged', async () => {
  const h = harness(), bbox = [118.7, 31.9, 119.0, 32.1]; // Approximately 631 km².
  await h.run('refresh()');
  ['west', 'south', 'east', 'north'].forEach((id, i) => { h.el(id).value = bbox[i]; });
  h.el('project-name').value = '扩大范围'; h.el('method').value = 'withtopo';
  h.el('apply').onclick();
  assert.equal(h.el('message').hidden, true);
  let submitted;
  h.route((url, options) => {
    if (url === '/api/jobs' && options.method === 'POST') {
      submitted = JSON.parse(options.body);
      const job = {...complete('larger'), name: submitted.name, bbox: submitted.bbox,
        status: 'running', progress: 0, files: ['log']};
      h.jobs([job]); return h.response(job);
    }
  });
  await h.el('generate').onclick();
  assert.deepEqual(submitted.bbox, bbox);
});

test('the frontend accepts 1000 km² and prevents submissions above it', async () => {
  const h = harness(); await h.run('refresh()');
  const boundsForArea = area => [0, -0.05, area / (111.32 * 111.32 * 0.1), 0.05];
  assert.doesNotThrow(() => h.run(`validateBbox(${JSON.stringify(boundsForArea(1000))})`));
  const bbox = boundsForArea(1000.01);
  ['west', 'south', 'east', 'north'].forEach((id, i) => { h.el(id).value = bbox[i]; });
  h.el('project-name').value = '过大范围';
  await h.el('generate').onclick();
  assert.match(h.el('message').textContent, /1000 km²/);
  assert.equal(h.requests.some(request => request.options.method === 'POST'), false);
});

test('opening and polling history keep the home map unselected', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()');
  assert.equal(h.run('selected'), null);
  assert.equal(h.el('map-title').textContent, '主地图');
  assert.equal(h.el('export').disabled, true);
  assert.equal(h.el('layers-control').hidden, true);
  assert.equal(h.requests.some(request => request.url.endsWith('/geojson')), false);
});

test('a late GeoJSON response cannot add layers after returning home', async () => {
  const h = harness(); let finish;
  h.jobs([complete('a')]); await h.run('refresh()');
  h.route(url => url.endsWith('/geojson') ? new Promise(resolve => { finish = resolve; }) : undefined);
  h.run("selectProject('a')"); h.run('showHome()');
  finish(h.response({features: [feature]})); await new Promise(setImmediate);
  assert.equal(h.run('selected'), null);
  assert.equal(h.run('loaded'), null);
  assert.equal(h.groups.reduce((count, group) => count + group.features.length, 0), 0);
  await h.run('refresh()'); assert.equal(h.run('selected'), null);
});

test('home during the last drawing chunk invalidates loaded state and permits reselect', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()');
  h.route(url => url.endsWith('/geojson') ? h.response({features: [feature]}) : undefined);
  h.run("selectProject('a')"); await new Promise(setImmediate);
  assert.equal(h.groups[0].features.length, 1);
  h.run('showHome()'); await h.flush();
  assert.equal(h.run('loaded'), null);
  assert.equal(h.groups[0].features.length, 0);
  h.run("selectProject('a')"); await new Promise(setImmediate); await h.flush();
  assert.equal(h.run('loaded'), 'a'); assert.equal(h.groups[0].features.length, 1);
});

test('deleting the selected project removes selection, output and export availability', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()');
  h.route(url => {
    if (url.endsWith('/geojson')) return h.response({features: [feature]});
    if (url.endsWith('/delete')) { h.jobs([]); return h.response({deleted: 'a'}); }
  });
  h.run("selectProject('a')"); await new Promise(setImmediate); await h.flush();
  h.run("openDelete('a')"); await h.el('delete-confirm').onclick();
  assert.equal(h.run('selected'), null); assert.equal(h.run('jobs.length'), 0);
  assert.equal(h.groups[0].features.length, 0); assert.equal(h.el('export').disabled, true);
  assert.equal(h.el('map-title').textContent, '主地图');
});

test('active cancellation stays available while browsing another project or the home map', async () => {
  const h = harness(), running = {...complete('run'), status: 'running', progress: 35, stage: '正在获取 OSM 信息', files: ['log']};
  h.jobs([running, complete('old')]); await h.run('refresh()');
  h.route(url => {
    if (url.endsWith('/geojson')) return h.response({features: []});
    if (url.endsWith('/run/cancel')) { h.jobs([{...running, status: 'cancelling'}, complete('old')]); return h.response({}); }
  });
  h.run("selectProject('old')");
  assert.match(h.el('task-title').textContent, /项目 run/);
  h.run('showHome()');
  assert.equal(h.el('cancel-side').hidden, false); assert.equal(h.el('cancel').hidden, false);
  await h.el('cancel-side').onclick();
  assert.equal(h.el('cancel').disabled, true); assert.equal(h.el('cancel-side').disabled, true);
  assert.equal(h.run('selected'), null); assert.equal(h.el('stage').textContent, '正在获取 OSM 信息');
});

test('failed task error is readable without replacing unrelated user messages', async () => {
  const h = harness(); h.jobs([{...complete('bad'), status: 'failed', error: 'OSM 请求超时', files: ['log']}]);
  await h.run('refresh()'); h.run("selectProject('bad'); message('坐标已更新'); renderProgress()");
  assert.equal(h.el('stage').textContent, 'OSM 请求超时');
  assert.equal(h.el('message').textContent, '坐标已更新');
});

test('home selected during task creation stays home when the task later completes', async () => {
  const h = harness(); await h.run('refresh()'); let finish;
  h.el('project-name').value = '新任务'; h.el('method').value = 'withtopo';
  h.route((url, options) => url === '/api/jobs' && options.method === 'POST' ? new Promise(resolve => { finish = resolve; }) : undefined);
  const creation = h.el('generate').onclick(); h.run('showHome()');
  const running = {...complete('new'), name: '新任务', status: 'running', files: ['log'], progress: 0};
  h.jobs([running]); finish(h.response(running)); await creation;
  assert.equal(h.run('selected'), null); assert.equal(h.el('cancel').hidden, false);
  h.jobs([{...complete('new'), name: '新任务'}]); await h.run('refresh()');
  assert.equal(h.run('selected'), null); assert.equal(h.el('map-title').textContent, '主地图');
  assert.equal(h.requests.some(request => request.url.endsWith('/geojson')), false);
});

test('renaming updates history and selected project without changing the selected id', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()');
  h.route(url => {
    if (url.endsWith('/geojson')) return h.response({features: []});
    if (url.endsWith('/rename')) { const renamed = {...complete('a'), name: '重命名成果'}; h.jobs([renamed]); return h.response(renamed); }
  });
  h.run("selectProject('a'); openRename('a')"); h.el('rename-name').value = '重命名成果';
  await h.el('rename-form').onsubmit({preventDefault() {}});
  assert.equal(h.run('selected'), 'a'); assert.equal(h.el('map-title').textContent, '重命名成果');
  assert.equal(h.el('project-name').value, '重命名成果'); assert.equal(h.el('rename-dialog').open, false);
});

test('pause and resume act on the active task while another project is selected', async () => {
  const h = harness(); let running = {...complete('run'), status: 'running', progress: 35, files: ['log']};
  h.jobs([running, complete('old')]); await h.run('refresh()');
  h.route(url => {
    if (url.endsWith('/geojson')) return h.response({features: []});
    if (url === '/api/jobs/run/pause') { running = {...running, status: 'pausing'}; h.jobs([running, complete('old')]); return h.response(running); }
    if (url === '/api/jobs/run/resume') { running = {...running, status: 'resuming'}; h.jobs([running, complete('old')]); return h.response(running); }
  });
  h.run("selectProject('old')");
  assert.equal(h.el('pause-side').textContent, '暂停生成');
  await h.el('pause').onclick();
  assert.equal(h.el('pause-side').textContent, '撤销暂停');
  assert.match(h.el('stage').textContent, /当前下载分块或计算步骤结束后暂停/);
  assert.equal(h.el('percent').textContent, '35%');
  running = {...running, status: 'paused'}; h.jobs([running, complete('old')]); await h.run('refresh()');
  assert.equal(h.el('pause').textContent, '恢复生成');
  assert.equal(h.el('generate').disabled, true);
  assert.equal(h.el('cancel').disabled, false);
  assert.equal(h.el('jobs').children[0].children[1].children[1].disabled, true);
  await h.el('pause-side').onclick();
  assert.equal(h.el('pause').disabled, true);
  assert.equal(h.el('pause').textContent, '正在恢复…');
  assert.equal(h.run('selected'), 'old');
  assert.match(h.el('task-title').textContent, /恢复中.*项目 run/);
});

test('pending pause can be withdrawn and paused tasks can still be cancelled', async () => {
  const h = harness(); let running = {...complete('run'), status: 'pausing', files: ['log']};
  h.jobs([running]); await h.run('refresh()');
  h.route(url => {
    if (url.endsWith('/resume')) { running = {...running, status: 'running'}; h.jobs([running]); return h.response(running); }
    if (url.endsWith('/cancel')) { running = {...running, status: 'cancelling'}; h.jobs([running]); return h.response(running); }
  });
  await h.el('pause-side').onclick();
  assert.equal(h.el('pause').textContent, '暂停生成');
  running = {...running, status: 'paused'}; h.jobs([running]); await h.run('refresh()');
  h.run('showHome()'); await h.el('cancel-side').onclick();
  assert.equal(h.el('cancel').disabled, true);
  assert.equal(h.el('pause').disabled, true);
  assert.equal(h.requests.some(request => request.url.endsWith('/resume')), true);
  assert.equal(h.requests.some(request => request.url.endsWith('/cancel')), true);
});

test('pause request failures re-enable controls and preserve progress', async () => {
  const h = harness(); h.jobs([{...complete('run'), status: 'running', progress: 42}]); await h.run('refresh()');
  h.route(url => url.endsWith('/pause') ? {ok: false, json: async () => ({error: '任务正在结束，请稍候'})} : undefined);
  await h.el('pause').onclick();
  assert.equal(h.el('pause').disabled, false);
  assert.equal(h.el('percent').textContent, '42%');
  assert.match(h.el('message').textContent, /暂停失败.*任务正在结束/);
});

test('a pending pause disables duplicate requests but leaves cancellation available', async () => {
  const h = harness(); const running = {...complete('run'), status: 'running', progress: 42};
  h.jobs([running]); await h.run('refresh()'); let finish;
  h.route(url => url.endsWith('/pause') ? new Promise(resolve => { finish = resolve; }) : undefined);
  const pause = h.el('pause').onclick();
  assert.equal(h.el('pause').disabled, true);
  assert.equal(h.el('pause-side').disabled, true);
  assert.equal(h.el('cancel').disabled, false);
  await h.el('pause-side').onclick();
  assert.equal(h.requests.filter(request => request.url.endsWith('/pause')).length, 1);
  const paused = {...running, status: 'paused'}; h.jobs([paused]); finish(h.response(paused)); await pause;
  assert.equal(h.el('pause').disabled, false);
  assert.equal(h.el('pause').textContent, '恢复生成');
});

for (const clearCache of [false, true]) test(`deleting sends the explicit cache choice ${clearCache}`, async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()'); let payload;
  h.route((url, options) => { if (url.endsWith('/delete')) { payload = JSON.parse(options.body); h.jobs([]); return h.response({deleted: 'a'}); } });
  h.run("openDelete('a')"); assert.equal(h.el('delete-cache').checked, false);
  h.el('delete-cache').checked = clearCache; await h.el('delete-confirm').onclick();
  assert.deepEqual(payload, {clear_cache: clearCache});
  assert.equal(h.el('delete-dialog').open, false);
});

test('deleting another project while paused allows keeping cache but blocks shared-cache clearing', async () => {
  const h = harness(); h.jobs([{...complete('run'), status: 'paused'}, complete('old')]); await h.run('refresh()');
  h.run("openDelete('old')");
  assert.equal(h.el('delete-confirm').disabled, false);
  assert.equal(h.el('delete-cache-busy').hidden, false);
  h.el('delete-cache').checked = true; h.el('delete-cache').onchange();
  assert.equal(h.el('delete-confirm').disabled, true);
  await h.el('delete-confirm').onclick();
  assert.equal(h.requests.some(request => request.url.endsWith('/delete')), false);
  h.el('delete-cache').checked = false; h.el('delete-cache').onchange();
  assert.equal(h.el('delete-confirm').disabled, false);
});

test('delete rejection preserves the project and cache choice for review', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()');
  h.route(url => url.endsWith('/delete') ? {ok: false, json: async () => ({error: '基础数据缓存正被使用，请稍后再试'})} : undefined);
  h.run("openDelete('a')"); h.el('delete-cache').checked = true;
  await h.el('delete-confirm').onclick();
  assert.equal(h.run('jobs.length'), 1);
  assert.equal(h.el('delete-dialog').open, true);
  assert.equal(h.el('delete-cache').checked, true);
  assert.equal(h.el('delete-confirm').disabled, false);
  assert.match(h.el('delete-error').textContent, /缓存正被使用/);
});

test('cache dialog shows sizes, safe defaults, and submits the selected categories', async () => {
  const h = harness(); await h.run('refresh()'); let payload;
  h.route((url, options) => {
    if (url === '/api/cache') return h.response({generation: {bytes: 2 * 1024**2, files: 5}, map: {bytes: 512, files: 1}, busy: false});
    if (url === '/api/cache/clear') { payload = JSON.parse(options.body); return h.response({cleared_bytes: 2 * 1024**2, cleared_files: 5}); }
  });
  await h.el('manage-cache').onclick();
  assert.equal(h.el('cache-generation').checked, true);
  assert.equal(h.el('cache-map').checked, false);
  assert.match(h.el('cache-generation-size').textContent, /2.0 MB.*5 个文件/);
  assert.match(h.el('cache-map-size').textContent, /512 B.*1 个文件/);
  h.el('cache-generation').checked = false; h.el('cache-generation').onchange();
  assert.equal(h.el('cache-confirm').disabled, true);
  h.el('cache-map').checked = true; h.el('cache-map').onchange();
  await h.el('cache-confirm').onclick();
  assert.deepEqual(payload, {generation: false, map: true});
  assert.equal(h.el('cache-dialog').open, false);
  assert.match(h.el('message').textContent, /缓存已清除.*已有模型成果保留/);
});

test('paused generation blocks cache clearing and completion refreshes the open dialog', async () => {
  const h = harness(); let busy = true;
  h.jobs([{...complete('run'), status: 'paused'}]); await h.run('refresh()');
  h.route(url => url === '/api/cache' ? h.response({generation: {bytes: 123, files: 1}, map: {bytes: 0, files: 0}, busy}) : undefined);
  await h.el('manage-cache').onclick();
  assert.equal(h.el('cache-dialog').open, true);
  assert.equal(h.el('cache-confirm').disabled, true);
  assert.equal(h.el('cache-busy').hidden, false);
  await h.el('cache-confirm').onclick();
  assert.equal(h.requests.some(request => request.url.endsWith('/clear')), false);
  busy = false; h.jobs([complete('run')]); await h.run('refresh()'); await new Promise(setImmediate);
  assert.equal(h.el('cache-busy').hidden, true);
  assert.equal(h.el('cache-confirm').disabled, false);
});

test('server rejection of cache clearing keeps dialog open with the actual Chinese reason', async () => {
  const h = harness(); await h.run('refresh()');
  h.route(url => {
    if (url === '/api/cache') return h.response({generation: {bytes: 123, files: 1}, map: {bytes: 0, files: 0}, busy: false});
    if (url === '/api/cache/clear') return {ok: false, json: async () => ({error: '任务正在运行或暂停，暂不能清除缓存'})};
  });
  await h.el('manage-cache').onclick(); await h.el('cache-confirm').onclick();
  assert.equal(h.el('cache-dialog').open, true);
  assert.equal(h.el('cache-back').disabled, false);
  assert.match(h.el('cache-error').textContent, /清除缓存失败.*正在运行或暂停/);
});

test('cache inventory errors keep confirmation disabled until a successful refresh', async () => {
  const h = harness(); await h.run('refresh()');
  h.route(url => url === '/api/cache' ? {ok: false, json: async () => ({error: '缓存目录暂不可读'})} : undefined);
  await h.el('manage-cache').onclick();
  assert.equal(h.el('cache-confirm').disabled, true);
  assert.match(h.el('cache-error').textContent, /读取缓存失败.*缓存目录暂不可读/);
  h.route(url => url === '/api/cache' ? h.response({generation: {bytes: 0, files: 0}, map: {bytes: 0, files: 0}, busy: false}) : undefined);
  await h.el('cache-refresh').onclick();
  assert.equal(h.el('cache-error').hidden, true);
  assert.equal(h.el('cache-confirm').disabled, false);
});

for (const cancellationStatus of ['cancelling', 'cancelled']) test(`a late pause response cannot replace ${cancellationStatus}`, async () => {
  const h = harness(), running = {...complete('run'), status: 'running', progress: 42, files: ['log']};
  h.jobs([running]); await h.run('refresh()'); let finishPause, failRefresh = false;
  h.route(url => {
    if (url.endsWith('/pause')) return new Promise(resolve => { finishPause = resolve; });
    if (url.endsWith('/cancel')) { h.jobs([{...running, status: cancellationStatus}]); return h.response({}); }
    if (url === '/api/jobs' && failRefresh) return Promise.reject(Error('本地服务短暂重连'));
  });
  const pause = h.el('pause').onclick();
  await h.el('cancel').onclick();
  assert.equal(h.run('jobs[0].status'), cancellationStatus);
  failRefresh = true;
  finishPause(h.response({...running, status: 'pausing'})); await pause;
  assert.equal(h.run('jobs[0].status'), cancellationStatus);
  if (cancellationStatus === 'cancelled') {
    assert.equal(h.el('pause').hidden, true);
    assert.equal(h.el('generate').disabled, false);
  } else {
    assert.equal(h.el('pause').disabled, true);
    assert.equal(h.el('cancel').disabled, true);
  }
});

test('a late resume response cannot replace a newer completed poll', async () => {
  const h = harness(), paused = {...complete('run'), status: 'paused', progress: 98, files: ['log']};
  h.jobs([paused]); await h.run('refresh()'); let finishResume, failRefresh = false;
  h.route(url => {
    if (url.endsWith('/resume')) return new Promise(resolve => { finishResume = resolve; });
    if (url === '/api/jobs' && failRefresh) return Promise.reject(Error('本地服务短暂重连'));
  });
  const resumption = h.el('pause').onclick();
  h.jobs([complete('run')]); await h.run('refresh()');
  failRefresh = true;
  finishResume(h.response({...paused, status: 'resuming'})); await resumption;
  assert.equal(h.run('jobs[0].status'), 'complete');
  assert.equal(h.el('pause').hidden, true);
  assert.equal(h.el('percent').textContent, '100%');
});

test('completion while cache inventory is in flight refreshes a stale busy response', async () => {
  const h = harness(), running = {...complete('run'), status: 'paused'};
  h.jobs([running]); await h.run('refresh()'); let finishInfo, count = 0;
  const info = busy => ({generation: {bytes: 100, files: 1}, map: {bytes: 0, files: 0}, busy});
  h.route(url => {
    if (url !== '/api/cache') return;
    count++;
    return count === 1 ? new Promise(resolve => { finishInfo = resolve; }) : h.response(info(false));
  });
  const opening = h.el('manage-cache').onclick();
  h.jobs([complete('run')]); await h.run('refresh()');
  finishInfo(h.response(info(true))); await opening; await new Promise(setImmediate);
  assert.equal(count, 2);
  assert.equal(h.el('cache-busy').hidden, true);
  assert.equal(h.el('cache-confirm').disabled, false);
});

test('paused-to-running-to-complete transitions keep the latest pause controls and progress', async () => {
  const h = harness(), running = {...complete('run'), progress: 42, files: ['log']};
  for (const [status, expectedText, disabled] of [
    ['pausing', '撤销暂停', false], ['paused', '恢复生成', false],
    ['resuming', '正在恢复…', true], ['running', '暂停生成', false],
  ]) {
    h.jobs([{...running, status}]); await h.run('refresh()');
    assert.equal(h.el('pause').textContent, expectedText);
    assert.equal(h.el('pause').disabled, disabled);
    assert.equal(h.el('percent').textContent, '42%');
    assert.equal(h.el('generate').disabled, true);
  }
  h.jobs([complete('run')]); await h.run('refresh()');
  assert.equal(h.el('pause').hidden, true);
  assert.equal(h.el('cancel').hidden, true);
  assert.equal(h.el('generate').disabled, false);
});

test('cache clear is single-flight and prevents closing the dialog until the response', async () => {
  const h = harness(); await h.run('refresh()'); let finishClear;
  h.route(url => {
    if (url === '/api/cache') return h.response({generation: {bytes: 100, files: 1}, map: {bytes: 0, files: 0}, busy: false});
    if (url === '/api/cache/clear') return new Promise(resolve => { finishClear = resolve; });
  });
  await h.el('manage-cache').onclick();
  const clearing = h.el('cache-confirm').onclick();
  await h.el('cache-confirm').onclick(); await h.el('cache-refresh').onclick();
  h.el('cache-back').onclick(); let escaped = false;
  h.el('cache-dialog').dispatch('cancel', {preventDefault() { escaped = true; }});
  assert.equal(escaped, true); assert.equal(h.el('cache-dialog').open, true);
  assert.equal(h.el('cache-generation').disabled, true);
  assert.equal(h.el('cache-map').disabled, true);
  assert.equal(h.requests.filter(request => request.url === '/api/cache/clear').length, 1);
  assert.equal(h.requests.filter(request => request.url === '/api/cache').length, 1);
  finishClear(h.response({cleared_bytes: 100, cleared_files: 1})); await clearing;
  assert.equal(h.el('cache-dialog').open, false);
});

test('delete is single-flight and cannot be dismissed while cache clearing is pending', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()'); let finishDelete;
  h.route(url => url.endsWith('/delete') ? new Promise(resolve => { finishDelete = resolve; }) : undefined);
  h.run("openDelete('a')"); h.el('delete-cache').checked = true;
  const deletion = h.el('delete-confirm').onclick();
  await h.el('delete-confirm').onclick(); h.el('delete-back').onclick();
  let escaped = false; h.el('delete-dialog').dispatch('cancel', {preventDefault() { escaped = true; }});
  assert.equal(escaped, true); assert.equal(h.el('delete-dialog').open, true);
  assert.equal(h.el('delete-cache').disabled, true);
  assert.equal(h.requests.filter(request => request.url.endsWith('/delete')).length, 1);
  h.jobs([]); finishDelete(h.response({deleted: 'a'})); await deletion;
  assert.equal(h.el('delete-dialog').open, false);
});

test('opening a different delete dialog resets the shared-cache opt-in', async () => {
  const h = harness(); h.jobs([complete('a'), complete('b')]); await h.run('refresh()');
  h.run("openDelete('a')"); h.el('delete-cache').checked = true; h.el('delete-back').onclick();
  h.run("openDelete('b')");
  assert.equal(h.el('delete-cache').checked, false);
  assert.equal(h.el('delete-name').textContent, '项目 b');
});

test('a new task arriving while delete is open blocks only the optional shared-cache clear', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()');
  h.run("openDelete('a')"); h.el('delete-cache').checked = true; h.el('delete-cache').onchange();
  assert.equal(h.el('delete-confirm').disabled, false);
  h.jobs([{...complete('run'), status: 'running'}, complete('a')]); await h.run('refresh()');
  assert.equal(h.el('delete-confirm').disabled, true);
  await h.el('delete-confirm').onclick();
  assert.equal(h.requests.some(request => request.url.endsWith('/delete')), false);
  h.el('delete-cache').checked = false; h.el('delete-cache').onchange();
  assert.equal(h.el('delete-confirm').disabled, false);
});

test('an old history poll cannot resurrect a deleted project', async () => {
  const h = harness(); h.jobs([complete('a')]); await h.run('refresh()'); let finishPoll, polling = false;
  h.route(url => {
    if (url === '/api/jobs' && !polling) { polling = true; return new Promise(resolve => { finishPoll = resolve; }); }
    if (url.endsWith('/delete')) { h.jobs([]); return h.response({deleted: 'a'}); }
  });
  const oldPoll = h.run('refresh()');
  h.run("openDelete('a')"); await h.el('delete-confirm').onclick();
  finishPoll(h.response([complete('a')])); await oldPoll;
  assert.equal(h.run('jobs.length'), 0);
  assert.equal(h.run('selected'), null);
});

test('cancellation intent invalidates an old pause response before cancellation replies', async () => {
  const h = harness(), running = {...complete('run'), status: 'running', progress: 42};
  h.jobs([running]); await h.run('refresh()'); let finishPause, finishCancel;
  h.route(url => {
    if (url.endsWith('/pause')) return new Promise(resolve => { finishPause = resolve; });
    if (url.endsWith('/cancel')) return new Promise(resolve => { finishCancel = resolve; });
    if (url === '/api/jobs') return Promise.reject(Error('本地服务短暂重连'));
  });
  const pause = h.el('pause').onclick(), cancel = h.el('cancel').onclick();
  finishPause(h.response({...running, status: 'pausing'})); await pause;
  assert.equal(h.run('jobs[0].status'), 'running');
  assert.equal(h.el('pause').disabled, true);
  assert.equal(h.el('cancel').disabled, true);
  finishCancel(h.response({})); await cancel;
});

test('a newer progress poll survives a late pause response when follow-up polling fails', async () => {
  const h = harness(), running = {...complete('run'), status: 'running', progress: 42};
  h.jobs([running]); await h.run('refresh()'); let finishPause, failRefresh = false;
  h.route(url => {
    if (url.endsWith('/pause')) return new Promise(resolve => { finishPause = resolve; });
    if (url === '/api/jobs' && failRefresh) return Promise.reject(Error('本地服务短暂重连'));
  });
  const pause = h.el('pause').onclick();
  h.jobs([{...running, status: 'paused', progress: 45}]); await h.run('refresh()');
  failRefresh = true; finishPause(h.response({...running, status: 'pausing'})); await pause;
  assert.equal(h.run('jobs[0].status'), 'paused');
  assert.equal(h.el('percent').textContent, '45%');
  assert.equal(h.el('pause').textContent, '恢复生成');
});

test('cache busy clears automatically after the completed worker finishes cleanup', async () => {
  const h = harness(); h.jobs([complete('run')]); await h.run('refresh()');
  let busy = true, count = 0;
  h.route(url => {
    if (url === '/api/cache') { count++; return h.response({generation: {bytes: 100, files: 1}, map: {bytes: 0, files: 0}, busy}); }
  });
  await h.el('manage-cache').onclick();
  assert.equal(h.el('cache-confirm').disabled, true);
  busy = false; await h.run('refresh()'); await new Promise(setImmediate);
  assert.equal(count, 2); assert.equal(h.el('cache-confirm').disabled, false);
  await h.run('refresh()');
  assert.equal(count, 2); // Do not rescan caches on every idle poll.
});

test('a malformed inventory cannot enable destructive cache clearing', async () => {
  const h = harness(); await h.run('refresh()');
  h.route(url => url === '/api/cache' ? h.response({busy: false}) : undefined);
  await h.el('manage-cache').onclick();
  assert.equal(h.el('cache-confirm').disabled, true);
  assert.match(h.el('cache-error').textContent, /读取缓存失败/);
  await h.el('cache-confirm').onclick();
  assert.equal(h.requests.some(request => request.url.endsWith('/clear')), false);
});

test('cache preservation warnings remain visible without turning successful models into failures', async () => {
  const h = harness(), finished = {...complete('a'), stage: '生成完成',
    cache_warning: '成果已完成，部分下载缓存未保存：磁盘暂不可写',
    persistence_warning: '任务状态已保存到恢复文件'};
  h.jobs([finished]); await h.run('refresh()');
  h.route(url => url.endsWith('/geojson') ? h.response({features: []}) : undefined);
  h.run("selectProject('a')");
  assert.equal(h.el('stage').textContent, '生成完成 · 任务状态已保存到恢复文件 · 成果已完成，部分下载缓存未保存：磁盘暂不可写');
  assert.equal(h.el('percent').textContent, '100%');
  assert.equal(h.el('export').disabled, false);
  assert.match(h.el('task-title').textContent, /已完成/);
  h.jobs([{...finished, persistence_warning: undefined}]); await h.run('refresh()');
  assert.match(h.el('stage').textContent, /下载缓存未保存/);
  assert.doesNotMatch(h.el('stage').textContent, /恢复文件/);
  h.jobs([{...finished, persistence_warning: undefined, cache_warning: undefined}]); await h.run('refresh()');
  assert.equal(h.el('stage').textContent, '生成完成');
});
