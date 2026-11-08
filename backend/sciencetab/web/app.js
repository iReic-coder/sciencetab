/* ScienceTab 前端交互：轮询状态、日志流、免刷新提交与取消、对比渲染。 */
(function () {
  "use strict";

  const meta = document.querySelector('meta[name="csrf-token"]');
  const CSRF = meta ? meta.content : "";
  const page = document.body.dataset.page || "";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  let toastTimer = null;
  function toast(message, isError) {
    let node = $(".toast");
    if (!node) {
      node = document.createElement("div");
      node.className = "toast";
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.className = "toast show" + (isError ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.className = "toast"; }, 4200);
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    opts.headers["X-CSRF-Token"] = CSRF;
    if (opts.body) opts.headers["Content-Type"] = "application/json";
    const resp = await fetch(path, opts);
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_e) { data = { raw: text }; }
    if (!resp.ok) {
      const err = new Error((data && (data.message || data.error)) || ("HTTP " + resp.status));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function badge(status) {
    const span = document.createElement("span");
    span.className = "badge s-" + status;
    span.textContent = status;
    return span;
  }

  function fmtDuration(value) {
    return value === null || value === undefined ? "—" : value + "s";
  }

  function fmtTime(value) {
    return value ? String(value).slice(0, 19).replace("T", " ") : "—";
  }

  /* 轮询：页面隐藏时暂停，回来立即刷新一次 */
  function poll(fn, ms) {
    let timer = null;
    let busy = false;
    async function tick() {
      if (document.hidden || busy) return;
      busy = true;
      try { await fn(); } catch (err) { /* 静默：下次再试 */ } finally { busy = false; }
    }
    function schedule() {
      clearInterval(timer);
      timer = setInterval(tick, ms);
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick();
    });
    schedule();
    tick();
    return { stop: () => clearInterval(timer) };
  }

  /* ---------- 运行列表 ---------- */
  function initRunsTable(table) {
    const statusFilter = document.querySelector("[data-filter-status]");
    const textFilter = document.querySelector("[data-filter-text]");
    const specFilter = document.querySelector("[data-filter-spec]");
    let runs = [];
    let lastSignature = null;

    function matches(run) {
      const status = statusFilter ? statusFilter.value : "";
      if (status === "active") {
        if (run.status !== "running") return false;
      } else if (status && run.status !== status) {
        return false;
      }
      if (specFilter && specFilter.value && run.spec_id !== specFilter.value) return false;
      const q = textFilter ? textFilter.value.trim().toLowerCase() : "";
      if (!q) return true;
      return (run.run_id + " " + run.spec_id + " " + run.status).toLowerCase().includes(q);
    }

    function renderRow(run, isNew) {
      const tr = document.createElement("tr");
      tr.dataset.runId = run.run_id;
      if (isNew) tr.classList.add("row-new");
      const cells = [
        { html: '<a class="mono" href="/runs/' + encodeURIComponent(run.run_id) + '">' + run.run_id + "</a>" },
        { text: run.spec_id },
        { node: badge(run.status) },
        { text: fmtDuration(run.duration_s), num: true },
        { text: fmtTime(run.finished_at || run.created_at), cls: "mono" },
        { html: run.status === "running"
            ? '<button class="ghost" data-cancel="' + run.run_id + '">取消</button>'
            : '<a class="muted" href="/compare?a=' + encodeURIComponent(run.run_id) + '&b=">对比</a>' },
      ];
      cells.forEach((cell) => {
        const td = document.createElement("td");
        if (cell.num) td.className = "num";
        if (cell.cls) td.className += " " + cell.cls;
        if (cell.node) td.appendChild(cell.node);
        else if (cell.html) td.innerHTML = cell.html;
        else td.textContent = cell.text;
        tr.appendChild(td);
      });
      return tr;
    }

    function render() {
      const body = table.tBodies[0];
      const visible = runs.filter(matches);
      const signature = [
        statusFilter ? statusFilter.value : "",
        textFilter ? textFilter.value : "",
        specFilter ? specFilter.value : "",
        visible.map((r) => [r.run_id, r.status, r.duration_s, r.finished_at].join("|")).join(";"),
      ].join("§");
      const counter = document.querySelector("[data-active-count]");
      if (counter) {
        const active = runs.filter((r) => r.status === "running").length;
        counter.textContent = active ? active + " 个运行中" : "空闲";
      }
      if (signature === lastSignature) return;
      lastSignature = signature;

      const existing = new Map($$("tr[data-run-id]", body).map((tr) => [tr.dataset.runId, tr]));
      const fragment = document.createDocumentFragment();
      visible.forEach((run) => {
        const row = renderRow(run, !existing.has(run.run_id));
        existing.delete(run.run_id);
        fragment.appendChild(row);
      });
      existing.forEach((tr) => tr.remove());
      body.innerHTML = "";
      body.appendChild(fragment);
      if (!visible.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = '<td colspan="6" class="muted">没有符合条件的运行</td>';
        body.appendChild(tr);
      }
    }

    poll(async () => {
      const data = await api("/api/runs" + (table.dataset.spec ? "?spec=" + encodeURIComponent(table.dataset.spec) : ""));
      runs = data.runs;
      render();
    }, 2500);

    if (statusFilter) statusFilter.addEventListener("change", render);
    if (specFilter) specFilter.addEventListener("change", render);
    if (textFilter) {
      textFilter.addEventListener("input", render);
      document.addEventListener("keydown", (ev) => {
        if (ev.key === "/" && document.activeElement !== textFilter) {
          ev.preventDefault();
          textFilter.focus();
        }
      });
    }
    table.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-cancel]");
      if (!btn) return;
      btn.disabled = true;
      btn.innerHTML = '<span class="spin"></span>';
      try {
        await api("/api/cancel", { method: "POST", body: JSON.stringify({ run_id: btn.dataset.cancel }) });
        toast("已发出取消请求");
      } catch (err) {
        toast("取消失败：" + err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "取消";
      }
    });
  }

  function initStartButtons() {
    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-start-spec]");
      if (!btn) return;
      const spec = btn.dataset.startSpec;
      btn.disabled = true;
      const original = btn.textContent;
      btn.innerHTML = '<span class="spin"></span> 启动中';
      try {
        const data = await api("/api/start", { method: "POST", body: JSON.stringify({ spec }) });
        toast("已提交：" + data.run_id);
        if (data.warning) toast(data.warning);
      } catch (err) {
        if (err.status === 409) toast("已有运行在进行：" + (err.data && err.data.running), true);
        else toast("启动失败：" + err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  }

  /* ---------- 运行详情 ---------- */
  function initRunDetail() {
    const root = document.querySelector("[data-run-id]");
    if (!root) return;
    const runId = root.dataset.runId;
    const statusNode = document.querySelector("[data-status]");
    const durationNode = document.querySelector("[data-duration]");
    const exitNode = document.querySelector("[data-exit]");
    const finishedNode = document.querySelector("[data-finished]");
    let streamStopped = false;
    const logPre = document.querySelector("[data-log]");
    let offset = logPre ? Number(logPre.dataset.logOffset || 0) : 0;

    poll(async () => {
      const data = await api("/api/runs/" + encodeURIComponent(runId));
      if (statusNode) {
        statusNode.innerHTML = "";
        statusNode.appendChild(badge(data.status));
      }
      if (durationNode) durationNode.textContent = fmtDuration(data.duration_s);
      if (exitNode) exitNode.textContent = data.exit_code === null || data.exit_code === undefined ? "—" : data.exit_code;
      if (finishedNode) finishedNode.textContent = fmtTime(data.finished_at);
      if (data.status !== "running") streamStopped = true;
      const cancelBtn = document.querySelector("[data-cancel-current]");
      if (cancelBtn && data.status !== "running") cancelBtn.remove();
      if (data.status === "running") streamLog();
    }, 2500);

    async function streamLog() {
      if (streamStopped) return;
      const pre = document.querySelector("[data-log]");
      if (!pre) return;
      const stream = document.querySelector("[data-log-stream]");
      const name = stream ? stream.value : "stdout";
      try {
        const data = await api("/api/runs/" + encodeURIComponent(runId) + "/log?stream=" + name + "&offset=" + offset);
        if (data.chunk) {
          pre.textContent += data.chunk;
          offset = data.offset;
          if (document.querySelector("[data-autoscroll]") && document.querySelector("[data-autoscroll]").checked) {
            pre.scrollTop = pre.scrollHeight;
          }
        }
      } catch (_err) { /* 忽略，下一轮再试 */ }
    }

    document.addEventListener("change", (ev) => {
      if (ev.target.matches("[data-log-stream]")) {
        offset = 0;
        const pre = document.querySelector("[data-log]");
        if (pre) pre.textContent = "";
        streamLog();
      }
    });

    const cancelBtn = document.querySelector("[data-cancel-current]");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", async () => {
        cancelBtn.disabled = true;
        try {
          await api("/api/cancel", { method: "POST", body: JSON.stringify({ run_id: runId }) });
          toast("已发出取消请求");
        } catch (err) {
          toast("取消失败：" + err.message, true);
        } finally {
          cancelBtn.disabled = false;
        }
      });
    }
    streamLog();
  }

  /* ---------- 标签页 ---------- */
  function initTabs() {
    const tabs = $$(".tabs button");
    if (!tabs.length) return;
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => {
          t.setAttribute("aria-selected", String(t === tab));
          const panel = document.getElementById(t.dataset.tab);
          if (panel) panel.hidden = t !== tab;
        });
        const target = tab.dataset.tab;
        history.replaceState(null, "", "#" + target);
      });
    });
    const hash = location.hash.replace("#", "");
    if (hash) {
      const tab = tabs.find((t) => t.dataset.tab === hash);
      if (tab) tab.click();
    }
  }

  /* ---------- 对比 ---------- */
  function initCompare() {
    const form = document.querySelector("[data-compare-form]");
    if (!form) return;
    const selA = form.querySelector("[name=a]");
    const selB = form.querySelector("[name=b]");
    const output = document.querySelector("[data-compare-output]");

    async function loadOptions() {
      const data = await api("/api/runs");
      [selA, selB].forEach((sel) => {
        const current = sel.value;
        sel.innerHTML = '<option value="">选择一次运行…</option>';
        data.runs.forEach((run) => {
          const option = document.createElement("option");
          option.value = run.run_id;
          option.textContent = run.run_id.slice(0, 15) + " · " + run.spec_id + " · " + run.status;
          sel.appendChild(option);
        });
        if (current) sel.value = current;
      });
    }

    async function run() {
      if (!selA.value || !selB.value) {
        output.innerHTML = '<p class="muted">选择两次运行后自动比较。</p>';
        return;
      }
      output.innerHTML = '<p class="muted"><span class="spin"></span> 比较中…</p>';
      try {
        const data = await api("/api/compare?a=" + encodeURIComponent(selA.value) + "&b=" + encodeURIComponent(selB.value));
        render(data);
        history.replaceState(null, "", "?a=" + encodeURIComponent(selA.value) + "&b=" + encodeURIComponent(selB.value));
      } catch (err) {
        output.innerHTML = '<p class="result-diff">比较失败：' + err.message + "</p>";
      }
    }

    function render(data) {
      const rows = [];
      data.comparable_identical.forEach((p) => rows.push([p, "一致", "result-same", ""]));
      (data.different_hashes || []).forEach((item) => rows.push([
        item.path + '<div class="hash">A ' + String(item.a).slice(0, 16) + "<br>B " +
          String(item.b).slice(0, 16) + "</div>",
        "不同", "result-diff", true,
      ]));
      data.comparable_different.forEach((p) => {
        if (!(data.different_hashes || []).some((item) => item.path === p)) {
          rows.push([p, "不同", "result-diff", ""]);
        }
      });
      data.only_in_a.forEach((p) => rows.push([p, "只在 A", "result-diff", ""]));
      data.only_in_b.forEach((p) => rows.push([p, "只在 B", "result-diff", ""]));
      const summary =
        '<div class="card"><div class="grid">' +
        '<div class="kv"><div class="k">同一规格</div><div class="v">' + (data.same_spec ? "是" : "否") + "</div></div>" +
        '<div class="kv"><div class="k">可比产物全部一致</div><div class="v ' +
          (data.all_comparable_identical ? "result-same" : "result-diff") + '">' +
          (data.all_comparable_identical ? "是" : "否") + "</div></div>" +
        '<div class="kv"><div class="k">A 验证</div><div class="v">' + (data.validation_a ? "通过" : "未通过/未登记") + "</div></div>" +
        '<div class="kv"><div class="k">B 验证</div><div class="v">' + (data.validation_b ? "通过" : "未通过/未登记") + "</div></div>" +
        "</div></div>";
      const table = rows.length
        ? '<table><thead><tr><th>产物</th><th>结果</th></tr></thead><tbody>' +
          rows.map((r) => '<tr><td class="mono">' + r[0] + '</td><td class="' + r[2] + '">' + r[1] + "</td></tr>").join("") +
          "</tbody></table>"
        : '<p class="muted">没有可比的共同产物。</p>';
      output.innerHTML = summary + table +
        '<p class="muted">图件与 run_log.txt 含生成时间，通常不同，不计入可比产物。</p>';
    }

    selA.addEventListener("change", run);
    selB.addEventListener("change", run);
    form.addEventListener("submit", (ev) => { ev.preventDefault(); run(); });
    loadOptions().then(run);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const table = document.querySelector("[data-runs-table]");
    if (page === "runs" || page === "spec") {
      if (table) initRunsTable(table);
      initStartButtons();
    }
    if (page === "run") initRunDetail();
    if (page === "compare") initCompare();
    initTabs();
  });
})();