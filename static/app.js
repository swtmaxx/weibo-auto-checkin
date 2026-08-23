(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const csrf = { value: null };
  let toastTimer = null;
  let pollTimer = null;
  let lastRunId = null;
  let storedClientSecretConfigured = false;
  let autoFilledOpenid = "";

  function showToast(message, isError) {
    const toast = $("#toast");
    if (!toast) return;
    toast.textContent = message || "";
    toast.classList.toggle("error", Boolean(isError));
    toast.classList.toggle("visible", Boolean(message));
    if (toastTimer) window.clearTimeout(toastTimer);
    if (message) toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 4200);
  }

  function setConnectionState(online) {
    const state = $("#connection-state");
    const indicator = $(".live-indicator");
    if (!state || !indicator) return;
    indicator.classList.toggle("is-offline", !online);
    state.textContent = online ? "连接正常" : "连接中断";
  }

  function markRefresh() {
    const label = $("#last-refresh");
    if (!label) return;
    label.textContent = "刚刚更新";
    label.dataset.updatedAt = String(Date.now());
  }

  function updateRefreshLabel() {
    const label = $("#last-refresh");
    if (!label || !label.dataset.updatedAt) return;
    const elapsed = Math.floor((Date.now() - Number(label.dataset.updatedAt)) / 1000);
    label.textContent = elapsed < 5 ? "刚刚更新" : elapsed < 60 ? elapsed + " 秒前更新" : Math.floor(elapsed / 60) + " 分钟前更新";
  }

  async function readResponse(response) {
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }
    if (!response.ok) {
      if (response.status === 401) window.location.href = "/";
      throw new Error(data.detail || "请求失败");
    }
    return data;
  }

  async function request(url, options) {
    const config = Object.assign({ credentials: "same-origin" }, options || {});
    config.headers = Object.assign({ "Accept": "application/json" }, config.headers || {});
    if (config.body && typeof config.body !== "string") {
      config.headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(config.body);
    }
    if (csrf.value && config.method && config.method !== "GET") {
      config.headers["X-CSRF-Token"] = csrf.value;
    }
    let response;
    try {
      response = await fetch(url, config);
    } catch (error) {
      setConnectionState(false);
      throw error;
    }
    setConnectionState(true);
    markRefresh();
    return readResponse(response);
  }

  function bindAuthForms() {
    const setupForm = $("#setup-form");
    if (setupForm) {
      setupForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const password = $("#setup-password").value;
        const confirm = $("#setup-confirm").value;
        const error = $("#setup-error");
        error.textContent = "";
        if (password !== confirm) {
          error.textContent = "两次输入的密码不一致";
          return;
        }
        try {
          const data = await request("/api/auth/setup", {
            method: "POST",
            body: { password: password }
          });
          csrf.value = data.csrf_token;
          window.location.href = "/";
        } catch (err) {
          error.textContent = err.message;
        }
      });
    }

    const loginForm = $("#login-form");
    if (loginForm) {
      loginForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const error = $("#login-error");
        error.textContent = "";
        try {
          const data = await request("/api/auth/login", {
            method: "POST",
            body: { password: $("#login-password").value }
          });
          csrf.value = data.csrf_token;
          window.location.href = "/";
        } catch (err) {
          error.textContent = err.message;
        }
      });
    }
  }

  function formatDate(value) {
    if (!value) return "-";
    return value.replace("T", " ").replace("+00:00", " UTC");
  }

  function accountBadge(account) {
    const badge = $("#account-badge");
    if (!badge) return;
    badge.className = "badge";
    if (!account.configured) {
      badge.classList.add("badge-neutral");
      badge.textContent = "未配置";
    } else if (account.logged_in) {
      badge.classList.add("badge-green");
      badge.textContent = "登录有效";
    } else {
      badge.classList.add("badge-amber");
      badge.textContent = "待验证";
    }
  }

  async function loadAccount() {
    const account = await request("/api/account");
    accountBadge(account);
    $("#account-state").textContent = !account.configured ? "未配置" : account.logged_in ? "已登录" : "待验证";
    $("#account-detail").textContent = account.last_verified_at ? "验证于 " + formatDate(account.last_verified_at) : "需要导入 Cookie";
    $("#account-name").textContent = account.login_name || (account.configured ? "Cookie 已导入" : "尚未导入 Cookie");
    $("#account-message").textContent = account.verification_message || "Cookie 只会以加密形式保存在服务器。";
  }

  function statusLabel(status) {
    const map = {
      available: ["可签到", "badge-blue"],
      signed: ["已签到", "badge-green"],
      unknown: ["未知", "badge-neutral"]
    };
    return map[status] || map.unknown;
  }

  function renderTopics(topics) {
    const body = $("#topics-body");
    body.replaceChildren();
    $("#topic-count").textContent = String(topics.length);
    const selected = topics.filter((topic) => topic.enabled).length;
    $("#selected-count").textContent = selected + " 个已启用";
    if (!topics.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.className = "empty-cell";
      cell.textContent = "尚未同步超话列表";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    topics.forEach((topic) => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const name = document.createElement("div");
      name.className = "topic-name";
      name.textContent = topic.name;
      const description = document.createElement("div");
      description.className = "topic-description";
      description.textContent = topic.description || topic.topic_key;
      nameCell.append(name, description);

      const stateCell = document.createElement("td");
      const state = document.createElement("span");
      const stateData = statusLabel(topic.remote_status);
      state.className = "badge " + stateData[1];
      state.textContent = stateData[0];
      stateCell.appendChild(state);

      const resultCell = document.createElement("td");
      resultCell.className = "muted";
      resultCell.textContent = topic.last_result || "-";

      const actionCell = document.createElement("td");
      actionCell.className = "align-right";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.className = "topic-toggle";
      toggle.checked = Boolean(topic.enabled);
      toggle.setAttribute("aria-label", "启用 " + topic.name);
      toggle.addEventListener("change", async () => {
        toggle.disabled = true;
        try {
          await request("/api/topics/" + encodeURIComponent(topic.topic_key), {
            method: "PATCH",
            body: { enabled: toggle.checked }
          });
          await loadTopics();
          showToast(toggle.checked ? "已启用 " + topic.name : "已停用 " + topic.name, false);
        } catch (err) {
          toggle.checked = !toggle.checked;
          showToast(err.message, true);
        } finally {
          toggle.disabled = false;
        }
      });
      actionCell.appendChild(toggle);
      row.append(nameCell, stateCell, resultCell, actionCell);
      body.appendChild(row);
    });
  }

  async function loadTopics() {
    const data = await request("/api/topics");
    renderTopics(data.topics || []);
  }

  function renderLogs(logs) {
    const container = $("#logs");
    container.replaceChildren();
    if (!logs || !logs.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "任务日志会显示在这里";
      container.appendChild(empty);
      return;
    }
    logs.forEach((log) => {
      const line = document.createElement("div");
      line.className = "log-line log-" + String(log.level || "info").toLowerCase();
      const time = document.createElement("span");
      time.textContent = formatDate(log.created_at);
      const level = document.createElement("span");
      level.textContent = log.level;
      const message = document.createElement("span");
      message.textContent = log.message;
      line.append(time, level, message);
      container.appendChild(line);
    });
    container.scrollTop = container.scrollHeight;
  }

  function renderRun(run) {
    const state = $("#task-state");
    const detail = $("#task-detail");
    const summary = $("#task-summary");
    const cancel = $("#cancel-button");
    if (!run) {
      state.className = "metric-value status-value status-idle";
      state.textContent = "空闲";
      detail.textContent = "";
      cancel.classList.add("hidden");
      summary.className = "task-summary empty-state";
      summary.textContent = "暂无运行中的任务";
      return;
    }
    lastRunId = run.id;
    const running = run.status === "running" || run.status === "queued";
    const labels = {
      queued: ["排队中", "status-queued"],
      running: ["执行中", "status-running"],
      completed: ["已完成", "status-completed"],
      failed: ["失败", "status-failed"],
      cancelled: ["已取消", "status-cancelled"]
    };
    const label = labels[run.status] || [run.status, "status-idle"];
    state.className = "metric-value status-value " + label[1];
    state.textContent = label[0];
    detail.textContent = "任务 #" + run.id;
    cancel.classList.toggle("hidden", !running);
    summary.className = "task-summary";
    const title = document.createElement("strong");
    title.textContent = (run.kind === "checkin" ? "立即签到" : "同步超话") + " · " + label[0];
    const info = document.createElement("span");
    const data = run.summary || {};
    info.textContent = running
      ? "开始于 " + formatDate(run.started_at || run.created_at)
      : "结果：成功 " + (data.success || 0) + "，已签到 " + (data.already || 0) + "，失败 " + (data.failed || 0);
    summary.replaceChildren(title, info);
    renderLogs(run.logs || []);
  }

  async function loadCurrentTask() {
    const data = await request("/api/tasks/current");
    renderRun(data.run);
    if (data.run && (data.run.status === "running" || data.run.status === "queued")) {
      if (!pollTimer) pollTimer = window.setTimeout(async () => {
        pollTimer = null;
        await loadCurrentTask().catch((err) => showToast(err.message, true));
      }, 1400);
    } else if (lastRunId) {
      await loadHistory();
    }
  }

  async function loadHistory() {
    const data = await request("/api/history?limit=20");
    const body = $("#history-body");
    body.replaceChildren();
    if (!data.runs || !data.runs.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "empty-cell";
      cell.textContent = "暂无记录";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    data.runs.forEach((run) => {
      const row = document.createElement("tr");
      const date = document.createElement("td");
      date.textContent = formatDate(run.created_at);
      const kind = document.createElement("td");
      kind.textContent = run.kind;
      const state = document.createElement("td");
      const stateBadge = document.createElement("span");
      const stateClass = {
        completed: "badge-green",
        failed: "badge-red",
        cancelled: "badge-amber",
        running: "badge-blue",
        queued: "badge-blue"
      }[run.status] || "badge-neutral";
      const stateLabel = {
        completed: "已完成",
        failed: "失败",
        cancelled: "已取消",
        running: "执行中",
        queued: "排队中"
      }[run.status] || run.status;
      stateBadge.className = "badge " + stateClass;
      stateBadge.textContent = stateLabel;
      state.appendChild(stateBadge);
      const result = document.createElement("td");
      const summary = run.summary || {};
      result.textContent = run.error || ("成功 " + (summary.success || 0) + " · 已签到 " + (summary.already || 0) + " · 失败 " + (summary.failed || 0));
      const action = document.createElement("td");
      action.className = "align-right";
      const link = document.createElement("button");
      link.className = "button button-ghost";
      link.type = "button";
      link.textContent = "查看";
      link.addEventListener("click", async () => {
        try {
          const detail = await request("/api/history/" + run.id);
          renderRun(detail);
          window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
        } catch (err) {
          showToast(err.message, true);
        }
      });
      action.appendChild(link);
      row.append(date, kind, state, result, action);
      body.appendChild(row);
    });
  }

  async function loadSchedule() {
    const data = await request("/api/schedule");
    $("#schedule-enabled").checked = Boolean(data.enabled);
    $("#schedule-time").value = data.run_time;
    $("#timezone-label").textContent = data.timezone;
    $("#schedule-state").textContent = data.enabled ? data.run_time : "未启用";
    $("#schedule-detail").textContent = data.last_run_date ? "上次运行 " + data.last_run_date : data.timezone;
  }

  function renderCooldown(cooldown) {
    const badge = $("#cooldown-badge");
    const detail = $("#cooldown-detail");
    if (!badge || !detail) return;
    badge.className = "badge " + (cooldown && cooldown.active ? "badge-red" : "badge-green");
    badge.textContent = cooldown && cooldown.active ? "冷却中" : "未冷却";
    detail.className = "notice " + (cooldown && cooldown.active ? "notice-warning" : "notice-neutral");
    detail.textContent = cooldown && cooldown.active
      ? "已暂停签到，截止 " + formatDate(cooldown.until) + "。原因：" + (cooldown.reason || "微博返回限流或风控响应")
      : "当前没有冷却。";
  }

  function renderNotificationState(notification) {
    const badge = $("#notification-badge");
    if (!badge) return;
    const configured = Boolean(
      notification.app_id && notification.user_openid && notification.client_secret_configured
    );
    badge.className = "badge";
    if (!configured) {
      badge.classList.add("badge-neutral");
      badge.textContent = "未配置";
    } else if (notification.enabled) {
      badge.classList.add("badge-green");
      badge.textContent = "已启用";
    } else {
      badge.classList.add("badge-amber");
      badge.textContent = "已停用";
    }
  }

  function renderQQListenerState(listener) {
    const detail = $("#qq-listener-status");
    if (!detail) return;
    const state = listener || {};
    let message = "监听未启用。";
    let className = "notice notice-neutral";
    if (state.enabled && state.configured && state.connected) {
      message = "已连接 QQ Gateway，等待 C2C 私聊事件。";
      className = "notice notice-success";
    } else if (state.enabled && state.configured) {
      message = state.last_error ? "正在重连 QQ Gateway：" + state.last_error : "正在连接 QQ Gateway。";
      className = "notice notice-warning";
    } else if (state.enabled) {
      message = "监听已开启，请填写 AppID 和 ClientSecret 后保存。";
      className = "notice notice-warning";
    }
    detail.className = className;
    detail.textContent = message;
  }

  function renderQQOpenids(openids) {
    const container = $("#qq-openid-list");
    if (!container) return;
    container.replaceChildren();
    if (!openids || !openids.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "尚未发现 OpenID。";
      container.appendChild(empty);
      return;
    }
    openids.forEach((item) => {
      const row = document.createElement("div");
      row.className = "openid-item";
      const value = document.createElement("code");
      value.className = "openid-value";
      value.textContent = item.user_openid || "";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button button-ghost button-small";
      button.textContent = "使用";
      button.addEventListener("click", () => {
        $("#qq-user-openid").value = item.user_openid || "";
        showToast("已填入 user_openid，请保存设置", false);
      });
      row.append(value, button);
      container.appendChild(row);
    });
    const firstOpenid = openids[0] && openids[0].user_openid;
    const input = $("#qq-user-openid");
    if (firstOpenid && input && !input.value.trim()) {
      input.value = firstOpenid;
      if (autoFilledOpenid !== firstOpenid) {
        autoFilledOpenid = firstOpenid;
        showToast("已自动填入 user_openid，请勾选启用通知并保存设置", false);
      }
    }
  }

  async function refreshQQOpenids() {
    try {
      const data = await request("/api/qq/openids");
      renderQQListenerState(data.listener || {});
      renderQQOpenids(data.openids || []);
    } catch (_) {
      // The regular page load reports authentication and network errors.
    }
  }

  async function loadSettings() {
    const data = await request("/api/settings");
    const policy = data.runtime || {};
    const notification = data.notifications || {};
    $("#checkin-delay").value = policy.checkin_delay_seconds;
    $("#max-topics").value = policy.max_topics_per_run;
    $("#max-failures").value = policy.max_consecutive_failures;
    $("#request-timeout").value = policy.request_timeout_seconds;
    $("#read-retries").value = policy.read_retry_count;
    $("#cooldown-enabled").checked = Boolean(policy.cooldown_on_rate_limit);
    $("#notifications-enabled").checked = Boolean(notification.enabled);
    $("#qq-app-id").value = notification.app_id || "";
    $("#qq-user-openid").value = notification.user_openid || "";
    $("#qq-client-secret").value = "";
    storedClientSecretConfigured = Boolean(notification.client_secret_configured);
    $("#clear-client-secret").checked = false;
    $("#notify-completed").checked = Boolean(notification.notify_completed);
    $("#notify-failed").checked = Boolean(notification.notify_failed);
    $("#notify-risk").checked = Boolean(notification.notify_risk);
    $("#listen-events").checked = Boolean(notification.listen_events);
    renderCooldown(data.cooldown || {});
    renderNotificationState(notification);
    renderQQListenerState(data.qq_listener || {});
    renderQQOpenids(data.qq_openids || []);
  }

  function settingsPayload() {
    return {
      runtime: {
        checkin_delay_seconds: Number($("#checkin-delay").value),
        max_topics_per_run: Number($("#max-topics").value),
        max_consecutive_failures: Number($("#max-failures").value),
        request_timeout_seconds: Number($("#request-timeout").value),
        read_retry_count: Number($("#read-retries").value),
        cooldown_on_rate_limit: $("#cooldown-enabled").checked
      },
      notifications: {
        enabled: $("#notifications-enabled").checked,
        app_id: $("#qq-app-id").value.trim(),
        user_openid: $("#qq-user-openid").value.trim(),
        client_secret: $("#qq-client-secret").value.trim() || null,
        clear_client_secret: $("#clear-client-secret").checked,
        notify_completed: $("#notify-completed").checked,
        notify_failed: $("#notify-failed").checked,
        notify_risk: $("#notify-risk").checked,
        listen_events: $("#listen-events").checked
      }
    };
  }

  function bindSettings() {
    const form = $("#settings-form");
    if (!form) return;
    const passwordForm = $("#password-form");
    passwordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#change-password-button");
      const currentPassword = $("#current-password").value;
      const newPassword = $("#new-password").value;
      const confirmPassword = $("#confirm-password").value;
      if (newPassword !== confirmPassword) {
        showToast("两次输入的新密码不一致", true);
        return;
      }
      button.disabled = true;
      try {
        await request("/api/auth/password", {
          method: "POST",
          body: {
            current_password: currentPassword,
            new_password: newPassword,
            confirm_password: confirmPassword
          }
        });
        window.location.href = "/";
      } catch (err) {
        showToast(err.message, true);
      } finally {
        button.disabled = false;
      }
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#save-settings-button");
      button.disabled = true;
      try {
        const data = await request("/api/settings", {
          method: "PUT",
          body: settingsPayload()
        });
        renderCooldown(data.cooldown || {});
        renderNotificationState(data.notifications || {});
        renderQQListenerState(data.qq_listener || {});
        renderQQOpenids(data.qq_openids || []);
        storedClientSecretConfigured = Boolean(data.notifications && data.notifications.client_secret_configured);
        $("#qq-client-secret").value = "";
        $("#clear-client-secret").checked = false;
        showToast("设置已保存，将从下一次任务生效", false);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        button.disabled = false;
      }
    });
    $("#discover-openid-button").addEventListener("click", async () => {
      const button = $("#discover-openid-button");
      const appId = $("#qq-app-id").value.trim();
      const clientSecret = $("#qq-client-secret").value.trim();
      if (!appId) {
        showToast("请先填写 AppID", true);
        return;
      }
      if (!clientSecret && !storedClientSecretConfigured) {
        showToast("请先填写 ClientSecret", true);
        return;
      }
      button.disabled = true;
      try {
        const data = await request("/api/qq/discovery", {
          method: "POST",
          body: { app_id: appId, client_secret: clientSecret || null }
        });
        storedClientSecretConfigured = Boolean(data.notifications && data.notifications.client_secret_configured);
        $("#listen-events").checked = true;
        renderQQListenerState(data.qq_listener || {});
        renderQQOpenids(data.qq_openids || []);
        showToast("已开始发现，请给 QQ 机器人发送一条私聊消息", false);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        button.disabled = false;
      }
    });
    $("#reset-settings-button").addEventListener("click", async () => {
      if (!window.confirm("恢复默认设置并清除 QQ 通知配置？")) return;
      try {
        const data = await request("/api/settings/reset", { method: "POST" });
        renderCooldown(data.cooldown || {});
        renderNotificationState(data.notifications || {});
        await loadSettings();
        showToast("已恢复默认设置", false);
      } catch (err) {
        showToast(err.message, true);
      }
    });
    $("#test-notification-button").addEventListener("click", async () => {
      const button = $("#test-notification-button");
      button.disabled = true;
      try {
        await request("/api/notifications/test", { method: "POST" });
        showToast("测试通知已发送", false);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        button.disabled = false;
      }
    });
    $("#clear-client-secret").addEventListener("change", (event) => {
      $("#qq-client-secret").disabled = event.target.checked;
      if (event.target.checked) $("#qq-client-secret").value = "";
    });
    $("#logout-button").addEventListener("click", async () => {
      try {
        await request("/api/auth/logout", { method: "POST" });
        window.location.href = "/";
      } catch (err) {
        showToast(err.message, true);
      }
    });
    window.setInterval(() => {
      const clock = $("#clock");
      if (clock) clock.textContent = new Date().toLocaleString("zh-CN", { hour12: false });
    }, 1000);
    window.setInterval(refreshQQOpenids, 5000);
  }

  async function startTask(endpoint, label, buttonSelector, workingLabel) {
    const button = $(buttonSelector);
    if (button) {
      button.disabled = true;
      button.classList.add("is-loading");
      button.textContent = workingLabel;
    }
    try {
      await request(endpoint, { method: "POST" });
      showToast(label + "已开始", false);
      await loadCurrentTask();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove("is-loading");
        button.textContent = label;
      }
    }
  }

  async function loadDashboard() {
    const status = await request("/api/status");
    csrf.value = status.csrf_token;
    await Promise.all([loadAccount(), loadTopics(), loadSchedule(), loadHistory(), loadCurrentTask()]);
  }

  function bindDashboard() {
    if (!$("#cookie-form")) return;
    $("#cookie-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = $("#cookie-input").value.trim();
      if (!value) {
        showToast("请粘贴 Cookie", true);
        return;
      }
      try {
        await request("/api/account/cookie", { method: "POST", body: { cookie: value } });
        $("#cookie-input").value = "";
        await loadAccount();
        showToast("Cookie 已加密保存", false);
      } catch (err) {
        showToast(err.message, true);
      }
    });
    $("#verify-button").addEventListener("click", async () => {
      try {
        await request("/api/account/verify", { method: "POST" });
        await loadAccount();
        showToast("Cookie 验证成功", false);
      } catch (err) {
        showToast(err.message, true);
        await loadAccount().catch(() => {});
      }
    });
    $("#clear-cookie-button").addEventListener("click", async () => {
      if (!window.confirm("确定清除当前 Cookie？")) return;
      try {
        await request("/api/account/cookie", { method: "DELETE" });
        await loadAccount();
        showToast("Cookie 已清除", false);
      } catch (err) {
        showToast(err.message, true);
      }
    });
    $("#sync-button").addEventListener("click", () => startTask("/api/topics/sync", "同步超话", "#sync-button", "同步中…"));
    $("#checkin-button").addEventListener("click", () => startTask("/api/tasks/checkin", "立即签到", "#checkin-button", "签到中…"));
    $("#cancel-button").addEventListener("click", async () => {
      const button = $("#cancel-button");
      button.disabled = true;
      try {
        await request("/api/tasks/cancel", { method: "POST" });
        showToast("已发出取消请求", false);
        await loadCurrentTask();
      } catch (err) {
        showToast(err.message, true);
      } finally {
        button.disabled = false;
      }
    });
    $("#schedule-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await request("/api/schedule", {
          method: "PUT",
          body: {
            enabled: $("#schedule-enabled").checked,
            run_time: $("#schedule-time").value
          }
        });
        await loadSchedule();
        showToast("每日计划已保存", false);
      } catch (err) {
        showToast(err.message, true);
      }
    });
    $("#refresh-history-button").addEventListener("click", () => loadHistory().catch((err) => showToast(err.message, true)));
    $("#logout-button").addEventListener("click", async () => {
      try {
        await request("/api/auth/logout", { method: "POST" });
        window.location.href = "/";
      } catch (err) {
        showToast(err.message, true);
      }
    });
    window.setInterval(() => {
      const clock = $("#clock");
      if (clock) clock.textContent = new Date().toLocaleString("zh-CN", { hour12: false });
    }, 1000);
    window.setInterval(updateRefreshLabel, 5000);
  }

  bindAuthForms();
  bindDashboard();
  bindSettings();
  if ($("#cookie-form")) {
    loadDashboard().catch((err) => showToast(err.message, true));
  }
  if ($("#settings-form")) {
    request("/api/status")
      .then((status) => {
        csrf.value = status.csrf_token;
        return loadSettings();
      })
      .catch((err) => showToast(err.message, true));
  }
})();
