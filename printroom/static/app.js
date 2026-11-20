"use strict";

const state = { data: null, editingDocId: null, busy: false, clearPageDraft: false, focusPage: false };

const STATUS_TEXT = {
  draft: "草稿",
  ready: "已入队",
  queued: "排队中",
  printing: "打印中",
  jammed: "卡纸待停稳",
  stopped: "已停稳待换机",
  completed: "已完成",
  active: "打印中",
  complete: "完整出纸",
  scrapped: "作废",
  in_progress: "出纸中",
  initial: "初始派单",
  continuation: "换机续单",
};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function badge(status, label) {
  return el("span", { class: `badge ${status}`, text: label || STATUS_TEXT[status] || status });
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

async function api(method, path, body) {
  const options = { method, headers: { "Accept": "application/json" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const resp = await fetch(path, options);
  let payload = null;
  try { payload = await resp.json(); } catch (_) { /* ignore */ }
  if (!resp.ok) {
    const err = new Error((payload && payload.error) || `请求失败 (${resp.status})`);
    err.payload = payload;
    throw err;
  }
  return payload;
}

let bannerTimer = null;
function showBanner(message, kind = "error") {
  const banner = document.getElementById("banner");
  banner.hidden = false;
  banner.className = kind === "ok" ? "banner ok" : "banner";
  banner.textContent = message;
  if (bannerTimer) clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { banner.hidden = true; }, kind === "ok" ? 3500 : 8000);
}

async function act(fn, okMessage) {
  if (state.busy) return;
  state.busy = true;
  try {
    await fn();
    if (okMessage) showBanner(okMessage, "ok");
    await refresh();
  } catch (err) {
    showBanner(err.message);
    await refresh();
  } finally {
    state.busy = false;
  }
}

async function refresh() {
  try {
    // 自动刷新时保留正在输入的内容与焦点，避免 5 秒轮询打断录入。
    const pageInput = document.getElementById("page-input");
    const titleInput = document.getElementById("doc-title");
    const saved = {
      page: pageInput ? pageInput.value : null,
      pageFocused: pageInput ? document.activeElement === pageInput : false,
      title: titleInput ? titleInput.value : null,
      titleFocused: titleInput ? document.activeElement === titleInput : false,
    };

    const data = await api("GET", "/api/state");
    state.data = data;
    document.getElementById("server-time").textContent = "服务器时间 " + fmtTime(data.server_time);
    render();

    const newPage = document.getElementById("page-input");
    if (newPage && (saved.page !== null || state.clearPageDraft)) {
      newPage.value = state.clearPageDraft ? "" : saved.page;
      if (state.focusPage || (!state.clearPageDraft && saved.pageFocused)) newPage.focus();
    }
    const newTitle = document.getElementById("doc-title");
    if (newTitle && saved.title !== null) {
      newTitle.value = saved.title;
      if (saved.titleFocused) newTitle.focus();
    }
    state.clearPageDraft = false;
    state.focusPage = false;
  } catch (err) {
    showBanner("无法连接服务：" + err.message);
  }
}

// ---------------------------------------------------------------- 文档录入

function renderDocuments() {
  const { documents } = state.data;
  const list = document.getElementById("documents");
  list.replaceChildren();

  const editing = documents.find((d) => d.id === state.editingDocId) || null;
  const editor = document.getElementById("doc-editor");
  editor.replaceChildren();

  if (!editing) {
    editor.className = "editor muted";
    editor.textContent = documents.length ? "点击下方草稿继续录入页面。" : "还没有正在编辑的文档。";
  } else if (editing.status !== "draft") {
    editor.className = "editor muted";
    editor.textContent = `《${editing.title}》已入队，页面锁定；共 ${editing.page_count} 页。`;
  } else {
    editor.className = "editor";
    const pages = el("ul", { class: "pages" });
    if (editing.pages.length === 0) {
      pages.append(el("li", { class: "muted", text: "尚未录入页面，请在下面按顺序添加。" }));
    }
    for (const p of editing.pages) {
      const empty = !p.content.trim();
      pages.append(el("li", {},
        el("span", { class: "no", text: `第 ${p.page_no} 页` }),
        el("span", { class: `txt${empty ? " empty" : ""}`, text: empty ? "（空白页）" : p.content }),
      ));
    }
    const sheets = Math.ceil(editing.page_count / 2);
    editor.append(
      el("h3", { text: `《${editing.title}》` }),
      el("div", { class: "meta", text: `共 ${editing.page_count} 页 · 双面打印需 ${sheets} 张纸` }),
      pages,
      el("form", { class: "row", onsubmit: (event) => {
        event.preventDefault();
        const input = document.getElementById("page-input");
        const content = input.value;
        act(async () => {
          await api("POST", `/api/documents/${editing.id}/pages`, { content });
          state.clearPageDraft = true;
          state.focusPage = true;
        });
      } },
        el("input", { id: "page-input", placeholder: "按顺序录入这一页的内容（可留空）", autocomplete: "off" }),
        el("button", { type: "submit", text: "添加一页" }),
      ),
      el("div", { class: "actions" },
        el("button", { class: "ghost small", type: "button", text: "删除末页",
          onclick: () => act(() => api("POST", `/api/documents/${editing.id}/pages/last`)) }),
        el("button", { class: "small", type: "button", text: "提交入队",
          disabled: editing.page_count < 1 ? "" : null,
          onclick: () => act(() => api("POST", `/api/documents/${editing.id}/finalize`), "已提交入队") }),
      ),
    );
  }

  const drafts = documents.filter((d) => d.status === "draft");
  for (const doc of drafts) {
    const active = doc.id === state.editingDocId;
    list.append(el("li", {},
      el("div", {},
        el("div", { class: "title", text: `《${doc.title}》` }),
        el("div", { class: "meta", text: `${doc.page_count} 页 · 草稿` }),
      ),
      el("div", { class: "actions" },
        el("button", { class: `ghost small${active ? "" : ""}`, type: "button",
          text: active ? "编辑中" : "继续录入",
          onclick: () => { state.editingDocId = doc.id; render(); } }),
        el("button", { class: "small", type: "button", text: "提交入队",
          disabled: doc.page_count < 1 ? "" : null,
          onclick: () => act(() => api("POST", `/api/documents/${doc.id}/finalize`), "已提交入队") }),
      ),
    ));
  }
}

// ---------------------------------------------------------------- 打印机

function attemptLine(attempt) {
  if (!attempt) return el("div", { class: "muted", text: "当前没有待出纸的纸张。" });
  const front = attempt.front_printed ? "已出" : "待出";
  const back = !attempt.required_back ? "留白" : (attempt.back_printed ? "已出" : "待出");
  const backPage = attempt.back_page === null ? "—" : attempt.back_page;
  return el("div", { class: "attempt" },
    el("b", { text: `第 ${attempt.sheet_no} 张` }),
    el("span", { text: `正面 第 ${attempt.front_page} 页 · ${front}` }),
    el("span", { text: `背面 第 ${backPage} 页 · ${back}` }),
  );
}

function renderPrinters() {
  const { printers, jobs } = state.data;
  const wrap = document.getElementById("printers");
  wrap.replaceChildren();

  for (const printer of printers) {
    const panel = el("div", { class: "printer" });
    const activeJob = printer.job_id ? jobs.find((j) => j.id === printer.job_id) : null;
    panel.append(el("div", { class: "head" },
      el("span", { class: "name", text: printer.name }),
      badge(printer.state === "idle" ? "queued" : (printer.state === "printing" ? "active" : "jammed"),
        printer.state === "idle" ? "空闲" : (printer.state === "printing" ? "打印中" : "卡纸待停稳")),
    ));

    if (printer.state === "idle") {
      panel.append(el("div", { class: "body", text: "空闲，可选择排队中的作业开始打印。" }));
      const queued = jobs.filter((j) => j.status === "queued");
      if (queued.length) {
        const select = el("select", { class: "small" });
        for (const job of queued) {
          select.append(el("option", { value: job.id, text: `#${job.id} 《${job.title}》 ${job.page_count} 页` }));
        }
        panel.append(el("div", { class: "actions" },
          select,
          el("button", { class: "small", type: "button", text: "开始打印",
            onclick: () => act(() => api("POST", `/api/jobs/${select.value}/start`, { printer_id: printer.id }), "已开始打印") }),
        ));
      }
    } else if (printer.state === "printing" && activeJob) {
      panel.append(
        el("div", { class: "body", text: `#${printer.job_id} 《${printer.job_title}》 · 第 ${printer.next_sheet}/${printer.total_sheets} 张` }),
        attemptLine(printer.attempt),
        el("div", { class: "actions" },
          el("button", { class: "small", type: "button", text: "出正面",
            disabled: !printer.attempt || printer.attempt.front_printed ? "" : null,
            onclick: () => act(() => api("POST", `/api/jobs/${printer.job_id}/print`, { side: "front", sheet_no: printer.next_sheet })) }),
          el("button", { class: "small", type: "button", text: "出背面",
            disabled: !printer.attempt || !printer.attempt.required_back || !printer.attempt.front_printed || printer.attempt.back_printed ? "" : null,
            onclick: () => act(() => api("POST", `/api/jobs/${printer.job_id}/print`, { side: "back", sheet_no: printer.next_sheet })) }),
          el("button", { class: "warn small", type: "button", text: "报告卡纸",
            onclick: () => act(() => api("POST", `/api/jobs/${printer.job_id}/jam`), "已报告卡纸，等待原机停稳") }),
        ),
      );
    } else if (printer.state === "jammed" && activeJob) {
      panel.append(
        el("div", { class: "body", text: `#${printer.job_id} 《${printer.job_title}》卡纸，半张已作废。` }),
        el("div", { class: "actions" },
          el("button", { class: "danger small", type: "button", text: "确认原机停稳",
            onclick: () => act(() => api("POST", `/api/jobs/${printer.job_id}/stop`), "已确认停稳，可换机续印") }),
        ),
      );
    }
    wrap.append(panel);
  }
}

// ---------------------------------------------------------------- 作业

function renderJobs() {
  const { jobs } = state.data;
  const wrap = document.getElementById("jobs");
  wrap.replaceChildren();
  if (!jobs.length) {
    wrap.append(el("p", { class: "muted", text: "还没有作业。先在左侧录入页面并提交入队。" }));
    return;
  }
  for (const job of jobs) {
    const card = el("div", { class: "job" });
    card.append(el("div", { class: "job-head" },
      el("div", {},
        el("span", { class: "job-title", text: `#${job.id} 《${job.title}》` }),
        el("span", { class: "muted", text: ` · ${job.page_count} 页 / ${job.total_sheets} 张` }),
      ),
      badge(job.status),
    ));

    const segments = el("div", { class: "segments" });
    for (const seg of job.segments) {
      segments.append(el("div", { class: `segment ${seg.status}` },
        el("div", { class: "seg-line" },
          el("b", { text: seg.kind === "initial" ? "初始派单" : "换机续单" }),
          el("span", { text: `${seg.printer_name} · 从第 ${seg.start_sheet} 张起` }),
          badge(seg.status),
          seg.note ? el("span", { class: "muted", text: seg.note }) : null,
        ),
      ));
    }

    card.append(el("div", { class: "job-body" },
      el("div", { class: "stats" },
        el("span", {}, "已完整出纸 ", el("b", { text: String(job.printed_sheets) }), " 张"),
        el("span", {}, "待处理张号 ", el("b", { text: job.current_sheet ? String(job.current_sheet) : "—" })),
        el("span", {}, "状态 ", el("b", { text: STATUS_TEXT[job.status] || job.status })),
      ),
      segments,
      job.collation && job.collation.length ? el("div", {},
        el("div", { class: "muted", text: "装订核对（完整出纸，按张号；按此顺序叠放即为原页序）：" }),
        el("div", { class: "collation" },
          ...job.collation.map((c) => el("span", { class: "sheet-chip" },
            el("b", { text: `第${c.sheet_no}张` }),
            ` 正${c.front_page}`,
            c.back_page === null ? " / 背留白" : ` / 背${c.back_page}`,
          )),
        ),
      ) : null,
      el("div", { class: "actions" },
        job.status === "stopped" ? el("button", { class: "small", type: "button", text: "换机派续单",
          onclick: () => act(() => api("POST", `/api/jobs/${job.id}/switch`, {}), "已派续单") }) : null,
        job.status === "jammed" ? el("span", { class: "muted", text: "原机停稳前不能派续单。" }) : null,
      ),
    ));
    wrap.append(card);
  }
}

// ---------------------------------------------------------------- 出纸记录

function renderRecords() {
  const tbody = document.getElementById("records");
  tbody.replaceChildren();
  const { records, jobs } = state.data;
  if (!records.length) {
    tbody.append(el("tr", {}, el("td", { class: "empty-row", colspan: "10", text: "暂无出纸记录。" })));
    return;
  }
  const titles = new Map(jobs.map((j) => [j.id, j.title]));
  for (const rec of records) {
    const frontMark = rec.front_printed ? "✓" : "—";
    const backMark = !rec.required_back ? "留白" : (rec.back_printed ? "✓" : "—");
    tbody.append(el("tr", { class: rec.status === "scrapped" ? "scrapped" : "" },
      el("td", { class: "mono", text: fmtTime(rec.started_at) }),
      el("td", { text: `#${rec.job_id} ${titles.get(rec.job_id) || ""}` }),
      el("td", { text: rec.printer_name }),
      el("td", { class: "mono", text: `第 ${rec.sheet_no} 张` }),
      el("td", { class: "mono", text: String(rec.front_page) }),
      el("td", { class: "mono", text: rec.back_page === null ? "—" : String(rec.back_page) }),
      el("td", { text: frontMark }),
      el("td", { text: backMark }),
      el("td", {}, badge(rec.status, STATUS_TEXT[rec.status] || rec.status)),
      el("td", { class: "muted", text: rec.scrap_reason || "—" }),
    ));
  }
}

function render() {
  if (!state.data) return;
  if (state.editingDocId !== null) {
    const still = state.data.documents.find((d) => d.id === state.editingDocId);
    if (!still) state.editingDocId = null;
  }
  renderDocuments();
  renderPrinters();
  renderJobs();
  renderRecords();
}

document.getElementById("doc-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("doc-title");
  const title = input.value.trim();
  if (!title) return;
  await act(async () => {
    const doc = await api("POST", "/api/documents", { title });
    state.editingDocId = doc.id;
    state.focusPage = true;
  });
  if (state.editingDocId !== null) input.value = "";
});

document.getElementById("refresh").addEventListener("click", () => refresh());

refresh();
setInterval(() => { if (!state.busy) refresh(); }, 5000);
