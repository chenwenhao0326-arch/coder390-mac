/* Coder390 local model settings. Does not send data outside this origin. */
(() => {
  if (document.querySelector('coder390-settings')) return;
  const host = document.createElement('coder390-settings');
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `
    <style>
      :host{font:14px/1.6 system-ui,sans-serif;color:#e9eef7;position:fixed;right:18px;top:12px;z-index:2147483000;color-scheme:dark}
      button,input,select{font:inherit}button{cursor:pointer;border:1px solid #41516a;border-radius:9px;background:#1a2638;color:#edf3ff;padding:8px 12px}
      button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #7bb6ff;outline-offset:2px}button:disabled{opacity:.55;cursor:wait}
      #toggle{box-shadow:0 3px 18px #0007;font-size:13px;background:#151b25ee}#dot{color:#9ca9bc;margin-right:6px}
      dialog{width:min(480px,calc(100vw - 32px));max-height:88vh;box-sizing:border-box;overflow:auto;border:1px solid #42516b;border-radius:15px;background:#111a27;color:#e9eef7;padding:22px;box-shadow:0 16px 80px #0009}
      dialog::backdrop{background:#0009}header{display:flex;justify-content:space-between;align-items:center;gap:15px}h2{font-size:19px;margin:0}#close{padding:4px 10px}
      label{display:block;margin:17px 0 6px;font-weight:600}input[type=text],select{box-sizing:border-box;width:100%;border:1px solid #42516b;border-radius:8px;padding:9px;background:#0c131f;color:#e9eef7}input[type=checkbox]{width:17px;height:17px;vertical-align:middle;margin-right:8px}
      .small{color:#a5b5cd;font-size:12px;overflow-wrap:anywhere}.status{border:1px solid #34465e;border-radius:8px;padding:10px;margin-top:16px;background:#172234}#error{color:#ffbaae;white-space:pre-wrap;overflow-wrap:anywhere}#error:empty{display:none}footer{margin-top:20px;display:flex;gap:10px;align-items:center;justify-content:flex-end}#apply{background:#2f6399;border-color:#5898d5}#notice{font-size:13px;color:#b9d8fa}
      @media(max-width:600px){:host{right:8px;top:8px}#toggle{font-size:12px;padding:6px 9px}dialog{padding:17px}}
    </style>
    <button id="toggle" type="button" aria-label="模型设置"><span id="dot">●</span><span id="badge">模型设置</span></button>
    <dialog aria-labelledby="title">
      <header><h2 id="title">模型设置</h2><button id="close" type="button" aria-label="关闭模型设置">×</button></header>
      <p class="small" id="model"></p>
      <label for="context">上下文容量</label><select id="context"></select>
      <label for="vision"><input id="vision" type="checkbox">启用看图</label>
      <label for="projector">视觉投影文件（mmproj）路径</label>
      <input id="projector" type="text" autocomplete="off" spellcheck="false" placeholder="填写这台 Mac 上的 .gguf 绝对路径">
      <p class="small" id="hint"></p>
      <div class="status" role="status" aria-live="polite" id="status">正在读取模型状态…</div>
      <p id="error" role="alert"></p>
      <p class="small">应用会重启模型并刷新页面。聊天记录保留，未发送的文字会恢复；未发送的附件请先处理。较小窗口可能需要新建对话。</p>
      <footer><span id="notice" aria-live="polite"></span><button id="apply" type="button">应用并重启</button></footer>
    </dialog>`;
  document.body.append(host);
  const $ = id => shadow.getElementById(id);
  const dialog = shadow.querySelector('dialog');
  let state, formToken, formVersion, clientError = '', busy = false, opening = false;
  const labels = { loading: '模型加载中', restarting: '正在重启模型', ready: '模型已就绪', error: '模型需要处理' };
  const draftKey = () => `coder390.pendingDraft:${location.pathname}${location.hash}`;

  function rememberDraft() {
    const editor = document.querySelector('textarea');
    if (editor?.value) sessionStorage.setItem(draftKey(), JSON.stringify({ value: editor.value, time: Date.now() }));
  }
  function restoreDraft() {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(draftKey()) || 'null'); } catch { return; }
    if (!saved || typeof saved.value !== 'string' || Date.now() - saved.time > 300000) return;
    const attempt = () => {
      const editor = document.querySelector('textarea');
      if (!editor) return false;
      if (!editor.value) {
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
        setter.call(editor, saved.value);
        editor.dispatchEvent(new Event('input', { bubbles: true }));
      }
      sessionStorage.removeItem(draftKey());
      return true;
    };
    if (!attempt()) {
      const observer = new MutationObserver(() => { if (attempt()) observer.disconnect(); });
      observer.observe(document.body, { childList: true, subtree: true });
      setTimeout(() => observer.disconnect(), 30000);
    }
  }
  restoreDraft();

  async function getState() {
    const response = await fetch('/_coder390/status', { cache: 'no-store', credentials: 'same-origin' });
    if (!response.ok) throw new Error(`读取设置失败（${response.status}）`);
    state = await response.json();
    paintStatus();
    return state;
  }
  function paintStatus() {
    const ready = state.phase === 'ready';
    $('dot').style.color = ready ? '#72d1a8' : state.phase === 'error' ? '#ff9c8a' : '#f0c879';
    $('badge').textContent = ready ? `模型设置 · ${state.settings.context / 1024}K · ${state.settings.vision_enabled ? '看图开启' : '文字'}` : `模型设置 · ${labels[state.phase] || state.phase}`;
    $('status').textContent = `${labels[state.phase] || state.phase} · ${state.settings.context / 1024}K 上下文 · ${state.settings.vision_enabled ? '看图开启' : '看图关闭'}${state.active_requests ? ' · 有请求正在处理' : ''}`;
    $('model').textContent = `${state.model_name} · ${state.profile} · 本机 ${Math.round(state.memory_gib)}GB`;
    $('hint').textContent = `使用 ${state.projector_filename}，不会下载或复制权重。图片处理上限 ${state.image_max_tokens} token。`;
    $('error').textContent = state.error || clientError;
    $('apply').disabled = busy || state.phase === 'loading' || state.phase === 'restarting' || state.active_requests > 0;
  }
  function fillForm() {
    formVersion = state.version;
    formToken = state.token;
    $('context').replaceChildren(...state.context_options.map(value => {
      const option = document.createElement('option');
      option.value = String(value); option.textContent = `${value / 1024}K（${value.toLocaleString()} token）`;
      return option;
    }));
    $('context').value = String(state.settings.context);
    $('vision').checked = state.settings.vision_enabled;
    $('projector').value = state.settings.mmproj_path;
  }
  $('toggle').addEventListener('click', async () => {
    if (opening) return;
    opening = true;
    clientError = '';
    $('notice').textContent = '';
    try { await getState(); fillForm(); dialog.showModal(); }
    catch (error) { clientError = error.message; $('error').textContent = clientError; dialog.showModal(); }
    finally { opening = false; }
  });
  $('close').addEventListener('click', () => dialog.close());
  $('apply').addEventListener('click', async () => {
    if (!state || busy) return;
    clientError = '';
    busy = true; paintStatus();
    for (const id of ['context', 'vision', 'projector']) $(id).disabled = true;
    $('notice').textContent = '正在校验并应用…';
    try {
      const response = await fetch('/_coder390/apply', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Coder390-Token': formToken },
        body: JSON.stringify({ version: formVersion, settings: {
          context: Number($('context').value), vision_enabled: $('vision').checked,
          mmproj_path: $('projector').value.trim()
        } })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `应用失败（${response.status}）`);
      if (!result.changed) { $('notice').textContent = '已是当前配置'; return; }
      $('notice').textContent = '模型重启中，请稍候…';
      const deadline = Date.now() + 420000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        await getState();
        if (state.operation !== result.operation) throw new Error('设置已被其他页面改变，请重新打开面板');
        if (state.phase === 'error' || (state.phase === 'ready' && state.error)) throw new Error(state.error);
        if (state.phase === 'ready') {
          rememberDraft();
          $('notice').textContent = '已生效，正在刷新…';
          location.reload();
          return;
        }
      }
      throw new Error('等待重启超时，请查看终端和模型状态');
    } catch (error) { clientError = error.message; $('error').textContent = clientError; $('notice').textContent = '未完成'; }
    finally {
      busy = false;
      for (const id of ['context', 'vision', 'projector']) $(id).disabled = false;
      $('apply').disabled = false;
    }
  });
  getState().catch(() => { $('badge').textContent = '模型设置 · 未连接'; });
  setInterval(() => { if (!busy) getState().catch(() => {}); }, 3000);
})();
