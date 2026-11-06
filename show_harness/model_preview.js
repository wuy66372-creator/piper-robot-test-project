(() => {
  const element = id => document.getElementById(id);
  const form = element('model-preview'), result = element('model-result');
  const client = 'model-' + ((globalThis.crypto && crypto.randomUUID)
    ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2));
  let runId = null, starting = false, previewing = false, heartbeatBusy = false;
  let generation = 0, startPending = false, stopsPending = 0;
  let displayedRunId = null;
  let serviceLoading = false, switchPending = false;
  const retiredRuns = new Set();
  const post = async (path, body, timeout = 5000) => {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: AbortSignal.timeout(timeout)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || '请求失败');
    return data;
  };
  const input = () => ({task: element('model-task').value, arm: element('model-arm').value,
    profile: element('model-profile').value, client});
  window.renderModelState = (state, startResponse = false) => {
    if (!state) return;
    if (startPending && !startResponse) return;
    if (retiredRuns.has(state.run_id)) return;
    if (runId && state.run_id !== runId) return;
    const busy = serviceLoading || switchPending || startPending || stopsPending || previewing || state.active || state.thread_pending;
    for (const id of ['model-infer', 'model-step', 'model-start', 'model-task', 'model-arm', 'model-profile', 'model-steps', 'model-load'])
      element(id).disabled = !!busy;
    if (!state.active && !starting) runId = null;
    if (state.run_id && !previewing) {
      displayedRunId = state.run_id;
      const action = state.raw_token ? ' · 模型：' + state.raw_token + (state.execution_token ? ' → 执行：' + state.execution_token : '') : '';
      result.textContent = `${state.arm} · ${state.steps}/${state.max_steps} 步 · ${state.message}${action}`;
    }
  };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (serviceLoading || switchPending || previewing || startPending || stopsPending || runId) return;
    previewing = true; element('model-infer').disabled = true;
    result.textContent = '正在读取前视和腕部图像并推理…';
    try {
      const data = await post('/api/model/preview', input(), 125000);
      result.textContent = `${data.arm} · 模型：${data.token} → 执行：${data.execution_token || data.token} · ${data.elapsed_s.toFixed(2)} 秒 · 仅预览，未执行`;
    } catch (error) { result.textContent = '未完成推理：' + error.message; }
    finally { previewing = false; element('model-infer').disabled = false; }
  });
  const start = async count => {
    if (serviceLoading || switchPending || startPending || stopsPending || previewing || runId) return;
    const ownGeneration = ++generation;
    if (displayedRunId) retiredRuns.add(displayedRunId);
    startPending = starting = true;
    result.textContent = `${element('model-arm').value} · 0/${count} 步 · 正在启动模型实机推理…`;
    try {
      const data = await post('/api/model/start', {...input(), max_steps: count}, 15000);
      if (ownGeneration !== generation || document.hidden || !starting) { stop(); return; }
      runId = data.run_id;
      window.renderModelState(data.model_run, true);
    } catch (error) { if (ownGeneration === generation) result.textContent = '未启动：' + error.message; }
    finally { startPending = false; if (ownGeneration === generation) starting = false; }
  };
  const stop = () => {
    const ownGeneration = ++generation;
    const previousRun = runId;
    stopsPending++;
    runId = null; starting = false;
    return fetch('/api/model/stop', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(previousRun ? {run_id: previousRun} : {}), keepalive: true,
      signal: AbortSignal.timeout(5000)}).then(r => r.json()).then(data => {
        if (ownGeneration === generation) result.textContent = data.message || '模型已停止';
      }).catch(() => { if (ownGeneration === generation) result.textContent = '连接中断；运行将因心跳超时停止'; })
      .finally(() => { stopsPending--; });
  };
  window.renderModelService = state => {
    if (!state) return;
    serviceLoading = state.phase === 'loading';
    const names = {qwen3_5_0_8b: '0.8B', qwen3_5_2b: '2B', qwen3_5_4b: '4B'};
    element('model-service').textContent = `${state.phase === 'ready' ? '当前后台' : '后台'}：${state.name || window.gumiModelNames?.[state.profile] || names[state.profile] || state.profile || '未加载'} · ${state.message}`;
    if (serviceLoading || switchPending) {
      for (const id of ['model-infer', 'model-step', 'model-start', 'model-profile', 'model-load'])
        element(id).disabled = true;
    }
  };
  element('model-load').addEventListener('click', async () => {
    if (serviceLoading || switchPending || startPending || stopsPending || previewing || runId) return;
    switchPending = true;
    element('model-service').textContent = '正在请求切换后台模型…';
    try {
      const data = await post('/api/model/switch', {profile: element('model-profile').value});
      window.renderModelService(data.model_service);
    } catch (error) { element('model-service').textContent = '未切换：' + error.message; }
    finally { switchPending = false; }
  });
  element('model-step').addEventListener('click', () => start(1));
  element('model-start').addEventListener('click', () => start(Number(element('model-steps').value)));
  element('model-stop').addEventListener('click', stop);
  setInterval(async () => {
    if (!runId || heartbeatBusy) return;
    const ownGeneration = generation, ownRun = runId;
    heartbeatBusy = true;
    try { await post('/api/model/heartbeat', {run_id: ownRun, client}, 1000); }
    catch (error) { if (ownGeneration === generation && runId === ownRun) stop(); }
    finally { heartbeatBusy = false; }
  }, 500);
  const leave = () => { if (runId || starting) stop(); };
  window.addEventListener('pagehide', leave);
  document.addEventListener('visibilitychange', () => { if (document.hidden) leave(); });
})();
