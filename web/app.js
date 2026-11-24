/* 成果画布：沿用原 WebSocket 协议，按真实任务展示作品、确认和执行经过。 */
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    connection: $('connection-status'), connectionLabel: $('connection-label'),
    files: $('workspace-files'), logout: $('logout-form'),
    newWork: $('new-chat'), workSelect: $('work-select'), title: $('page-title'),
    detail: $('session-detail'), welcome: $('welcome'), scroll: $('canvas-scroll'),
    outputView: $('output-view'), historyView: $('history-view'), sourcesView: $('sources-view'),
    documentTitle: $('document-title'), documentStatus: $('document-status'),
    output: $('output-document'), empty: $('output-empty'), wordCount: $('output-word-count'),
    copy: $('copy-output'), export: $('export-output'), activityCount: $('activity-count'),
    activityEmpty: $('activity-empty'), activityList: $('activity-list'),
    sourcesEmpty: $('sources-empty'), sourcesList: $('sources-list'),
    goal: $('work-goal'), decisions: $('decision-list'), commandDock: $('command-dock'),
    decisionPanel: $('decision-panel'), decisionQuestion: $('decision-question'),
    decisionDock: $('decision-dock'), decisionStatus: $('decision-status'),
    form: $('composer-form'), input: $('prompt-input'), send: $('send-button'),
    sendLabel: $('send-label'), hint: $('composer-hint'), taskStatus: $('task-status'),
    cancel: $('cancel-task'), connectionNotice: $('connection-notice'),
    connectionNoticeText: $('connection-notice-text'), reconnect: $('reconnect'),
    toast: $('toast'), scrollBottom: $('scroll-bottom'),
    footerNote: document.querySelector('.footer-note'),
  };
  const state = {
    socket: null, connected: false, hosted: false, busy: false, cancelling: false, resetting: false,
    answerSending: false, turns: [], active: null, selected: null, pending: null,
    questions: new Map(), view: 'output', counter: 0, follow: true,
    renderQueued: false, renderedTurn: null, renderedText: null, toastTimer: null,
    selection: '', tools: [],
  };
  const labels = {
    initializing: '正在准备知识库与工具', retrieving: '正在查找相关资料',
    thinking: '正在构思与整理', working: '正在执行任务', waiting: '有一件事，需要你确认',
    cancelling: '正在停止，等待当前操作结束', idle: '准备好了，我们从一个想法开始',
  };
  const toolNames = {
    ask_user: '向你确认', fetch: '获取网页资料', read_file: '阅读文件',
    read_text_file: '阅读文本', read_media_file: '读取媒体文件', read_multiple_files: '阅读多份文件',
    write_file: '写入成果文件', append_file: '补充文件', edit_file: '修改文件',
    list_directory: '查看资料目录', list_directory_with_sizes: '查看目录',
    directory_tree: '梳理目录结构', create_directory: '创建文件夹',
    search_files: '查找资料', move_file: '移动文件', copy_file: '复制文件',
    get_file_info: '查看文件信息', list_allowed_directories: '查看工作范围',
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function toast(text) {
    clearTimeout(state.toastTimer);
    ui.toast.textContent = text;
    ui.toast.hidden = false;
    state.toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 2800);
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast('已复制'); }
    catch { toast('未能复制，请选择正文手动复制'); }
  }
  function resizeInput() {
    ui.input.style.height = 'auto';
    const limit = Math.max(44, Math.min(132, innerHeight * 0.2));
    ui.input.style.height = Math.min(ui.input.scrollHeight, limit) + 'px';
  }
  function selectedTurn() {
    return state.turns.find((turn) => turn.id === state.selected) || null;
  }
  function draftText(turn) {
    if (!turn) return '';
    if (turn.status === 'done') return turn.result;
    for (let i = turn.phases.length - 1; i >= 0; i -= 1) {
      if (turn.phases[i].text) return turn.phases[i].text;
    }
    return '';
  }
  function titleOf(turn, raw) {
    const heading = raw.trimStart().match(/^#{1,4}\s+([^\n]+)/);
    return heading ? heading[1].replace(/\*\*|\x60/g, '').trim().slice(0, 90) : turn.prompt.split('\n')[0].slice(0, 52);
  }
  function updateComposer() {
    const waiting = Boolean(state.pending);
    document.body.dataset.phase = !state.connected ? 'offline'
      : state.cancelling ? 'cancelling' : waiting ? 'waiting' : state.busy ? 'working' : 'idle';
    const destination = waiting ? ui.decisionDock : ui.commandDock;
    if (ui.form.parentElement !== destination) destination.append(ui.form);
    ui.decisionPanel.hidden = !waiting;
    if (waiting) {
      ui.decisionQuestion.textContent = state.pending.question;
      ui.decisionStatus.textContent = state.answerSending ? '答复正在送达…' : '你的答复会用于继续当前任务';
    }
    const canType = state.connected && !state.resetting && !state.cancelling && (!state.busy || waiting);
    ui.input.disabled = !canType || state.answerSending;
    ui.send.disabled = ui.input.disabled || !ui.input.value.trim();
    ui.sendLabel.textContent = waiting ? '确认并继续' : state.turns.length ? '调整这份作品' : '开始创作';
    ui.send.setAttribute('aria-label', waiting ? '回复确认并继续任务' : '提交创作要求');
    ui.input.placeholder = waiting ? '写下你的选择或补充信息…'
      : state.cancelling ? '正在停止当前操作…'
      : state.busy ? '作品正在生成，你可以查看过程或取消任务'
      : !state.connected ? '连接工作空间后即可开始'
      : state.turns.length ? '哪里需要调整？也可以提出下一个任务…' : '描述你想完成的事，或从一个例子开始…';
    ui.hint.textContent = waiting ? '这是对当前询问的回复' : 'Enter 提交 · Shift + Enter 换行';
    ui.cancel.hidden = !state.busy;
    ui.cancel.disabled = !state.connected || state.cancelling;
    ui.cancel.textContent = state.cancelling ? '正在停止…' : '取消任务';
    ui.newWork.disabled = !state.connected || state.busy || state.resetting;
    ui.workSelect.disabled = state.turns.length === 0;
    resizeInput();
  }
  function setConnection(kind) {
    state.connected = kind === 'connected';
    ui.connection.dataset.state = kind;
    ui.connectionLabel.textContent = kind === 'connected' ? '已连接' : kind === 'connecting' ? '正在连接' : '已断开';
    updateComposer();
  }
  function switchView(view) {
    state.view = view;
    ui.outputView.hidden = view !== 'output';
    ui.historyView.hidden = view !== 'history';
    ui.sourcesView.hidden = view !== 'sources';
    document.querySelectorAll('[data-view]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.view === view));
    });
    render();
    ui.scroll.scrollTop = 0;
  }
  function scheduleRender() {
    if (state.renderQueued) return;
    state.renderQueued = true;
    requestAnimationFrame(() => { state.renderQueued = false; render(); });
  }

  function inlineText(parent, text) {
    const pattern = /\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g;
    let previous = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(previous, match.index)));
      if (match[1] !== undefined) {
        parent.append(element("strong", "", match[1]));
      } else if (match[2] !== undefined) {
        parent.append(element("code", "", match[2]));
      } else {
        let valid = false;
        try { valid = ["http:", "https:"].includes(new URL(match[4]).protocol); } catch { /* 普通文字回退 */ }
        if (valid) {
          const link = element("a", "", match[3]);
          link.href = match[4];
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          parent.append(link);
        } else parent.append(document.createTextNode(match[0]));
      }
      previous = match.index + match[0].length;
    }
    parent.append(document.createTextNode(text.slice(previous)));
  }

  function tableCells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  }

  function isTableHeader(lines, index) {
    if (!lines[index].includes("|") || index + 1 >= lines.length) return false;
    const cells = tableCells(lines[index + 1]);
    return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function markdown(text, target) {
    const fragment = document.createDocumentFragment();
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    const isBlock = (line) => /^\s*(```|#{1,4}\s|[-*+]\s|\d+\.\s|>\s?)/.test(line) || /^\s*([-*_])\1{2,}\s*$/.test(line);
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      if (/^\s*```/.test(line)) {
        const language = line.trim().slice(3).trim();
        const code = [];
        index += 1;
        while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
        if (index < lines.length) index += 1;
        const wrapper = element("div", "code-block");
        const header = element("div", "code-header");
        header.append(element("span", "", language || "代码"));
        const copy = element("button", "", "复制代码");
        copy.type = "button";
        copy.addEventListener("click", () => copyText(code.join("\n")));
        header.append(copy);
        const pre = document.createElement("pre");
        pre.append(element("code", "", code.join("\n")));
        wrapper.append(header, pre);
        fragment.append(wrapper);
        continue;
      }
      if (isTableHeader(lines, index)) {
        const wrapper = element("div", "markdown-table");
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const row = document.createElement("tr");
        tableCells(lines[index]).forEach((cell) => { const th = document.createElement("th"); inlineText(th, cell); row.append(th); });
        head.append(row);
        table.append(head);
        index += 2;
        const body = document.createElement("tbody");
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
          const tr = document.createElement("tr");
          tableCells(lines[index++]).forEach((cell) => { const td = document.createElement("td"); inlineText(td, cell); tr.append(td); });
          body.append(tr);
        }
        table.append(body);
        wrapper.append(table);
        fragment.append(wrapper);
        continue;
      }
      const heading = line.match(/^\s*(#{1,4})\s+(.+)$/);
      if (heading) {
        const node = document.createElement(`h${heading[1].length}`);
        inlineText(node, heading[2]); fragment.append(node); index += 1; continue;
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { fragment.append(document.createElement("hr")); index += 1; continue; }
      if (/^\s*>/.test(line)) {
        const quote = element("blockquote");
        const collected = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) collected.push(lines[index++].replace(/^\s*>\s?/, ""));
        inlineText(quote, collected.join("\n")); fragment.append(quote); continue;
      }
      const listMatch = line.match(/^\s*([-*+]|\d+\.)\s+(.+)$/);
      if (listMatch) {
        const ordered = /^\d/.test(listMatch[1]);
        const list = document.createElement(ordered ? "ol" : "ul");
        while (index < lines.length) {
          const item = lines[index].match(/^\s*([-*+]|\d+\.)\s+(.+)$/);
          if (!item || /^\d/.test(item[1]) !== ordered) break;
          const li = document.createElement("li"); inlineText(li, item[2]); list.append(li); index += 1;
        }
        fragment.append(list); continue;
      }
      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !isBlock(lines[index]) && !isTableHeader(lines, index)) paragraph.push(lines[index++]);
      const p = document.createElement("p"); inlineText(p, paragraph.join("\n")); fragment.append(p);
    }
    target.replaceChildren(fragment);
  }



  const notice = element('div', 'canvas-notice');
  notice.setAttribute('role', 'status');
  notice.hidden = true;
  ui.output.before(notice);
  const emptyResult = element('p', 'empty-result-note');
  emptyResult.hidden = true;
  ui.empty.append(emptyResult);
  const selectionBar = element('div', 'selection-context');
  selectionBar.hidden = true;
  const selectionText = element('span', 'selection-excerpt');
  const selectionUse = element('button', 'text-button', '针对这段修改');
  selectionUse.type = 'button';
  const selectionClear = element('button', 'text-button', '取消选段');
  selectionClear.type = 'button';
  selectionBar.append(selectionText, selectionUse, selectionClear);
  ui.commandDock.prepend(selectionBar);

  function clearSelection() {
    state.selection = '';
    selectionBar.hidden = true;
  }
  function captureSelection() {
    if (state.busy || !state.connected) return;
    const selection = getSelection();
    if (!selection || selection.isCollapsed || !ui.output.contains(selection.anchorNode) || !ui.output.contains(selection.focusNode)) return;
    const text = selection.toString().trim().slice(0, 3000);
    if (!text) return;
    state.selection = text;
    selectionText.textContent = '选中：' + text.slice(0, 80) + (text.length > 80 ? '…' : '');
    selectionBar.hidden = false;
  }
  selectionUse.addEventListener('click', () => {
    if (!state.selection || state.busy) return;
    ui.input.value = '请修改以下片段：\n\n' + state.selection + '\n\n修改要求：';
    updateComposer();
    ui.input.focus();
    ui.input.setSelectionRange(ui.input.value.length, ui.input.value.length);
  });
  selectionClear.addEventListener('click', clearSelection);
  ui.output.addEventListener('mouseup', captureSelection);
  ui.output.addEventListener('keyup', captureSelection);

  function renderOutput(turn) {
    const raw = draftText(turn);
    const hasContent = Boolean(raw.trim());
    ui.welcome.hidden = state.turns.length > 0;
    ui.empty.hidden = hasContent;
    ui.output.hidden = !hasContent;
    emptyResult.hidden = !turn || hasContent || ['error', 'cancelled'].includes(turn.status);
    if (turn) emptyResult.textContent = turn.status === 'done'
      ? '本轮没有返回正文，可以到“任务经过”查看实际执行结果。'
      : turn.status === 'waiting' ? '收到你的补充后，继续完善这份作品。' : '正在准备这份作品，内容会在这里逐步呈现。';
    ui.documentTitle.textContent = turn ? titleOf(turn, raw) : '你的下一份作品';
    const statuses = { running: '正在打磨', waiting: '等你确认', done: '本轮已完成', error: '本轮未完成', cancelled: '本轮已取消' };
    ui.documentStatus.textContent = turn ? (turn.status === 'done' && !hasContent ? '执行已结束' : statuses[turn.status]) : '尚未开始';
    ui.documentStatus.dataset.state = turn ? turn.status : 'empty';
    ui.wordCount.textContent = raw ? Array.from(raw.replace(/\s/g, '')).length + ' 字' : '等待一份真实成果';
    ui.copy.disabled = !hasContent;
    ui.export.disabled = !turn || turn.status !== 'done' || !hasContent;
    if (state.renderedTurn !== (turn && turn.id) || state.renderedText !== raw) {
      const body = raw.trimStart().replace(/^#{1,4}\s+[^\n]+\n?/, '');
      markdown(body, ui.output);
      state.renderedTurn = turn && turn.id;
      state.renderedText = raw;
    }
    notice.hidden = !turn || !turn.notice;
    if (turn && turn.notice) {
      notice.textContent = turn.notice;
      notice.className = 'canvas-notice' + (turn.status === 'error' ? ' error' : '');
    }
    ui.goal.textContent = turn ? turn.prompt : '你的目标、资料和想法，会汇成这里的一份作品。';
    ui.decisions.replaceChildren();
    if (!turn || turn.confirmations.length === 0) {
      ui.decisions.append(element('p', 'brief-empty', '需要你做决定时，会在这里留下依据。'));
    } else {
      for (const item of turn.confirmations) {
        const note = element('div', 'decision-note');
        note.append(element('p', 'decision-note-question', item.question));
        note.append(element('p', 'decision-note-answer', item.answer || (item.status === 'cancelled' ? '本次确认已结束' : '等待你的补充')));
        ui.decisions.append(note);
      }
    }
  }
  function renderHistory(turn) {
    const opened = new Set(Array.from(ui.activityList.querySelectorAll('details[open]')).map((node) => node.dataset.key));
    ui.activityList.replaceChildren();
    ui.activityEmpty.hidden = Boolean(turn);
    ui.activityCount.textContent = turn ? String(turn.steps.length) : '0';
    if (!turn) return;
    const brief = element('li', 'activity-item done');
    brief.append(element('strong', 'activity-name', '任务简报'), element('p', 'activity-summary', turn.prompt));
    ui.activityList.append(brief);
    for (const item of turn.confirmations) {
      const row = element('li', 'activity-item ' + (item.answer ? 'done' : item.status === 'cancelled' ? 'interrupted' : 'running'));
      row.append(element('strong', 'activity-name', '与你确认'), element('p', 'activity-summary', item.question));
      row.append(element('p', 'activity-answer', item.answer || (item.status === 'cancelled' ? '已结束' : '等待回复')));
      ui.activityList.append(row);
    }
    for (const step of turn.steps) {
      if (step.name === 'ask_user' && turn.confirmations.length) continue;
      const row = element('li', 'activity-item ' + step.status);
      row.append(element('strong', 'activity-name', toolNames[step.name] || step.name));
      const statuses = { running: '正在执行', done: '已完成', error: '未能完成', deferred: '暂缓，等待重新决策', interrupted: '已中断' };
      row.append(element('span', 'activity-state', statuses[step.status]));
      const details = document.createElement('details');
      details.dataset.key = step.key;
      details.open = opened.has(step.key);
      details.append(element('summary', '', '查看执行详情'));
      const content = [];
      if (step.arguments !== undefined) content.push('参数\n' + JSON.stringify(step.arguments, null, 2));
      if (step.result !== undefined) content.push('结果\n' + JSON.stringify(step.result, null, 2));
      details.append(element('pre', 'activity-details', content.join('\n\n').slice(0, 14000)));
      row.append(details);
      ui.activityList.append(row);
    }
    if (turn.steps.length === 0 && turn.phases.some((phase) => phase.text)) {
      const row = element('li', 'activity-item ' + (turn.status === 'done' ? 'done' : 'running'));
      row.append(element('strong', 'activity-name', '整理与生成成果'), element('p', 'activity-summary', '这次任务未调用外部工具，内容直接呈现在成果页。'));
      ui.activityList.append(row);
    }
  }
  function addSource(turn, value, kind) {
    if (typeof value !== 'string' || !value.trim()) return;
    if (kind === 'web' || kind === 'mentioned') {
      try { const url = new URL(value); if (!['http:', 'https:'].includes(url.protocol)) return; value = url.href; }
      catch { return; }
      const existing = turn.sources.find((item) => item.value === value && ['web', 'mentioned'].includes(item.kind));
      if (existing) {
        if (kind === 'web') existing.kind = 'web';
        return;
      }
    }
    if (!turn.sources.some((item) => item.value === value && item.kind === kind)) turn.sources.push({ value, kind });
  }
  function renderSources(turn) {
    ui.sourcesList.replaceChildren();
    ui.sourcesEmpty.hidden = Boolean(turn && turn.sources.length);
    if (!turn) return;
    for (const source of turn.sources) {
      const item = element('li', 'source-entry');
      item.append(element('span', 'source-kind', source.kind === 'web' ? '已访问网页' : source.kind === 'mentioned' ? '正文中的链接' : source.kind === 'output' ? '输出文件' : '参考文件'));
      if (source.kind === 'web' || source.kind === 'mentioned') {
        const link = element('a', 'source-link', source.value);
        link.href = source.value; link.target = '_blank'; link.rel = 'noopener noreferrer';
        item.append(link);
      } else item.append(element('code', 'source-path', source.value));
      ui.sourcesList.append(item);
    }
  }
  function render() {
    const turn = selectedTurn();
    ui.title.textContent = turn ? turn.prompt.split('\n')[0].slice(0, 40) : '今天，想让什么好事发生？';
    ui.detail.textContent = state.turns.length ? '本次工作 · ' + state.turns.length + ' 份结果' : '从一个小小的想法开始，我们一起把它做好。';
    renderOutput(turn);
    if (state.view === 'history') renderHistory(turn);
    if (state.view === 'sources') renderSources(turn);
    if (state.view === 'output' && state.follow && state.busy && turn === state.active) ui.scroll.scrollTop = ui.scroll.scrollHeight;
  }
  function updateWorkSelect() {
    ui.workSelect.replaceChildren();
    if (!state.turns.length) ui.workSelect.append(new Option('本次成果', ''));
    for (const turn of state.turns) {
      ui.workSelect.append(new Option(String(turn.id).padStart(2, '0') + ' · ' + turn.prompt.split('\n')[0].slice(0, 24), String(turn.id)));
    }
    ui.workSelect.value = state.selected === null ? '' : String(state.selected);
  }
  function finishPending(status) {
    if (state.pending) state.pending.status = status;
    state.pending = null;
    state.answerSending = false;
    updateComposer();
  }
  function interruptSteps() {
    if (!state.active) return;
    for (const step of state.active.steps) if (step.status === 'running') step.status = 'interrupted';
  }
  function toolEvent(event) {
    const turn = state.active;
    if (!turn) return;
    const key = turn.phases.length + ':' + (event.id || event.name);
    let step = turn.stepMap.get(key);
    if (!step) {
      step = { key, name: event.name, status: 'running' };
      turn.stepMap.set(key, step);
      turn.steps.push(step);
    }
    if (event.arguments !== undefined) step.arguments = event.arguments;
    if (event.result !== undefined) step.result = event.result;
    step.status = event.type === 'tool_deferred' ? 'deferred' : event.type === 'tool_result' ? (event.result && event.result.isError ? 'error' : 'done') : 'running';
    if (step.status === 'done' && step.arguments) {
      if (typeof step.arguments.url === 'string') addSource(turn, step.arguments.url, 'web');
      const path = step.arguments.path;
      if (typeof path === 'string') addSource(turn, path, /write|append|edit|move|copy|create/.test(step.name) ? 'output' : 'file');
      if (Array.isArray(step.arguments.paths)) step.arguments.paths.forEach((value) => addSource(turn, value, 'file'));
    }
    scheduleRender();
  }
  function send(payload) {
    if (!state.connected || !state.socket || state.socket.readyState !== WebSocket.OPEN) { toast('工作空间尚未连接'); return false; }
    const text = JSON.stringify(payload);
    if (text.length > 50000) { toast('内容过长，请拆成几次提交'); return false; }
    state.socket.send(text);
    return true;
  }
  function clearLocal() {
    state.busy = false; state.cancelling = false; state.resetting = false;
    state.turns = []; state.active = null; state.selected = null; state.counter = 0;
    state.questions.clear(); state.tools = []; state.renderedText = null; state.renderedTurn = null;
    finishPending('cancelled'); clearSelection(); updateWorkSelect();
    ui.input.value = ''; ui.taskStatus.textContent = '新作品，从你的想法开始';
    switchView('output'); updateComposer();
  }
  function handleEvent(event) {
    const turn = state.active;
    switch (event.type) {
      case 'ready':
        state.hosted = event.authenticated === true;
        if (ui.files) ui.files.hidden = event.files_url !== '/files';
        if (ui.logout) ui.logout.hidden = event.authenticated !== true;
        if (ui.footerNote && state.hosted) ui.footerNote.textContent = '云端工作空间 · 请及时下载文件';
        setConnection('connected'); ui.connectionNotice.hidden = true;
        ui.taskStatus.textContent = '工作空间已连接'; ui.input.focus(); break;
      case 'status':
        ui.taskStatus.textContent = event.message || labels[event.state] || '正在处理';
        if (event.state === 'idle') { state.busy = false; state.cancelling = false; state.resetting = false; if (state.pending) finishPending('cancelled'); }
        else if (event.state === 'cancelling') state.cancelling = true;
        else state.busy = true;
        updateComposer(); scheduleRender(); break;
      case 'response_start':
        if (turn) { turn.phase = { text: '' }; turn.phases.push(turn.phase); }
        break;
      case 'delta':
        if (!turn || typeof event.content !== 'string') break;
        if (!turn.phase) { turn.phase = { text: '' }; turn.phases.push(turn.phase); }
        turn.phase.text += event.content; scheduleRender(); break;
      case 'question': {
        if (!turn || state.questions.has(event.request_id)) break;
        const confirmation = { id: event.request_id, question: event.question, answer: '', status: 'waiting' };
        turn.confirmations.push(confirmation); state.questions.set(event.request_id, confirmation);
        state.pending = confirmation; state.answerSending = false; state.busy = true;
        turn.status = 'waiting'; state.selected = turn.id;
        updateWorkSelect(); switchView('output'); updateComposer(); ui.input.focus(); break;
      }
      case 'answer_accepted': {
        const confirmation = state.questions.get(event.request_id);
        if (!confirmation || confirmation.answer) break;
        confirmation.answer = event.content;
        confirmation.status = 'answered';
        if (state.pending && state.pending.id === event.request_id) finishPending('answered');
        if (turn) turn.status = 'running';
        updateComposer(); scheduleRender(); break;
      }
      case 'tools': state.tools = event.tools || []; break;
      case 'tool_start': case 'tool_result': case 'tool_deferred': toolEvent(event); break;
      case 'done':
        if (!turn) break;
        turn.result = typeof event.content === 'string' ? event.content : '';
        turn.status = 'done';
        turn.notice = '';
        for (const match of turn.result.matchAll(/https?:\/\/[^\s<>()\]]+/g)) {
          const url = match[0].replace(/[，。；、！？,.]+$/, '');
          if (!turn.sources.some((source) => source.value === url)) addSource(turn, url, 'mentioned');
        }
        state.follow = false; render(); ui.scroll.scrollTop = 0; break;
      case 'cancelled':
        finishPending('cancelled'); interruptSteps();
        if (turn) { turn.status = 'cancelled'; turn.notice = '当前任务已取消。已执行的文件操作会保留，可以继续提出新要求。'; }
        ui.input.value = ''; scheduleRender(); break;
      case 'reset': clearLocal(); toast('已开启一份新作品'); break;
      case 'error': {
        state.answerSending = false;
        const validationError = ['busy', 'stale_question', 'invalid_message', 'invalid_json', 'invalid_answer'].includes(event.code);
        if (validationError) {
          if (event.code === 'stale_question') finishPending('cancelled');
          toast(event.message || '请检查输入后重试');
        } else {
          finishPending('cancelled'); interruptSteps();
          if (turn) { turn.status = 'error'; turn.notice = event.message || '本轮未能完成，请稍后重试。'; }
          else toast(event.message || '工作空间暂时不可用');
        }
        updateComposer(); scheduleRender(); break;
      }
      default: break;
    }
  }
  function connect() {
    if (!['http:', 'https:'].includes(location.protocol)) {
      setConnection('offline');
      ui.connectionNotice.hidden = false;
      ui.connectionNoticeText.textContent = '当前是网页文件预览。请先运行 uv run web_main.py，再打开本地工作空间进行交互。';
      ui.reconnect.textContent = '打开工作空间 ↗';
      ui.reconnect.disabled = false;
      ui.taskStatus.textContent = '预览模式 · 尚未连接本地服务';
      return;
    }
    setConnection('connecting'); ui.reconnect.disabled = true;
    const previous = state.socket;
    const socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    state.socket = socket;
    if (previous) previous.close();
    socket.addEventListener('message', (message) => {
      if (state.socket !== socket) return;
      try { handleEvent(JSON.parse(message.data)); }
      catch { toast('收到无法识别的服务消息，请重新连接'); }
    });
    socket.addEventListener('close', (event) => {
      if (state.socket !== socket) return;
      if (event.code === 4401) { location.href = '/auth/login'; return; }
      finishPending('cancelled'); interruptSteps();
      if (state.active && !['done', 'error', 'cancelled'].includes(state.active.status)) {
        state.active.status = 'error'; state.active.notice = '连接中断，本轮没有完成。重新连接将开启新会话。';
      }
      setConnection('offline'); ui.connectionNotice.hidden = false;
      ui.connectionNoticeText.textContent = state.turns.length
        ? '连接已断开。重新连接将开启新作品，请先复制或导出需要保留的成果。'
        : state.hosted || !['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)
          ? '暂时无法连接工作空间，请稍后点击重新连接。'
          : '无法连接工作空间。请确认已运行 uv run web_main.py，并通过终端显示的地址打开网页。';
      if (event.code === 1013) ui.connectionNoticeText.textContent = '当前使用人数已达到上限，请稍后点击重新连接。';
      ui.reconnect.disabled = false; ui.taskStatus.textContent = '工作空间已断开';
      scheduleRender();
    });
    socket.addEventListener('error', () => {
      if (state.socket === socket) ui.connectionNoticeText.textContent = '无法连接，请确认本地网页服务仍在运行。';
    });
  }

  ui.form.addEventListener('submit', (event) => {
    event.preventDefault();
    const content = ui.input.value.trim();
    if (!content || ui.send.disabled) return;
    if (state.pending) {
      if (!send({ type: 'answer', request_id: state.pending.id, content })) return;
      state.answerSending = true;
    } else {
      if (state.busy) return;
      const selected = selectedTurn();
      let requestContent = content;
      let basedOn = null;
      // 回看旧成果后，明确把“这份作品”交给模型，避免它误改最近的其他成果。
      if (selected && selected !== state.active && selected.status === 'done' && selected.result.trim()) {
        basedOn = selected.id;
        const prefix = '页面上用户当前选中的作品：\n标题：' + titleOf(selected, selected.result)
          + '\n原需求：' + selected.prompt.slice(0, 500) + '\n作品正文（节选，完整内容已在对话历史中）：\n';
        const suffix = '\n\n用户的新要求：\n' + content
          + '\n\n如果新要求是在调整“这份作品”，以页面选中作品为对象；如果明确是独立任务，按用户新要求执行。';
        // 原成果已经在模型历史里，只补充定位线索，避免重复发送长文影响检索接口。
        const excerpt = selected.result.length > 2400
          ? selected.result.slice(0, 1800) + '\n[中间正文略去]\n' + selected.result.slice(-600)
          : selected.result;
        requestContent = prefix + excerpt + suffix;
      }
      if (!send({ type: 'message', content: requestContent })) return;
      const turn = {
        id: ++state.counter, prompt: content, status: 'running', result: '', notice: '',
        phases: [], phase: null, steps: [], stepMap: new Map(), confirmations: [], sources: [], basedOn,
      };
      state.turns.push(turn); state.active = turn; state.selected = turn.id;
      state.busy = true; state.follow = true;
      updateWorkSelect(); switchView('output');
      ui.taskStatus.textContent = '正在把你的想法整理成作品…';
    }
    ui.input.value = ''; clearSelection(); updateComposer(); scheduleRender();
  });
  ui.input.addEventListener('input', updateComposer);
  ui.input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!ui.send.disabled) ui.form.requestSubmit();
    }
  });
  ui.cancel.addEventListener('click', () => {
    if (!state.busy || state.cancelling || !send({ type: 'cancel' })) return;
    state.cancelling = true; ui.taskStatus.textContent = labels.cancelling; updateComposer();
  });
  ui.newWork.addEventListener('click', () => {
    if (state.busy || state.resetting || !send({ type: 'reset' })) return;
    state.resetting = true; updateComposer();
  });
  ui.workSelect.addEventListener('change', () => {
    state.selected = Number(ui.workSelect.value); state.follow = false;
    clearSelection(); render(); ui.scroll.scrollTop = 0;
  });
  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));
  $('activity-toggle').addEventListener('click', () => switchView(state.view === 'history' ? 'output' : 'history'));
  $('activity-close').addEventListener('click', () => switchView('output'));
  ui.reconnect.addEventListener('click', () => {
    if (!['http:', 'https:'].includes(location.protocol)) {
      location.href = 'http://127.0.0.1:8765/';
      return;
    }
    clearLocal(); connect();
  });
  ui.copy.addEventListener('click', () => { const raw = draftText(selectedTurn()); if (raw) copyText(raw); });
  ui.export.addEventListener('click', () => {
    const turn = selectedTurn();
    if (!turn || turn.status !== 'done' || !turn.result.trim()) return;
    const url = URL.createObjectURL(new Blob([turn.result], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = titleOf(turn, turn.result).replace(/[\\/:*?"<>|]/g, '_').slice(0, 60) + '.md';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => {
    if (state.busy) return;
    ui.input.value = button.dataset.prompt; updateComposer(); ui.input.focus();
  }));
  ui.scroll.addEventListener('scroll', () => {
    state.follow = ui.scroll.scrollHeight - ui.scroll.scrollTop - ui.scroll.clientHeight < 90;
    if (ui.scrollBottom) ui.scrollBottom.hidden = state.view !== 'output' || state.follow || !state.busy;
  }, { passive: true });
  if (ui.scrollBottom) ui.scrollBottom.addEventListener('click', () => { state.follow = true; ui.scroll.scrollTop = ui.scroll.scrollHeight; });
  addEventListener('resize', resizeInput);
  updateWorkSelect(); render(); updateComposer(); connect();
})();
