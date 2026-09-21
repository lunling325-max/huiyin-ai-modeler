/* 回音 · AI 建模智能体 — Vue 3 无构建前端。
   外观 1:1 复刻《UI页面参考代码.html》;内部保留自有大脑链路:
   POST /api/run (SSE) -> 把 thought / tool / tool_result / done / end
   渲染进参考款消息气泡(工具卡/渲染预览/模型下载嵌于 AI 消息内)。
   会话历史存 localStorage,切回旧会话 = 续同一服务端工作目录。 */
(function () {
  'use strict';

  var P = 'zaowutai_'; // localStorage 前缀

  function load(k, d) {
    try {
      var v = localStorage.getItem(P + k);
      return v === null ? d : JSON.parse(v);
    } catch (e) { return d; }
  }
  function save(k, v) {
    try { localStorage.setItem(P + k, JSON.stringify(v)); } catch (e) {}
  }

  function uid() {
    return (Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  }
  function hhmm() {
    try { return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }
    catch (e) { return ''; }
  }
  function trunc(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n) + '…(已截断)' : s;
  }
  function cut(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n) : s;
  }

  var DEFAULT_ACCENT = '#7c6df0';

  Vue.createApp({
    data: function () {
      var theme = load('theme', 'light');
      return {
        // ---- 顶栏/外观 ----
        dark: theme === 'dark',
        settingsOpen: false,
        sidebarOpen: false,
        apiDot: '',                // ''=成功 · sending · error
        fontSize: load('font', 'medium'),
        showTs: load('showts', true),
        showActs: load('showacts', true),
        accent: load('accent', DEFAULT_ACCENT),
        DEFAULT_ACCENT: DEFAULT_ACCENT,

        // ---- 大脑模型(顶栏插头切换) ----
        modelList: [],               // 从 /api/models 拉取(预置+自定义)
        model: '',                    // 当前选中模型 id(空=服务端默认)
        modelMenuOpen: false,
        modelFormOpen: false,         // 「添加模型」表单展开
        modelForm: { label: '', base_url: '', api_key: '', model: '' },
        modelHelpOpen: false,          // 「怎么填」示例面板开合
        modelHelpList: [               // 常用 OpenAI 兼容服务示例(点一下自动填入表单)
          { name: 'DeepSeek Flash', base_url: '', model: 'deepseek-v4-flash' },
          { name: 'DeepSeek Pro',   base_url: '', model: 'deepseek-v4-pro' },
          { name: '本地 Ollama',    base_url: 'http://localhost:11434/v1', model: 'llama3.2' },
          { name: '通义千问 Qwen',  base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
          { name: '智谱 GLM',       base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
          { name: 'Kimi Moonshot',  base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
        ],

        // ---- 登录 ----
        loginVisible: false,
        loginUser: '',
        loginPass: '',
        currentUser: load('user', ''),   // 已登录用户名(空=未登录)

        // ---- 输入 ----
        input: '',
        composing: false,
        pendingImg: null,          // {name, dataUrl} 待发送参考图

        // ---- 会话 ----
        convs: [],                 // 已完成/进行中的会话(左侧历史)
        cur: { id: uid(), title: '', updatedAt: Date.now(), sid: null, workdir: '', blocks: [] },
        busy: false,
        toastOn: false,
        toastMsg: '',

        examples: [],                 // 空状态实际展示的示例芯片(每次随机挑 3)
        examplePool: [                // 更大的示例池,展示时随机选
          { t: '做一把简洁的木椅', i: 'fas fa-chair' },
          { t: '一张圆形小茶几', i: 'fas fa-table' },
          { t: '一座朱红金顶的中式宫殿', i: 'fas fa-building-columns' },
          { t: '公园里的木头凉亭', i: 'fas fa-tree' },
          { t: '一只卡通小黄鸭', i: 'fas fa-shapes' },
          { t: '一个三层书架', i: 'fas fa-layer-group' },
          { t: '一盏简约台灯', i: 'fas fa-lightbulb' },
          { t: '一盆多肉盆栽', i: 'fas fa-seedling' },
        ],

        // 流内部状态
        _reason: null,
        _liveThought: null,
        _lastTool: null
      };
    },

    computed: {
      canSend: function () {
        return !this.busy && (((this.input || '').trim().length > 0) || !!this.pendingImg);
      },
      modelLabel: function () {
        for (var i = 0; i < this.modelList.length; i++) {
          if (this.modelList[i].id === (this.model || 'deepseek-v4-flash')) return this.modelList[i].label;
        }
        return 'DeepSeek V4 Flash · 快速';
      },
      // 用户标识单一来源: 消息头像 + 左下角头像都用它, 保证两处永远一致。
      // 已登录 -> 名字首字母; 未登录(即"我") -> "你"。
      meGlyph: function () {
        var u = (this.currentUser || '').trim();
        return u ? u.charAt(0).toUpperCase() : '用户';
      }
    },

    watch: {
      dark: 'applyRoot',
      fontSize: 'applyRoot',
      showTs: 'applyRoot',
      showActs: 'applyRoot',
      accent: 'applyRoot'
    },

    mounted: function () {
      this.toastTimer = null;
      // 先读会话历史,再 applyRoot(applyRoot 内会 persist,不能把历史写空)
      this.convs = this._hydrate(load('convs', []));
      var active = load('active', null);
      var found = null;
      for (var i = 0; i < this.convs.length; i++) {
        if (this.convs[i].id === active) { found = this.convs[i]; break; }
      }
      this.cur = found ? found : this._newDraft();
      this.model = load('model', '') || '';   // 恢复上次选的模型(空=服务端默认)
      this.loadModels();              // 拉取可用模型列表(预置+自定义)
      this.pickExamples();            // 首次加载给一组随机示例
      this.applyRoot();

      var self = this;
      // 点击设置下拉/模型菜单之外 -> 关闭
      document.addEventListener('click', function (e) {
        var r = self.$refs.topRight;
        if (r && !r.contains(e.target)) {
          if (self.settingsOpen) self.settingsOpen = false;
          if (self.modelMenuOpen) self.modelMenuOpen = false;
        }
      });
      // Esc 关闭设置/模型菜单/抽屉/编辑态(关闭合集与上方点击一致)
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          self.settingsOpen = false;
          self.modelMenuOpen = false;
          self.sidebarOpen = false;
          if (self.cur && self.cur.blocks.length) {
            for (var j = self.cur.blocks.length - 1; j >= 0; j--) {
              var m = self.cur.blocks[j];
              if (m.role === 'user' && m.editing) { m.editing = false; return; }
            }
          }
        }
      });

      this.refreshScroll();
    },

    methods: {
      // ================= 外观 / 设置 =================
      applyRoot: function () {
        var el = document.documentElement;
        if (this.dark) el.setAttribute('data-theme', 'dark');
        else el.removeAttribute('data-theme');
        el.classList.remove('font-small', 'font-medium', 'font-large');
        el.classList.add('font-' + this.fontSize);
        el.classList.toggle('no-timestamp', !this.showTs);
        el.classList.toggle('hide-actions', !this.showActs);
        this._applyAccent();
        this.persist();
      },
      _applyAccent: function () {
        var c = /^#?([0-9a-f]{6})$/i.exec(String(this.accent));
        var hex = this.accent, r, g, b;
        if (c) {
          hex = '#' + c[1].toLowerCase();
          r = parseInt(c[1].slice(0, 2), 16);
          g = parseInt(c[1].slice(2, 4), 16);
          b = parseInt(c[1].slice(4, 6), 16);
        } else { // 非法值回退默认
          hex = DEFAULT_ACCENT; r = 124; g = 109; b = 240;
          this.accent = DEFAULT_ACCENT;
        }
        var dark = this.dark;
        var hover = 'rgb(' + Math.round(r * 0.85) + ',' + Math.round(g * 0.85) + ',' + Math.round(b * 0.85) + ')';
        var soft = dark ? '0.25' : '0.15';
        var glow = dark ? '0.30' : '0.20';
        var st = document.documentElement.style;
        st.setProperty('--accent', hex);
        st.setProperty('--accent-hover', hover);
        st.setProperty('--accent-soft', 'rgba(' + r + ',' + g + ',' + b + ',' + soft + ')');
        st.setProperty('--accent-glow', 'rgba(' + r + ',' + g + ',' + b + ',' + glow + ')');
      },
      setFont: function (f) { this.fontSize = f; save('font', f); this.applyRoot(); },
      setAccent: function (v) { this.accent = v; save('accent', v); this.applyRoot(); },
      persistView: function () {
        save('showts', this.showTs);
        save('showacts', this.showActs);
      },
      toggleTheme: function () {
        this.dark = !this.dark;
        save('theme', this.dark ? 'dark' : 'light');
        this.modelMenuOpen = false;
        this.settingsOpen = false;
        this.applyRoot();
      },
      // 顶栏插头:开合模型菜单(打开时关设置;两者互斥,保证顺序无关)
      toggleModelMenu: function () {
        this.modelMenuOpen = !this.modelMenuOpen;
        this.settingsOpen = false;
      },
      // 顶栏齿轮:开合设置(打开时关模型菜单;与上面互相排斥)
      toggleSettings: function () {
        this.settingsOpen = !this.settingsOpen;
        this.modelMenuOpen = false;
      },
      setModel: function (m) {
        this.model = m.id;
        save('model', m.id);
        this.modelMenuOpen = false;
        this.modelFormOpen = false;
        this.toast('大脑模型已切换:' + m.label);
      },
      // 打开侧栏抽屉(移动端): 同时收掉顶栏下拉
      openSidebar: function () {
        this.sidebarOpen = true;
        this.modelMenuOpen = false;
        this.settingsOpen = false;
      },
      // 点击左下角用户资料: 未登录 -> 登录页; 已登录 -> 提示
      onUserClick: function () {
        if (this.currentUser) { this.toast('已登录: ' + this.currentUser); return; }
        this.loginVisible = true;
        this.loginUser = '';
        this.loginPass = '';
      },
      // 提交登录
      doLogin: function () {
        var u = (this.loginUser || '').trim();
        if (!u) { this.toast('请先填用户名'); return; }
        this.currentUser = u;
        save('user', u);
        this.loginVisible = false;
        this.loginUser = '';
        this.loginPass = '';
        this.toast('登录成功,欢迎: ' + u);
      },
      // 退出登录
      logout: function () {
        if (!confirm('退出当前登录?')) return;
        this.currentUser = '';
        save('user', '');
        this.toast('已退出登录');
      },
      // 拉取 /api/models,刷新菜单(手动选中则保持; model 不在列表则回退默认)
      loadModels: function () {
        var self = this;
        fetch('/api/models').then(function (r) { return r.json(); }).then(function (d) {
          self.modelList = (d && d.models) || [];
          if (self.model) {
            var hit = false;
            for (var i = 0; i < self.modelList.length; i++) {
              if (self.modelList[i].id === self.model) { hit = true; break; }
            }
            if (!hit) { self.model = ''; save('model', ''); }
          }
        }).catch(function () {});
      },
      // 「添加模型」: 展开表单 / 收起
      toggleModelForm: function () {
        this.modelFormOpen = !this.modelFormOpen;
        if (this.modelFormOpen) this.modelForm = { label: '', base_url: '', api_key: '', model: '' };
      },
      // 「怎么填」: 点示例自动填 base_url/model/label 并展开表单
      useModelHelp: function (h) {
        this.modelForm.base_url = h.base_url || '';
        this.modelForm.model = h.model;
        if (!this.modelForm.label) this.modelForm.label = h.name;
        this.modelHelpOpen = false;
        this.modelFormOpen = true;
        this.toast('已填入:' + h.name);
      },
      // 提交自定义模型
      submitModel: function () {
        var self = this;
        var f = this.modelForm;
        if (!(f.model || '').trim()) {
          this.toast('请先填写模型名'); return;
        }
        fetch('/api/models', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label: f.label, base_url: f.base_url.trim(), api_key: f.api_key.trim(), model: f.model.trim() })
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d.error) { self.toast(d.error); return; }
          self.modelList = d.models || self.modelList;
          if (d.id) { self.model = d.id; save('model', d.id); self.toast('已添加模型'); }
          self.modelFormOpen = false;
        }).catch(function () { self.toast('添加模型失败'); });
      },
      // 删除任意模型(预置/自定义均可, 从池子移除)
      delModel: function (m, e) {
        if (e) e.stopPropagation();
        if (!m.id) return;
        var self = this;
        if (!confirm('删除模型「' + (m.label || m.id) + '」?')) return;
        fetch('/api/models/' + encodeURIComponent(m.id), { method: 'DELETE' })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d.error) { self.toast(d.error); return; }
            self.modelList = d.models || self.modelList;
            if (self.model === m.id) { self.model = ''; save('model', ''); }
            self.toast('已删除');
          });
      },
      toast: function (msg) {
        var self = this;
        this.toastMsg = msg;
        this.toastOn = true;
        if (this.toastTimer) clearTimeout(this.toastTimer);
        this.toastTimer = setTimeout(function () { self.toastOn = false; }, 2000);
      },
      timeAgo: function (ts) {
        var diff = Date.now() - (ts || 0);
        if (diff < 60000) return '刚刚';
        if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前';
        if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前';
        if (diff < 172800000) return '昨天';
        return Math.floor(diff / 86400000) + '天前';
      },

      // ================= 会话管理 =================
      _newDraft: function () {
        return { id: uid(), title: '', updatedAt: Date.now(), sid: null, workdir: '', blocks: [] };
      },
      isCur: function (id) { return !!this.cur && this.cur.id === id; },
      newChat: function () {
        if (this.busy) { this.toast('模型正在生成并自审中,请稍候,完成后即可新建对话'); return; }
        this.cur = this._newDraft();
        save('active', null);
        this.persist();
        this.pickExamples();          // 每个新对话换一组随机示例
        this.refreshScroll();
      },
      // 从示例池随机挑 3 个填到空状态(洗牌不重复)
      pickExamples: function () {
        var arr = (this.examplePool || []).slice();
        for (var i = arr.length - 1; i > 0; i--) {
          var j = Math.floor(Math.random() * (i + 1));
          var t = arr[i]; arr[i] = arr[j]; arr[j] = t;
        }
        this.examples = arr.slice(0, 3);
      },
      selectConv: function (id) {
        // 生成中允许切换浏览: 流仍写入发起请求的会话(_runConv), 不会串台
        for (var i = 0; i < this.convs.length; i++) {
          if (this.convs[i].id === id) {
            this.cur = this.convs[i];
            save('active', id);
            this.sidebarOpen = false;
            this.refreshScroll();
            return;
          }
        }
      },
      deleteConv: function (id) {
        if (this.busy) { this.toast('智能体正在工作,稍候再删'); return; }
        if (!confirm('删除这个本地会话记录?')) return;
        var next = null;
        for (var i = 0; i < this.convs.length; i++) {
          if (this.convs[i].id === id) {
            this.convs.splice(i, 1);
            if (this.convs.length) next = this.convs[Math.max(0, i - 1)];
            break;
          }
        }
        this.persist();
        if (this.cur && this.cur.id === id) {
          this.cur = next || this._newDraft();
          save('active', next ? next.id : null);
        }
        this.refreshScroll();
      },
      clearAll: function () {
        if (this.busy) { this.toast('智能体正在工作,稍候再清空'); return; }
        if (!confirm('确定清空所有对话记录?服务端产物文件不受影响。')) return;
        this.convs = [];
        save('convs', []);
        this.cur = this._newDraft();
        save('active', null);
        this.refreshScroll();
      },

      // ================= 消息操作 =================
      startEdit: function (m) {
        if (this.busy) { this.toast('智能体正在工作,稍候再改'); return; }
        m.editing = true;
        m.editText = m.text || '';
      },
      cancelEdit: function (m) {
        m.editing = false;
        m.editText = '';
      },
      saveEdit: function (m) {
        var t = (m.editText || '').trim();
        if (!t) { this.toast('内容不能为空'); return; }
        m.text = t;
        m.editing = false;
        m.editText = '';
        // 是最后一条用户消息(尚无 AI 应答)-> 直接以新文本重跑
        if (!this.busy && this._isLastUser(m)) {
          this.resendFromUser(m);
        } else {
          this.toast('已更新这条消息');
          this.persist();
        }
      },
      _isLastUser: function (m) {
        var idx = this.cur.blocks.indexOf(m);
        return idx === this.cur.blocks.length - 1;
      },
      deleteMsg: function (m) {
        if (this.busy) { this.toast('智能体正在工作,稍候再删'); return; }
        if (!confirm('删除这条消息?')) return;
        var i = this.cur.blocks.indexOf(m);
        if (i >= 0) this.cur.blocks.splice(i, 1);
        this.persist();
        this.refreshScroll();
      },
      copyMsg: function (m) {
        var text;
        if (m.role === 'user') {
          text = m.text || '';
        } else {
          var parts = [];
          (m.blocks || []).forEach(function (b) {
            if (b.k === 'text' && b.text) parts.push(b.text);
            else if (b.k === 'error' && b.text) parts.push(b.text);
            else if (b.k === 'thought' && b.text) parts.push(b.text);
            else if (b.k === 'files') parts.push('附件: ' + b.groups.map(function (g) {
              return g.items.map(function (f) { return f.name; }).join(', ');
            }).join('; '));
          });
          text = parts.join('\n\n');
        }
        if (!text) { this.toast('该消息无可复制的文本内容'); return; }
        var self = this;
        function ok() { self.toast('已复制'); }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(ok).catch(function () { self._copyFallback(text); ok(); });
        } else { self._copyFallback(text); ok(); }
      },
      _copyFallback: function (text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e) {}
        document.body.removeChild(ta);
      },

      // ================= 重新生成 =================
      regen: function (mAsst) {
        if (this.busy) { this.toast('智能体正在工作'); return; }
        var idx = this.cur.blocks.indexOf(mAsst);
        if (idx < 0) return;
        // 向前找最近一条用户请求
        var ui = -1;
        for (var i = idx - 1; i >= 0; i--) {
          if (this.cur.blocks[i].role === 'user') { ui = i; break; }
        }
        if (ui < 0) { this.toast('找不到对应的用户请求'); return; }
        // 从该用户请求之后全删(含本 AI 消息),重跑同一请求(同会话=sid 不变)
        this.cur.blocks.splice(ui + 1);
        var u = this.cur.blocks[ui];
        this._spawnRun(u.text, u.img || null, false);
      },
      resendFromUser: function (u) {
        // u 已更新文本,是最后一条 -> 追加 AI 并重跑(不重复推用户气泡)
        this._spawnRun(u.text, u.img || null, false);
      },
      _spawnRun: function (text, imgData, addUser) {
        if (!text || this.busy) return;
        if (addUser !== false) this._pushUser(text, imgData || null);
        this._ensureSaved();
        var mAsst = this._pushAssistant();
        this.apiDot = 'sending';
        this.busy = true;
        this._runMsg = mAsst;              // 供"停止生成"定位当前正在写的助手消息
        this._runConv = this.cur;          // 锁住本次流写入的会话(不是当前视图)
        this.refreshScroll();
        this._runHttp(text, imgData || null, mAsst);
      },

      // ================= 发送 =================
      send: function () {
        if (!this.canSend || this.busy) return;
        var text = (this.input || '').trim();
        var img = this.pendingImg;
        if (!text && img) text = '请参考上传的图片,建模还原这个形象,渲染自审后交付。';
        this.input = '';
        this.pendingImg = null;
        this._spawnRun(text, img ? img.dataUrl : null);
      },
      onEnterKey: function (e) {
        if (this.composing || e.isComposing) return;
        if (e.shiftKey) return;
        e.preventDefault();
        this.send();
      },
      onEnterEdit: function (e, m) {
        if (this.composing || e.isComposing) return;
        e.preventDefault();
        var t = (m.editText || '').trim();
        if (!t) { this.toast('内容不能为空'); return; }
        this.saveEdit(m);
      },
      useExample: function (t) {
        if (this.busy) return;
        this.input = t;
        this.send();
      },
      // 停止/暂停生成: abort 流 + 通知服务端中止该会话的 run
      stopGen: function () {
        if (!this.busy) return;
        var sid = (this._runConv && this._runConv.sid) || (this.cur && this.cur.sid) || null;
        if (this._abortCtrl) { try { this._abortCtrl.abort(); } catch (e) {} this._abortCtrl = null; }
        if (sid) {
          // 让服务端主循环下一轮检测到 cancel 即中止, 不浪费资源
          fetch('/api/stop', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sid })
          }).catch(function () {});
        }
        var m = this._runMsg || (this._runConv && this._runConv.blocks.length ? this._runConv.blocks[this._runConv.blocks.length - 1] : null);
        this._stopStreaming(m);
        this.toast('已停止生成');
      },

      _pushUser: function (text, img) {
        var m = {
          role: 'user', id: uid(), ts: Date.now(), tsText: hhmm(),
          text: text, img: img || null
        };
        this.cur.blocks.push(m);
        if (!this.cur.title && text) this.cur.title = trunc(text, 28);
        this.cur.updatedAt = Date.now();
      },
      _pushAssistant: function () {
        var m = {
          role: 'assistant', id: uid(), ts: Date.now(), tsText: hhmm(),
          status: 'sending', blocks: []
        };
        this.cur.blocks.push(m);
        return m;
      },

      // 会话登记进左侧历史(草稿第一次发送时)
      _ensureSaved: function () {
        var inList = false;
        for (var i = 0; i < this.convs.length; i++) if (this.convs[i].id === this.cur.id) { inList = true; break; }
        if (!inList) this.convs.push(this.cur);
        save('active', this.cur.id);
        this.persist();
      },

      // ================= SSE 引擎(保留自原实现) =================
      _runHttp: function (message, image, mAsst) {
        var self = this;
        var payload = { message: message, session_id: this.cur.sid || null };
        if (this.model) payload.model = this.model;   // 顶栏选择的模型(空=服务端默认)
        if (image) {
          var body = image;
          if (body.indexOf(',') >= 0 && body.indexOf('data:') === 0) body = body.slice(body.indexOf(',') + 1);
          payload.image = { name: '参考图.png', data: body };
        }
        var ctrl = new AbortController();
        self._abortCtrl = ctrl;              // 供"停止生成"abort 本次 fetch
        fetch('/api/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal: ctrl.signal
        }).then(function (res) {
          if (!res.ok) {
            return res.text().then(function (t) { throw new Error('HTTP ' + res.status + ' ' + t.slice(0, 200)); });
          }
          if (!res.body || !res.body.getReader) {
            throw new Error('当前浏览器不支持流式读取,请用新版 Chrome/Edge');
          }
          var reader = res.body.getReader();
          var dec = new TextDecoder('utf-8');
          var buf = '';
          function pump() {
            return reader.read().then(function (r) {
              if (r.done) return;
              buf += dec.decode(r.value, { stream: true });
              var cut;
              while ((cut = buf.indexOf('\n\n')) >= 0) {
                var chunk = buf.slice(0, cut);
                buf = buf.slice(cut + 2);
                self._onSSE(chunk, mAsst);
              }
              return pump();
            });
          }
          return pump();
        }).catch(function (err) {
          self._stopStreaming(mAsst);
          if (err && err.name === 'AbortError') {
            // 用户主动暂停: 不推错误, 正常收尾
            self.apiDot = '';
            self.persist();
            self.refreshScroll();
            return;
          }
          self._pushBlk(mAsst, { k: 'error', text: '连接建模服务失败: ' + (err && err.message ? err.message : err) });
          self.apiDot = 'error';
          self.persist();
          self.refreshScroll();
        }).then(function () {
          self._stopStreaming(mAsst);
        });
      },
      _onSSE: function (chunk, mAsst) {
        var lines = chunk.split('\n');
        for (var i = 0; i < lines.length; i++) {
          var l = lines[i];
          if (l.indexOf('data: ') !== 0) continue;
          var payload = l.slice(6).replace(/\r$/, '');
          var ev;
          try { ev = JSON.parse(payload); } catch (e) { continue; }
          this._handleEvent(ev, mAsst);
        }
      },
      _handleEvent: function (ev, mAsst) {
        var self = this;
        var rc = this._runConv || this.cur;   // 流写入目标 = 发起请求的那条会话(与当前视图解耦, 切走不串台)
        switch (ev.type) {
          case 'meta':
            if (ev.session_id) rc.sid = ev.session_id;
            if (ev.workdir) rc.workdir = ev.workdir;
            this.apiDot = '';
            this.persist();
            break;
          case 'thought':
            this._stopLiveThought(mAsst);
            this._pushBlk(mAsst, { k: 'thought', text: ev.text || '', live: true });
            this._liveThought = this._lastBlock(mAsst);
            break;
          case 'llm_token':
            // 推理流的思考过程边生成边推: 只收 reasoning, 让"它在想"那块活起来; content 是机器 JSON 不逐字展示。
            if (ev.kind === 'reasoning') {
              if (!this._thinkingToken) {
                this._stopLiveThought(mAsst);
                this._thinkingToken = { k: 'reason', text: '', live: true };
                this._pushBlk(mAsst, this._thinkingToken);
              }
              this._thinkingToken.text += ev.text;
              this.refreshScroll();
            }
            break;
          case 'tool': {
            this._stopLiveThought(mAsst);
            var args = ev.args || {};
            var argsStr;
            try { argsStr = JSON.stringify(args, null, 2); }
            catch (e) { argsStr = String(args); }
            argsStr = cut(argsStr, 12000);
            var tb = { k: 'tool', name: ev.name || 'tool', args: argsStr, result: null, st: 'run', open: false };
            this._pushBlk(mAsst, tb);
            this._lastTool = tb;
            break;
          }
          case 'tool_result':
            if (this._lastTool) {
              this._lastTool.result = cut(ev.text || '', 6000);
              this._lastTool.st = /Traceback|Error|error|失败|failed|Exception/i.test(ev.text || '') ? 'bad' : 'ok';
              this._lastTool = null;
            }
            break;
          case 'done':
            this._stopLiveThought(mAsst);
            if (ev.answer) this._pushBlk(mAsst, { k: 'text', text: ev.answer });
            break;
          case 'error':
            this._stopLiveThought(mAsst);
            this._pushBlk(mAsst, { k: 'error', text: ev.text || '未知错误' });
            break;
          case 'end':
            this._stopLiveThought(mAsst);
            this._stopStreaming(mAsst);
            if (ev.session_id) rc.sid = ev.session_id;
            this.apiDot = '';
            var arts = ev.artifacts || [];
            var pngs = arts.filter(function (a) { return a.kind === 'png'; });
            var models = arts.filter(function (a) { return a.kind === 'model'; });
            var others = arts.filter(function (a) { return a.kind !== 'png' && a.kind !== 'model'; });
            if (pngs.length) this._pushBlk(mAsst, { k: 'imgs', items: pngs });
            var groups = [];
            if (models.length) groups.push({
              icon: 'fas fa-box-open',
              title: '模型文件 · 下载后可用 Blender 打开 / 通用查看 / 3D 打印',
              items: models
            });
            if (others.length) groups.push({ icon: 'fas fa-file-lines', title: '其他产物', items: others });
            if (groups.length) this._pushBlk(mAsst, { k: 'files', groups: groups });
            rc.updatedAt = Date.now();
            this.persist();
            this.refreshScroll();
            break;
        }
        void self;
      },

      // ---- 块/状态工具 ----
      _lastBlock: function (m) {
        return m.blocks.length ? m.blocks[m.blocks.length - 1] : null;
      },
      _pushBlk: function (m, b) {
        m.blocks.push(b);
        this.refreshScroll();
      },
      _stopLiveThought: function (m) {
        if (this._liveThought) {
          this._liveThought.live = false;
          this._liveThought = null;
        }
        if (this._thinkingToken) {
          this._thinkingToken.live = false;
          this._thinkingToken = null;
        }
        void m;
      },
      _stopStreaming: function (m) {
        this.busy = false;
        this._runConv = null;            // 本次流结束, 归属会话解锁
        this._runMsg = null;
        this._abortCtrl = null;
        this._thinkingToken = null;
        if (m) m.status = '';
      },

      // ================= 参考图 =================
      pickRef: function () {
        if (this.busy) return;
        this.$refs.file.click();
      },
      onFile: function (ev) {
        var f = ev.target.files && ev.target.files[0];
        ev.target.value = '';
        if (!f) return;
        var self = this;
        var rd = new FileReader();
        rd.onload = function () {
          var img = new Image();
          img.onload = function () {
            var MAX = 1024;
            var s = Math.min(1, MAX / Math.max(img.width, img.height));
            var c = document.createElement('canvas');
            c.width = Math.max(1, Math.round(img.width * s));
            c.height = Math.max(1, Math.round(img.height * s));
            c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
            self.pendingImg = { name: f.name || '参考图.png', dataUrl: c.toDataURL('image/png') };
            self.toast('参考图已就绪');
          };
          img.src = rd.result;
        };
        rd.readAsDataURL(f);
      },

      // ================= 滚动 / 持久化 =================
      refreshScroll: function () {
        var self = this;
        this.$nextTick(function () {
          var el = self.$refs.msgs;
          if (el) el.scrollTop = el.scrollHeight;
        });
      },
      persist: function () {
        // 序列化时去掉瞬态字段、压缩超长文本;整体过大则丢弃会话内图片 dataURL
        var clean = [];
        for (var i = 0; i < this.convs.length; i++) {
          var c = this.convs[i];
          clean.push({
            id: c.id, title: c.title || '新对话', updatedAt: c.updatedAt || Date.now(),
            sid: c.sid || null, workdir: c.workdir || '',
            blocks: (c.blocks || []).map(this._sanitizeMsg).filter(function (m) {
              return !(m.role === 'assistant' && (!m.blocks || !m.blocks.length));
            })
          });
        }
        var j = JSON.stringify(clean);
        if (j && j.length > 3500000) {
          clean.forEach(function (c) {
            c.blocks.forEach(function (m) {
              if (m.role === 'user' && m.img && String(m.img).indexOf('data:') === 0) m.img = null;
            });
          });
        }
        save('convs', clean);
      },
      _sanitizeMsg: function (m) {
        if (m.role === 'user') {
          var u = { role: 'user', id: m.id, ts: m.ts, tsText: m.tsText, text: m.text || '', img: m.img || null };
          return u;
        }
        var blks = (m.blocks || []).map(function (b) {
          var o = { k: b.k };
          if (b.k === 'tool') {
            o.name = b.name; o.args = cut(b.args || '', 12000);
            o.result = b.result === null || b.result === undefined ? null : cut(b.result, 6000);
            o.st = b.st || 'ok'; o.open = false;
          } else if (b.k === 'thought') o.text = b.text;
          else if (b.k === 'text') o.text = b.text;
          else if (b.k === 'error') o.text = b.text;
          else if (b.k === 'imgs') o.items = b.items;
          else if (b.k === 'files') o.groups = b.groups;
          return o;
        });
        return { role: 'assistant', id: m.id, ts: m.ts, tsText: m.tsText, status: '', blocks: blks };
      },
      _hydrate: function (list) {
        // 从存储还原(打平历史条目形态)
        var out = [];
        for (var i = 0; i < list.length; i++) {
          var c = list[i];
          if (c && typeof c === 'object' && Array.isArray(c.blocks)) {
            c.blocks.forEach(function (m) {
              m.editing = false;
              m.editText = '';
              if (!m.tsText) {
                try { m.tsText = m.ts ? new Date(m.ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''; }
                catch (e) { m.tsText = ''; }
              }
              if (m.role === 'assistant') m.status = '';
            });
            out.push(c);
          }
        }
        return out;
      },

      // ================= 展示辅助 =================
      human: function (n) {
        n = Number(n) || 0;
        if (n < 1024) return n + ' B';
        if (n < 1048576) return Math.round(n / 1024) + ' KB';
        return (n / 1048576).toFixed(1) + ' MB';
      },
      extOf: function (name) {
        var m = /\.([a-zA-Z0-9]+)$/.exec(String(name));
        return m ? m[1].toUpperCase() : 'FILE';
      },
      toolFa: function (name) {
        return {
          blender_exec: 'fa-cubes',
          render_preview: 'fa-camera',
          export_glb_stl: 'fa-box-open'
        }[name] || 'fa-cog';
      },
      toolLabel: function (name) {
        return {
          blender_exec: 'blender_exec · 现场自写建模代码',
          render_preview: 'render_preview · 渲染预览自检',
          export_glb_stl: 'export_glb_stl · 导出模型文件'
        }[name] || name;
      },
      toolDesc: function (name) {
        return {
          blender_exec: '按需求现场生成并执行 Blender Python 代码',
          render_preview: '无头渲染 PNG 供智能体自审外观',
          export_glb_stl: '交付 .blend 可编辑 / .glb 通用 / .stl 毫米可打印'
        }[name] || '';
      }
    }
  }).mount('#app');
})();
