/* =========================================================
   智慧眼 · 前端逻辑
   - 状态轮询: state / logs / buffer (2s/1s/2s)
   - 图库: 懒加载分页
   - 配置: 一键保存 (热生效,无需重启)
   ========================================================= */
const { createApp, ref, computed, onMounted, onUnmounted, nextTick, watch } = Vue;

createApp({
  setup() {
    // ---------- 基础 ----------
    const tab = ref('dashboard');
    const tabs = [
      { key: 'dashboard', label: '实时状态', icon: '◉' },
      { key: 'identity',  label: '身份画像', icon: '👤' },
      { key: 'gallery',   label: '历史图库', icon: '▦' },
      { key: 'config',    label: '运行配置', icon: '⚙' },
      { key: 'logs',      label: '实时日志', icon: '☰' },
    ];

    const online = ref(false);
    const now = ref('');
    const state = ref({});
    const cfg = ref({});
    const bufferItems = ref([]);
    const identityMd = ref('');
    const toast = ref(null);
    function showToast(msg, type = 'ok', ms = 2000) {
      toast.value = { msg, type };
      setTimeout(() => { toast.value = null; }, ms);
    }

    // ---------- 状态轮询 ----------
    let stateTimer = null;
    async function pollState() {
      try {
        const r = await fetch('/api/state');
        const j = await r.json();
        if (j.ok) {
          state.value = j.data;
          online.value = true;
        }
      } catch (e) {
        online.value = false;
      }
    }
    async function pollBuffer() {
      try {
        const r = await fetch('/api/buffer');
        const j = await r.json();
        if (j.ok) bufferItems.value = j.data || [];
      } catch (_) {}
    }
    async function pollIdentity() {
      try {
        const r = await fetch('/api/identity');
        const j = await r.json();
        if (j.ok) identityMd.value = j.data || '';
      } catch (_) {}
    }
    function tickClock() {
      const d = new Date();
      const p = n => String(n).padStart(2, '0');
      now.value = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    // ---------- 状态显示 ----------
    const isProcessing = computed(() => {
      const s = state.value.state;
      return s === 'CAPTURE' || s === 'PROCESS' || s === 'THINKING' || s === 'DISPLAY';
    });
    const isBusy = computed(() => isProcessing.value);
    const stateLabel = computed(() => {
      const m = {
        READY: '待机 READY',
        CAPTURE: '📷 抓拍中…',
        PROCESS: '🖼 图像增强…',
        THINKING: '🧠 AI 识别中…',
        DISPLAY: '📋 结果展示',
        ERROR: '⚠️ 错误',
      };
      const label = m[state.value.state] || (state.value.state || 'INIT');
      if (state.value.state === 'READY' && bufferItems.value.length > 0) {
        return `⏳ 待消费 (${bufferItems.value.length} 张)`;
      }
      return label;
    });
    const stateClass = computed(() => 's-' + (state.value.state || 'READY'));

    function formatTime(ts) {
      if (!ts) return '--';
      const d = new Date(ts > 1e12 ? ts : ts * 1000);
      const p = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    function shortTime(ts) {
      if (!ts) return '--';
      const d = new Date(ts > 1e12 ? ts : ts * 1000);
      const p = n => String(n).padStart(2, '0');
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    // ---------- 手动触发 ----------
    async function trigger() {
      try {
        const r = await fetch('/api/trigger', { method: 'POST' });
        const j = await r.json();
        if (j.ok) showToast('已入队,等待处理…');
        else showToast(j.err || '失败', 'err');
      } catch (e) {
        showToast('网络错误: ' + e.message, 'err');
      }
    }

    // ---------- 图库 ----------
    const gallery = ref({ items: [], total: 0, page: 1, per_page: 20 });
    const galleryStatus = ref('');
    const modal = ref(null);

    async function reloadGallery() {
      await loadPage(1);
    }
    async function loadPage(p) {
      const url = `/api/images?page=${p}&per_page=${gallery.value.per_page}` +
                  (galleryStatus.value ? `&status=${galleryStatus.value}` : '');
      const r = await fetch(url);
      const j = await r.json();
      if (j.ok) gallery.value = j.data;
    }
    function openImage(img) { modal.value = img; }
    async function delImage(img, fromModal = false) {
      if (!confirm(`确认删除「${img.object_name || '未知'}」?`)) return;
      const r = await fetch('/api/images/' + img.id, { method: 'DELETE' });
      const j = await r.json();
      if (j.ok) {
        showToast('已删除');
        if (fromModal) modal.value = null;
        reloadGallery();
        pollState();
      } else {
        showToast('删除失败', 'err');
      }
    }

    // ---------- 配置 ----------
    async function loadConfig() {
      const r = await fetch('/api/config');
      const j = await r.json();
      if (j.ok) cfg.value = j.data;
    }
    async function saveConfig() {
      const r = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg.value),
      });
      const j = await r.json();
      if (j.ok) {
        cfg.value = j.data;
        showToast('已保存,立即生效');
      } else {
        showToast('保存失败', 'err');
      }
    }

    // ---------- 日志 ----------
    const logLines = ref([]);
    const logAuto = ref(true);
    const logbox = ref(null);
    let lastLogIdx = 0;
    async function pollLogs() {
      const r = await fetch('/api/logs?tail=200');
      const j = await r.json();
      if (!j.ok) return;
      const lines = j.data || [];
      if (lines.length < lastLogIdx) {
        logLines.value = lines;
        lastLogIdx = 0;
      } else if (lines.length > lastLogIdx) {
        const inc = lines.slice(lastLogIdx);
        logLines.value = logLines.value.concat(inc);
        lastLogIdx = lines.length;
        if (logLines.value.length > 1000) logLines.value = logLines.value.slice(-500);
        if (logAuto.value) {
          nextTick(() => {
            if (logbox.value) logbox.value.scrollTop = logbox.value.scrollHeight;
          });
        }
      }
    }

    // ---------- 生命周期 ----------
    let clockTimer = null, logTimer = null;
    onMounted(() => {
      tickClock(); setInterval(tickClock, 1000);
      pollState(); stateTimer = setInterval(pollState, 2000);
      pollBuffer(); setInterval(pollBuffer, 2000);
      pollIdentity(); setInterval(pollIdentity, 5000);
      loadConfig();
      loadPage(1);
      pollLogs(); logTimer = setInterval(pollLogs, 1000);
    });
    onUnmounted(() => {
      clearInterval(stateTimer); clearInterval(clockTimer); clearInterval(logTimer);
    });

    watch(tab, (v) => { if (v === 'gallery') loadPage(gallery.value.page); });

    return {
      tab, tabs, online, now, state, bufferItems, identityMd,
      stateLabel, stateClass, isBusy, isProcessing, formatTime, shortTime,
      trigger,
      gallery, galleryStatus, reloadGallery, loadPage, modal, openImage, delImage,
      cfg, loadConfig, saveConfig,
      logLines, logAuto, logbox,
      toast,
    };
  }
}).mount('#app');
