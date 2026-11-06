/* Browser front camera. Capture starts only after a user's click and permission. */
(() => {
  const video = document.getElementById('webcam-video');
  const front = document.getElementById('img-front');
  const canvas = document.createElement('canvas');
  const start = document.getElementById('webcam-start');
  const stop = document.getElementById('webcam-stop');
  const state = document.getElementById('webcam-status');
  // The host camera feeds both the normal snapshot view and model observations.
  // Browsers must not replace it or ask for local webcam permission.
  let hostCamera = false;
  window.updateFrontCameraState = function(camera) {
      hostCamera = camera?.source === 'realsense_d435i';
      if (hostCamera) {
        start.disabled = true; stop.disabled = true;
        state.textContent = camera.available
          ? `主视角 Intel RealSense D435i · ${camera.width}×${camera.height} · ${camera.fps} fps · ${camera.depth_available ? 'RGB + 对齐深度' : 'RGB'}`
          : 'D435i 主视角暂不可用，正在等待相机恢复';
      }
  };
  const client = 'webcam-' + ((globalThis.crypto && crypto.randomUUID)
    ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2));
  const requestTimeoutMs = 3000;
  let stream = null, session = null, ownedToken = null, generation = 0, sequence = 0, frameHandle = null;
  let lastUpload = -Infinity, busyGeneration = null, backoff = false, failures = 0, callbacks = 0;

  async function jsonPost(path, body) {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: AbortSignal.timeout(requestTimeoutMs)});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || 'Camera request failed');
    return result;
  }

  // The local video is the preview; downloading the uploaded frame back would
  // double the tunnel traffic for the same picture.
  function showVideo(active) { video.hidden = !active; front.hidden = active; }

  function close(message = '前视 webcam 已关闭') {
    generation++;
    if (frameHandle !== null) video.cancelVideoFrameCallback(frameHandle);
    frameHandle = null;
    if (stream) stream.getTracks().forEach(track => track.stop());
    stream = null; video.srcObject = null; showVideo(false);
    const previousToken = ownedToken; session = null; ownedToken = null; busyGeneration = null;
    if (previousToken) fetch('/api/webcam/stop', {method: 'POST', keepalive: true,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: previousToken})}).catch(() => {});
    start.disabled = hostCamera; stop.disabled = true;
    if (!hostCamera) state.textContent = message;
  }

  // A slow or lost upload only makes the server-side front view stale. Keep the
  // camera open, drop the old ticket and continue with a NEW video frame.
  function retry(error) {
    session = null; backoff = true; failures++;
    const reason = error.name === 'TimeoutError' || error.name === 'AbortError' ? '相机请求超时' : error.message;
    state.textContent = `前视上传失败，正在用新帧重试（连续 ${failures} 次）：${reason}`;
  }

  async function renew(activeGeneration) {
    const renewed = await jsonPost('/api/webcam/start', {client});
    if (generation !== activeGeneration) {
      fetch('/api/webcam/stop', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: renewed.token})}).catch(() => {});
      return;
    }
    session = renewed; ownedToken = renewed.token; sequence = 0;
  }

  function next(activeGeneration) {
    if (generation !== activeGeneration || !stream || document.hidden) return;
    frameHandle = video.requestVideoFrameCallback((now, metadata) => {
      frameHandle = null;
      callbacks++;
      next(activeGeneration);
      if (generation !== activeGeneration) return;
      if (backoff) { backoff = false; lastUpload = now + 400; return; }
      if (busyGeneration === activeGeneration || now - lastUpload < 100) return;
      busyGeneration = activeGeneration; lastUpload = now;
      if (!session) {
        renew(activeGeneration).catch(error => { if (generation === activeGeneration) retry(error); })
          .finally(() => { if (busyGeneration === activeGeneration) busyGeneration = null; });
        return;
      }
      canvas.width = 640; canvas.height = 480;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, 640, 480);
      const scale = Math.min(640 / video.videoWidth, 480 / video.videoHeight);
      const w = video.videoWidth * scale, h = video.videoHeight * scale;
      // No mirror: the server and all models see this exact orientation.
      ctx.drawImage(video, (640 - w) / 2, (480 - h) / 2, w, h);
      const ticket = session;
      const frameSequence = ++sequence;
      // Some browsers repeat mediaTime for live camera streams; presentedFrames
      // (or one callback per presented frame) is what proves a new video frame.
      const frameId = Number.isFinite(metadata.presentedFrames) ? metadata.presentedFrames : callbacks;
      canvas.toBlob(async blob => {
        try {
          if (!blob || generation !== activeGeneration || document.hidden) return;
          const response = await fetch('/api/webcam/frame', {method: 'POST', body: blob,
            headers: {'Content-Type': 'image/jpeg', 'X-Webcam-Token': ticket.token,
              'X-Webcam-Ticket': ticket.ticket, 'X-Webcam-Sequence': String(frameSequence),
              'X-Webcam-Media-Time': String(metadata.mediaTime), 'X-Webcam-Frame': String(frameId)},
            // Response latency can exceed capture freshness. The server still
            // rejects any capture ticket older than 800 ms, independently.
            signal: AbortSignal.timeout(requestTimeoutMs)});
          const result = await response.json();
          if (generation !== activeGeneration) return;
          if (!response.ok || !result.ok) throw new Error(result.message || 'HTTP ' + response.status);
          session = result; failures = 0;
          state.textContent = '前视 webcam 已上传 · 640×480 · 约 10 fps';
        } catch (error) {
          if (generation === activeGeneration) retry(error);
        } finally { if (busyGeneration === activeGeneration) busyGeneration = null; }
      }, 'image/jpeg', .85);
    });
  }

  start.addEventListener('click', async () => {
    if (start.disabled || hostCamera) return;
    start.disabled = true; stop.disabled = false;
    const activeGeneration = ++generation;
    state.textContent = '等待浏览器相机授权…';
    let acquired = null;
    try {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia)
        throw new Error('请通过 localhost / 127.0.0.1 或 HTTPS 打开 GUMI');
      if (!video.requestVideoFrameCallback) throw new Error('请使用支持视频帧回调的新版浏览器');
      acquired = await navigator.mediaDevices.getUserMedia({audio: false,
        video: {width: {ideal: 640}, height: {ideal: 480}, frameRate: {ideal: 15, max: 15}}});
      if (generation !== activeGeneration || document.hidden) { acquired.getTracks().forEach(t => t.stop()); return; }
      stream = acquired; video.srcObject = stream; showVideo(true);
      stream.getVideoTracks()[0].addEventListener('ended', () => {
        if (generation === activeGeneration) close('前视相机已断开');
      });
      await video.play();
      if (generation !== activeGeneration || document.hidden) return;
      // Session creation uses the same recoverable path as later renewals.
      // A transient HTTP failure must not tear down an authorized camera.
      session = null; ownedToken = null; busyGeneration = null;
      sequence = 0; lastUpload = -Infinity; backoff = false; failures = 0;
      state.textContent = '前视预览已开启，正在连接推理相机…';
      next(activeGeneration);
    } catch (error) {
      if (acquired) acquired.getTracks().forEach(t => t.stop());
      if (generation === activeGeneration) close('前视相机未启用：' + error.message);
    }
  });
  stop.addEventListener('click', () => close());
  window.addEventListener('pagehide', () => close());
  document.addEventListener('visibilitychange', () => { if (document.hidden) close('页面隐藏，前视 webcam 已关闭'); });
})();
