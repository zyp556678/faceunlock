/* faceunlock 管理界面前端（纯 vanilla JS，无打包器、无 CDN）。
 *
 * 与后端唯一的契约是 docs/GUI_API.md：
 *   - 所有请求带 X-FaceUnlock-Token（由 Rust 侧 initialization_script 注入）
 *   - 统一错误体 {ok:false, error, code}，code 一律翻成中文提示
 *   - 轮询接口在会话不存在时返回 {ok:true, state:"idle"}
 */
'use strict';

(function () {
  // ---------------------------------------------------------------- 基础设施
  const FU = window.__FACEUNLOCK__ || null;
  const BASE = FU && FU.port ? ('http://127.0.0.1:' + FU.port) : null;
  const TOKEN = FU && FU.token ? FU.token : '';
  const POLL_MS = 150;

  const ERROR_TEXT = {
    AUTH_REQUIRED: '需要管理员授权：系统密码框被取消或授权失败，请重试并在弹窗中输入密码',
    BUSY: '摄像头被其它程序占用，请关闭视频会议/浏览器后重试',
    NO_FACE: '没有检测到人脸，请正对摄像头',
    NOT_ENROLLED: '该用户还没有录入人脸，请先在「人脸管理」中添加',
    INVALID: '请求参数不合法（详见错误信息）',
    INTERNAL: '后端内部错误（详见错误信息）',
    NETWORK: '无法连接本地后端服务（faceunlock-gui-bridge 可能已退出）',
  };

  class ApiError extends Error {
    constructor(code, message) {
      super(message || ERROR_TEXT[code] || code);
      this.code = code || 'INTERNAL';
    }
    get friendly() {
      const base = ERROR_TEXT[this.code];
      return base ? base + (this.message ? '：' + this.message : '') : this.message;
    }
  }

  const $ = (sel) => document.querySelector(sel);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function api(path, opts) {
    opts = opts || {};
    if (!BASE) throw new ApiError('NETWORK', '未拿到后端端口');
    let resp;
    try {
      resp = await fetch(BASE + path, {
        method: opts.method || 'GET',
        headers: Object.assign({ 'X-FaceUnlock-Token': TOKEN },
          opts.body ? { 'Content-Type': 'application/json' } : {}),
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (e) {
      throw new ApiError('NETWORK', String(e && e.message ? e.message : e));
    }
    let data;
    try {
      data = await resp.json();
    } catch (e) {
      throw new ApiError('INTERNAL', 'HTTP ' + resp.status + ' 返回了非 JSON 内容');
    }
    if (!data || data.ok !== true) {
      throw new ApiError(data && data.code, data && data.error);
    }
    return data;
  }

  // ---------------------------------------------------------------- Toast / 模态框
  function toast(msg, kind) {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    $('#toast-root').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4200);
  }

  function openModal(html, opts) {
    opts = opts || {};
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML = '<div class="modal ' + (opts.narrow ? 'narrow' : '') + '">' + html + '</div>';
    $('#modal-root').appendChild(mask);
    if (!opts.persistent) {
      mask.addEventListener('click', (e) => { if (e.target === mask) closeModal(mask); });
    }
    const x = mask.querySelector('.close-x');
    if (x) x.addEventListener('click', () => closeModal(mask));
    return mask;
  }
  function closeModal(mask) {
    if (mask && mask.parentNode) mask.parentNode.removeChild(mask);
  }

  /** 二次确认（破坏性操作统一走这里） */
  function confirmDialog(title, text, confirmLabel, danger) {
    return new Promise((resolve) => {
      const mask = openModal(
        '<div class="modal-head"><h3>' + esc(title) + '</h3><button class="close-x">✕</button></div>' +
        '<div class="modal-body"><p>' + text + '</p></div>' +
        '<div class="modal-foot">' +
        '<button class="btn ghost" data-act="no">取消</button>' +
        '<button class="btn ' + (danger ? 'danger' : 'primary') + '" data-act="yes">' +
        esc(confirmLabel || '确定') + '</button></div>', { narrow: true, persistent: true });
      mask.querySelector('[data-act="no"]').addEventListener('click', () => { closeModal(mask); resolve(false); });
      mask.querySelector('.close-x').addEventListener('click', () => { closeModal(mask); resolve(false); });
      mask.querySelector('[data-act="yes"]').addEventListener('click', () => { closeModal(mask); resolve(true); });
    });
  }

  /** 文本输入弹窗（Tauri 下 window.prompt 不可靠，自己实现） */
  function promptDialog(title, label, value) {
    return new Promise((resolve) => {
      const mask = openModal(
        '<div class="modal-head"><h3>' + esc(title) + '</h3><button class="close-x">✕</button></div>' +
        '<div class="modal-body"><label class="field" style="display:block">' + esc(label) +
        '<input type="text" id="prompt-input" style="width:100%;margin-top:8px" value="' +
        esc(value || '') + '"></label></div>' +
        '<div class="modal-foot"><button class="btn ghost" data-act="no">取消</button>' +
        '<button class="btn primary" data-act="yes">确定</button></div>', { narrow: true, persistent: true });
      const input = mask.querySelector('#prompt-input');
      input.focus();
      input.select();
      const done = (v) => { closeModal(mask); resolve(v); };
      mask.querySelector('[data-act="no"]').addEventListener('click', () => done(null));
      mask.querySelector('.close-x').addEventListener('click', () => done(null));
      mask.querySelector('[data-act="yes"]').addEventListener('click', () => done(input.value.trim()));
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') done(input.value.trim());
        if (e.key === 'Escape') done(null);
      });
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function fmtTime(sec) {
    if (!sec) return '–';
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function drawFrame(canvas, b64, box) {
    if (!canvas || !b64) return;
    const img = new Image();
    img.onload = function () {
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      if (box && box.length === 4) {
        ctx.lineWidth = Math.max(2, canvas.width / 320);
        ctx.strokeStyle = '#22c55e';
        ctx.strokeRect(box[0], box[1], box[2], box[3]);
        ctx.font = Math.round(canvas.width / 42) + 'px sans-serif';
        ctx.fillStyle = '#22c55e';
        ctx.fillText('人脸 ' + box[2] + '×' + box[3], box[0], Math.max(16, box[1] - 8));
      }
    };
    img.src = 'data:image/jpeg;base64,' + b64;
  }

  // ---------------------------------------------------------------- 全局状态
  const S = {
    tab: 'faces',
    state: null,
    user: null,
    faces: [],
    enroll: null,   // {mask, active, session}
    verify: null,   // {active, timer}
    preview: false,
  };

  // ---------------------------------------------------------------- 标签页
  function switchTab(name) {
    S.tab = name;
    document.querySelectorAll('.nav-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === name);
    });
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('active', t.id === 'tab-' + name);
    });
    if (name === 'faces') loadFaces();
    if (name === 'verify') prepareVerify();
    if (name === 'settings') { renderSettings(); loadDoctor(); }
  }

  // ---------------------------------------------------------------- 状态 / 人脸列表
  async function loadState() {
    const st = await api('/api/state');
    S.state = st;
    $('#meta-user').textContent = st.current_user || '–';
    $('#meta-version').textContent = st.version || '–';
    const users = (st.users && st.users.length) ? st.users.slice() : [];
    if (!users.some((u) => u.user === st.current_user)) {
      users.unshift({ user: st.current_user, uid: null, face_count: null });
    }
    S.users = users;
    if (!S.user) S.user = st.current_user;
    const sel = $('#user-select');
    sel.innerHTML = '';
    const list = st.can_manage_others ? users : users.filter((u) => u.user === st.current_user);
    (list.length ? list : [{ user: st.current_user }]).forEach((u) => {
      const o = document.createElement('option');
      o.value = u.user;
      o.textContent = u.user + (u.face_count == null ? '' : '（' + u.face_count + ' 张人脸）');
      sel.appendChild(o);
    });
    sel.value = S.user;
    sel.disabled = !st.can_manage_others;
    setBackend(st.privileged ? 'ok' : 'warn',
      st.privileged ? '后端就绪（已授权）' : '后端就绪（未授权特权操作）');
    return st;
  }

  function setBackend(kind, text) {
    const el = $('#backend-state');
    el.className = 'pill ' + kind;
    el.textContent = text;
  }

  async function loadFaces() {
    const user = S.user || (S.state && S.state.current_user);
    if (!user) return;
    try {
      const r = await api('/api/faces?user=' + encodeURIComponent(user));
      S.faces = r.faces || [];
      $('#auth-banner').classList.add('hidden');
      renderFaces();
    } catch (e) {
      S.faces = [];
      renderFaces();
      if (e.code === 'AUTH_REQUIRED') {
        $('#auth-banner').classList.remove('hidden');
      } else {
        toast(e.friendly, 'err');
      }
    }
  }

  function renderFaces() {
    const grid = $('#faces-grid');
    grid.innerHTML = '';
    $('#faces-empty').classList.toggle('hidden', S.faces.length > 0);
    S.faces.forEach((f) => {
      const q = f.quality || {};
      const card = document.createElement('div');
      card.className = 'face-card';
      const thumb = f.thumb
        ? '<img class="face-thumb" src="data:image/jpeg;base64,' + f.thumb + '" alt="人脸缩略图">'
        : '<div class="face-thumb placeholder">☺</div>';
      card.innerHTML = thumb +
        '<div class="face-body">' +
        '<p class="face-label">' + esc(f.label || '未命名') + '</p>' +
        '<div class="face-meta">创建于 ' + fmtTime(f.created) + '</div>' +
        '<div class="face-meta">质量：高度 ' + (q.height != null ? Math.round(q.height) : '–') +
        'px · 清晰度 ' + (q.sharpness != null ? Math.round(q.sharpness) : '–') +
        ' · 亮度 ' + (q.brightness != null ? Math.round(q.brightness) : '–') + '</div>' +
        '<div class="face-actions">' +
        '<button class="btn small ghost" data-act="rename">重命名</button>' +
        '<button class="btn small danger" data-act="delete">删除</button>' +
        '</div></div>';
      card.querySelector('[data-act="rename"]').addEventListener('click', () => renameFace(f));
      card.querySelector('[data-act="delete"]').addEventListener('click', () => deleteFace(f));
      grid.appendChild(card);
    });
  }

  async function renameFace(f) {
    const label = await promptDialog('重命名人脸', '新名称（最多 64 字符）', f.label);
    if (label == null || label === '') return;
    try {
      await api('/api/face/rename', { method: 'POST', body: { user: S.user, id: f.id, label: label } });
      toast('已重命名为「' + label + '」', 'ok');
      loadFaces();
    } catch (e) { toast(e.friendly, 'err'); }
  }

  async function deleteFace(f) {
    const ok = await confirmDialog('删除人脸',
      '确定删除「' + esc(f.label || '未命名') + '」？该模板将不再能用于登录，此操作不可撤销。',
      '删除', true);
    if (!ok) return;
    try {
      await api('/api/face/delete', { method: 'POST', body: { user: S.user, id: f.id } });
      toast('已删除', 'ok');
      loadFaces();
    } catch (e) { toast(e.friendly, 'err'); }
  }

  // ---------------------------------------------------------------- 录入对话框
  function openEnrollDialog() {
    const mask = openModal(
      '<div class="modal-head"><h3>添加人脸</h3><button class="close-x">✕</button></div>' +
      '<div class="modal-body"><div class="enroll-grid">' +
      '<div><div class="enroll-canvas-wrap"><canvas id="enroll-canvas" width="640" height="360"></canvas></div>' +
      '<div class="progress-track"><div class="progress-fill" id="enroll-progress"></div></div>' +
      '<div class="kv"><span id="enroll-count">已采集 0/5</span><span id="enroll-user"></span></div></div>' +
      '<div><label class="field" style="display:block">名称<input type="text" id="enroll-label" ' +
      'style="width:100%;margin-top:6px" value="正面"></label>' +
      '<div class="hint-big" id="enroll-hint">准备中…</div>' +
      '<p class="muted small" id="enroll-message">正在打开摄像头…</p>' +
      '<div class="rows" style="margin-top:10px"><label class="row"><span>采集张数</span>' +
      '<input type="number" id="enroll-count-input" min="1" max="20" value="5"></label></div>' +
      '<div class="note" id="enroll-note">请正对摄像头，按提示缓慢转动头部；每张合格样本都会有提示音似的提示语变化。</div>' +
      '</div></div></div>' +
      '<div class="modal-foot"><button class="btn ghost" id="enroll-cancel">取消</button>' +
      '<button class="btn primary" id="enroll-start">开始采集</button></div>',
      { persistent: true });

    const session = { mask: mask, active: false, started: false };
    S.enroll = session;
    mask.querySelector('#enroll-user').textContent = '用户：' + S.user;
    stopPreviewImg();

    mask.querySelector('.close-x').addEventListener('click', () => cancelEnroll());
    mask.querySelector('#enroll-cancel').addEventListener('click', () => cancelEnroll());
    mask.querySelector('#enroll-start').addEventListener('click', () => startEnroll(session));
    return session;
  }

  async function startEnroll(session) {
    const label = ($('#enroll-label').value || '未命名').trim();
    const count = Math.max(1, Math.min(20, parseInt($('#enroll-count-input').value, 10) || 5));
    const note = $('#enroll-note');
    note.className = 'note';
    note.textContent = '正在打开摄像头…';
    $('#enroll-start').disabled = true;
    try {
      await api('/api/enroll/start', { method: 'POST', body: { user: S.user, label: label, count: count } });
    } catch (e) {
      note.className = 'note err';
      note.textContent = e.friendly;
      $('#enroll-start').disabled = false;
      return;
    }
    session.active = true;
    session.started = true;
    $('#enroll-start').disabled = true;
    $('#enroll-start').textContent = '采集中…';
    enrollLoop(session);
  }

  async function enrollLoop(session) {
    while (session.active && S.enroll === session) {
      let r;
      try {
        r = await api('/api/enroll/poll');
      } catch (e) {
        session.active = false;
        showEnrollError(session, e);
        return;
      }
      if (r.state === 'idle') { session.active = false; return; }
      const s = r.session || {};
      drawFrame($('#enroll-canvas'), s.frame, s.box);
      const need = s.required || 1;
      $('#enroll-progress').style.width = Math.round(100 * (s.captured || 0) / need) + '%';
      $('#enroll-count').textContent = '已采集 ' + (s.captured || 0) + '/' + need;
      $('#enroll-hint').textContent = s.hint || s.message || '';
      $('#enroll-message').textContent = s.message || '';
      if (r.state === 'done') {
        session.active = false;
        await commitEnroll(session);
        return;
      }
      await sleep(POLL_MS);
    }
  }

  async function commitEnroll(session) {
    const note = $('#enroll-note');
    note.className = 'note warn';
    note.innerHTML = '采集完成，正在保存…<br><b>即将弹出系统密码框用于保存，请输入管理员密码。</b>';
    $('#enroll-hint').textContent = '正在保存';
    try {
      const r = await api('/api/enroll/commit', { method: 'POST' });
      toast('已保存 ' + r.added + ' 张人脸模板', 'ok');
      closeModal(session.mask);
      S.enroll = null;
      await loadState().catch(() => {});
      loadFaces();
    } catch (e) {
      showEnrollError(session, e, true);
    }
  }

  function showEnrollError(session, e, canRetry) {
    const note = $('#enroll-note');
    note.className = 'note err';
    note.textContent = e.friendly;
    $('#enroll-hint').textContent = '未完成';
    const btn = $('#enroll-start');
    btn.disabled = false;
    if (canRetry && e.code === 'AUTH_REQUIRED') {
      btn.textContent = '重新保存（再次授权）';
      btn.onclick = () => { btn.onclick = null; btn.textContent = '采集中…'; btn.disabled = true; commitEnroll(session); };
    } else {
      btn.textContent = '重新开始';
      btn.onclick = null;
    }
    if (e.code === 'AUTH_REQUIRED') $('#auth-banner').classList.remove('hidden');
  }

  async function cancelEnroll() {
    const session = S.enroll;
    if (!session) return;
    session.active = false;
    S.enroll = null;
    closeModal(session.mask);
    if (session.started) {
      try { await api('/api/enroll/cancel', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    }
    $('#enroll-start') && ($('#enroll-start').disabled = false);
  }

  // ---------------------------------------------------------------- 识别测试
  function prepareVerify() {
    const thr = (S.state && S.state.config && S.state.config.threshold) || 0.5;
    $('#score-mark').style.left = Math.round(thr * 100) + '%';
    $('#score-thr').textContent = '阈值 ' + thr.toFixed(2);
    return loadFaces().then(() => {
      if (!S.faces.length) {
        $('#verify-msg').textContent = '该用户还没有录入人脸：请先到「人脸管理」添加，再回来测试。';
      }
    }).catch(() => {});
  }

  async function startVerify() {
    if (!S.faces.length) {
      toast('还没有录入人脸，请先到「人脸管理」添加', 'warn');
      switchTab('faces');
      return;
    }
    $('#verify-msg').textContent = '正在打开摄像头…';
    try {
      await api('/api/verify/start', { method: 'POST', body: { user: S.user } });
    } catch (e) {
      $('#verify-msg').textContent = e.friendly;
      toast(e.friendly, 'err');
      return;
    }
    $('#btn-verify-start').disabled = true;
    $('#btn-verify-stop').disabled = false;
    $('#verify-idle').classList.add('hidden');
    S.verify = { active: true };
    verifyLoop();
  }

  async function verifyLoop() {
    while (S.verify && S.verify.active) {
      let r;
      try {
        r = await api('/api/verify/poll');
      } catch (e) {
        $('#verify-msg').textContent = e.friendly;
        toast(e.friendly, 'err');
        stopVerify(true);
        return;
      }
      if (r.state !== 'running') { stopVerify(true); return; }
      drawFrame($('#verify-canvas'), r.frame, r.box);
      const thr = r.threshold != null ? r.threshold : 0.5;
      $('#score-mark').style.left = Math.round(thr * 100) + '%';
      $('#score-thr').textContent = '阈值 ' + thr.toFixed(2);
      if (r.score == null) {
        $('#score-value').textContent = '–';
        $('#score-fill').style.width = '0%';
        $('#verify-badge').className = 'badge run';
        $('#verify-badge').textContent = '未检测到人脸';
        $('#verify-msg').textContent = r.message || '请正对摄像头';
      } else {
        // 分数可能是负值，映射到 0~1 显示；阈值参考线用同一映射
        const shown = Math.max(0, Math.min(1, r.score));
        $('#score-value').textContent = r.score.toFixed(3);
        $('#score-fill').style.width = Math.round(shown * 100) + '%';
        $('#score-fill').classList.toggle('low', !r.passed);
        $('#verify-badge').className = 'badge ' + (r.passed ? 'pass' : 'fail');
        $('#verify-badge').textContent = r.passed ? '通过' : '不通过';
        $('#verify-msg').textContent = r.message || '';
      }
      await sleep(POLL_MS + 50);
    }
  }

  async function stopVerify(silent) {
    if (S.verify) S.verify.active = false;
    S.verify = null;
    $('#btn-verify-start').disabled = false;
    $('#btn-verify-stop').disabled = true;
    $('#verify-idle').classList.remove('hidden');
    $('#verify-badge').className = 'badge idle';
    $('#verify-badge').textContent = '已停止';
    try { await api('/api/verify/stop', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    if (!silent) $('#verify-msg').textContent = '已停止';
  }

  // ---------------------------------------------------------------- 设置
  const SERVICES = [
    ['gdm-password', '登录 / 锁屏', 'GDM 登录界面与 GNOME 锁屏'],
    ['sudo', 'sudo', '终端 sudo 提权'],
    ['sudo-i', 'sudo -i', '交互式 root shell'],
    ['su', 'su', '切换用户'],
    ['polkit-1', 'pkexec / 管理员弹窗', '图形界面提权授权框'],
    ['login', '控制台登录', 'tty 登录（默认关闭，风险较高）'],
  ];

  function renderSettings() {
    const cfg = (S.state && S.state.config) || {};
    $('#cfg-enabled').checked = !!cfg.enabled;
    const thr = typeof cfg.threshold === 'number' ? cfg.threshold : 0.5;
    $('#cfg-threshold').value = thr;
    $('#threshold-out').textContent = thr.toFixed(2);
    $('#cfg-window').value = cfg.window_frames != null ? cfg.window_frames : 3;
    $('#cfg-required').value = cfg.required_frames != null ? cfg.required_frames : 2;
    $('#cfg-timeout').value = cfg.timeout_ms != null ? cfg.timeout_ms : 4000;
    $('#cfg-no-face').value = cfg.no_face_timeout_ms != null ? cfg.no_face_timeout_ms : 1000;

    const box = $('#services-box');
    box.innerHTML = '';
    SERVICES.forEach(([key, label, desc]) => {
      const on = !!(cfg.services && cfg.services[key]);
      const wrap = document.createElement('label');
      wrap.className = 'svc';
      wrap.innerHTML = '<input type="checkbox" data-svc="' + key + '"' + (on ? ' checked' : '') + '>' +
        '<span>' + esc(label) + '<small>' + esc(desc) + '</small></span>';
      wrap.querySelector('input').addEventListener('change', (e) => {
        const patch = { services: {} };
        patch.services[key] = e.target.checked;
        saveConfig(patch, label + (e.target.checked ? ' 已启用' : ' 已停用'));
      });
      box.appendChild(wrap);
    });

    const pam = (S.state && S.state.pam) || {};
    $('#pam-box').innerHTML =
      '<div class="row"><span>profile 已安装</span><b>' + (pam.profile_installed ? '是' : '否') + '</b></div>' +
      '<div class="row"><span>已接入 PAM 栈</span><b>' + (pam.enabled ? '是' : '否') + '</b></div>' +
      '<div class="row"><span>配置文件</span><span class="muted">' + esc(pam.file || '–') + '</span></div>';
  }

  async function saveConfig(patch, okMsg) {
    try {
      const r = await api('/api/config', { method: 'POST', body: patch });
      if (S.state) {
        S.state.config = r.config || S.state.config;
      }
      renderSettings();
      $('#auth-banner').classList.add('hidden');
      if (okMsg) toast(okMsg, 'ok');
      return true;
    } catch (e) {
      toast(e.friendly, 'err');
      renderSettings();   // 回滚界面上的开关
      if (e.code === 'AUTH_REQUIRED') $('#auth-banner').classList.remove('hidden');
      return false;
    }
  }

  async function loadDoctor() {
    const tbody = $('#doctor-body');
    tbody.innerHTML = '<tr><td colspan="3" class="muted">自检中…</td></tr>';
    try {
      const r = await api('/api/doctor');
      tbody.innerHTML = '';
      (r.checks || []).forEach((c) => {
        const tr = document.createElement('tr');
        const cls = c.status === 'ok' ? 'status-ok' : (c.status === 'warn' ? 'status-warn' : 'status-fail');
        const txt = c.status === 'ok' ? '正常' : (c.status === 'warn' ? '注意' : '失败');
        tr.innerHTML = '<td>' + esc(c.name) + '</td><td class="' + cls + '">' + txt +
          '</td><td class="muted">' + esc(c.detail) + '</td>';
        tbody.appendChild(tr);
      });
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="3" class="status-fail">' + esc(e.friendly) + '</td></tr>';
    }
  }

  // ---------------------------------------------------------------- 实时预览（MJPEG）
  function togglePreview() {
    const wrap = $('#preview-wrap');
    if (S.preview) { stopPreviewImg(); return; }
    const img = document.createElement('img');
    img.className = 'preview-img';
    img.id = 'preview-img';
    img.alt = '实时预览';
    img.src = BASE + '/api/preview.mjpg?token=' + encodeURIComponent(TOKEN) + '&t=' + Date.now();
    const holder = document.createElement('div');
    holder.id = 'preview-wrap';
    holder.appendChild(img);
    $('.preview-bar').insertAdjacentElement('afterend', holder);
    S.preview = true;
    $('#btn-preview').textContent = '关闭实时预览';
  }
  function stopPreviewImg() {
    const wrap = $('#preview-wrap');
    if (wrap) wrap.remove();
    S.preview = false;
    const b = $('#btn-preview');
    if (b) b.textContent = '开启实时预览';
  }

  // ---------------------------------------------------------------- 事件绑定
  function bind() {
    document.querySelectorAll('.nav-item').forEach((b) => {
      b.addEventListener('click', () => switchTab(b.dataset.tab));
    });
    $('#btn-refresh').addEventListener('click', () => { loadState().then(loadFaces).catch((e) => toast(e.friendly, 'err')); });
    $('#btn-add-face').addEventListener('click', openEnrollDialog);
    $('#btn-add-face-2').addEventListener('click', openEnrollDialog);
    $('#btn-retry-auth').addEventListener('click', () => { loadFaces(); });
    $('#user-select').addEventListener('change', (e) => { S.user = e.target.value; loadFaces(); });
    $('#btn-preview').addEventListener('click', togglePreview);

    $('#btn-verify-start').addEventListener('click', startVerify);
    $('#btn-verify-stop').addEventListener('click', () => stopVerify(false));

    $('#cfg-enabled').addEventListener('change', (e) => {
      saveConfig({ enabled: e.target.checked }, '总开关已' + (e.target.checked ? '开启' : '关闭'));
    });
    const slider = $('#cfg-threshold');
    slider.addEventListener('input', () => { $('#threshold-out').textContent = parseFloat(slider.value).toFixed(2); });
    slider.addEventListener('change', () => {
      saveConfig({ threshold: parseFloat(slider.value) }, '阈值已设为 ' + parseFloat(slider.value).toFixed(2));
    });
    $('#btn-save-vote').addEventListener('click', () => {
      saveConfig({
        window_frames: parseInt($('#cfg-window').value, 10),
        required_frames: parseInt($('#cfg-required').value, 10),
        timeout_ms: parseInt($('#cfg-timeout').value, 10),
        no_face_timeout_ms: parseInt($('#cfg-no-face').value, 10),
      }, '多帧投票参数已保存');
    });
    $('#btn-pam-enable').addEventListener('click', async () => {
      const ok = await confirmDialog('启用人脸认证',
        '将把 <code>pam_faceunlock.so</code> 加入 PAM 认证栈（需要管理员授权）。启用后登录/锁屏会先尝试人脸。', '启用');
      if (!ok) return;
      try {
        const r = await api('/api/pam', { method: 'POST', body: { action: 'enable' } });
        S.state.pam = r.pam; renderSettings(); toast('已启用人脸认证', 'ok');
      } catch (e) { toast(e.friendly, 'err'); }
    });
    $('#btn-pam-disable').addEventListener('click', async () => {
      const ok = await confirmDialog('停用人脸认证',
        '将从 PAM 认证栈移除人脸模块（保留已录入的模板）。确定继续？', '停用', true);
      if (!ok) return;
      try {
        const r = await api('/api/pam', { method: 'POST', body: { action: 'disable' } });
        S.state.pam = r.pam; renderSettings(); toast('已停用人脸认证', 'ok');
      } catch (e) { toast(e.friendly, 'err'); }
    });
    $('#btn-panic').addEventListener('click', async () => {
      const ok1 = await confirmDialog('一键停用（panic）',
        '这会<b>关闭总开关</b>并把人脸模块从 PAM 栈移除，用于「人脸出问题进不去系统」时快速还原。<br>' +
        '已有模板不会被删除，之后可以重新启用。', '继续', true);
      if (!ok1) return;
      const ok2 = await confirmDialog('再次确认',
        '确定要一键停用人脸识别登录吗？之后登录将只能使用密码。', '确认停用', true);
      if (!ok2) return;
      try {
        const r = await api('/api/pam', { method: 'POST', body: { action: 'panic' } });
        S.state.pam = r.pam;
        await loadState();
        renderSettings();
        loadDoctor();
        toast('已一键停用：人脸模块已从 PAM 移除，总开关已关闭', 'ok');
      } catch (e) { toast(e.friendly, 'err'); }
    });
    $('#btn-doctor').addEventListener('click', loadDoctor);

    // 心跳：后端退出时给出明确提示
    setInterval(async () => {
      try {
        await fetch(BASE + '/health');
        if (!S.state) return;
      } catch (e) {
        setBackend('fail', '后端已断开');
      }
    }, 5000);
  }

  // ---------------------------------------------------------------- 启动
  async function boot() {
    if (!FU || !FU.port) {
      $('#app').classList.add('hidden');
      $('#fatal').classList.remove('hidden');
      const err = window.__FACEUNLOCK_ERROR__;
      $('#fatal-msg').textContent = (err && err.error) ? err.error : '未收到后端握手信息。';
      return;
    }
    $('#app').classList.remove('hidden');
    try {
      await loadState();
    } catch (e) {
      setBackend('fail', '后端不可用');
      toast(e.friendly, 'err');
    }
    bind();
    // 支持深链：Rust 侧 --tab 注入的 FU.tab，或 URL hash（#settings 等）
    const want = (FU && FU.tab) || (location.hash || '').replace('#', '').trim();
    switchTab(['faces', 'verify', 'settings'].indexOf(want) >= 0 ? want : 'faces');
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
