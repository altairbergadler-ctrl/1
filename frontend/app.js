const app = document.querySelector("#app");
const toast = document.querySelector("#toast");

const state = {
  authenticated: false,
  currentUser: null,
  csrfToken: null,
  recoveryCsrfToken: null,
  playerSecret: null,
  statusFilter: "ALL",
  qobuzWatchToken: 0,
  yandexWatchToken: 0,
  acquisitionWatchToken: 0,
};

const statusLabels = {
  READY: "Готов",
  NEEDS_REVIEW: "Проверить",
  MISSING: "Нет в архиве",
  UNMATCHED: "Не обработан",
};

const qobuzDownloadLabels = {
  queued: "В очереди",
  searching: "Поиск в Qobuz",
  downloading: "Скачивается",
  downloaded: "Скачан",
  stored: "В хранилище",
  conflict: "Уже существует",
  not_found: "Не найден в Qobuz",
  ambiguous: "Нужно выбрать версию",
  failed: "Ошибка загрузки",
};

const qobuzPhaseLabels = {
  queued: "Задание поставлено в очередь",
  downloading: "Поиск и загрузка треков",
  batch_pause: "Пауза перед следующей пачкой",
  batch_draining: "Сохраняем пачку перед продолжением",
  batch_scanning: "Добавляем пачку в каталог",
  batch_replicating: "Выгружаем пачку в Google Drive",
  importing: "Перенос файлов в хранилище",
  scanning: "Обновление каталога",
  paused: "Загрузка безопасно приостановлена",
  matching: "Сопоставление с плейлистом",
  completed: "Загрузка завершена",
};

const acquisitionPhaseLabels = {
  queued: "В очереди",
  provider_check: "Проверка Qobuz и Яндекс Музыки",
  importing: "Импорт файлов",
  scanning: "Сканирование каталога",
  drive_upload: "Загрузка в Google Drive и локальная очистка",
  batch_pause: "Пауза между пачками",
  paused: "Приостановлено после пачки",
  final_matching: "Финальное сопоставление",
  completed: "Все пачки завершены",
};

const yandexDownloadLabels = {
  ...qobuzDownloadLabels,
  searching: "Поиск в Яндекс Музыке",
  not_found: "Не найден в Яндекс Музыке",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, timeout = 4200) {
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.hidden = true;
  }, timeout);
}

async function api(path, options = {}) {
  const {
    allowUnauthorized = false,
    csrfToken = state.csrfToken,
    ...requestOptions
  } = options;
  const headers = new Headers(requestOptions.headers || {});
  const method = String(requestOptions.method || "GET").toUpperCase();
  if (requestOptions.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...requestOptions,
    credentials: "same-origin",
    headers,
  });
  if (response.status === 401 && !allowUnauthorized) {
    state.authenticated = false;
    state.currentUser = null;
    state.csrfToken = null;
    state.playerSecret = null;
    renderLogin();
    throw new Error("Сессия завершена. Войдите снова.");
  }
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      const data = await response.json();
      const detail = data.detail;
      message = typeof detail === "string" ? detail : detail?.message || message;
    } catch {
      // The status is sufficient when a proxy returned a non-JSON error page.
    }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function shell(content) {
  const ownerLinks = state.currentUser?.role === "owner" ? `
    <a class="button ghost small" href="#/users">Пользователи</a>
    <a class="button ghost small" href="#/storage">Хранилище</a>
    <a class="button ghost small" href="#/providers">Провайдеры</a>
  ` : "";
  const identity = state.currentUser?.display_name || state.currentUser?.email || "";
  return `
    <div class="shell">
      <header class="topbar">
        <a class="brand" href="#/playlists" aria-label="К списку плейлистов">
          <span class="brand-mark" aria-hidden="true">●</span>
          <span>
            <span class="brand-name">Lossless Archive</span>
            <span class="brand-subtitle">bit-perfect library</span>
          </span>
        </a>
        <nav class="nav-actions" aria-label="Основная навигация">
          <span class="current-user" title="Текущий пользователь">${escapeHtml(identity)}</span>
          <a class="button ghost small" href="#/playlists">Плейлисты</a>
          <a class="button ghost small" href="#/players">Плееры</a>
          <a class="button ghost small" href="#/notifications">Уведомления</a>
          ${ownerLinks}
          <a class="button ghost small" href="#/review">Review</a>
          <button class="ghost small" type="button" data-action="logout">Выйти</button>
        </nav>
      </header>
      ${content}
    </div>
  `;
}

function loadingPage(label = "Загружаем фонотеку") {
  app.innerHTML = shell(`
    <main>
      <section class="page-header">
        <div>
          <p class="eyebrow">LOSSLESS ARCHIVE</p>
          <h1>${escapeHtml(label)}</h1>
        </div>
      </section>
      <div class="loading-bar" aria-label="Загрузка"></div>
    </main>
  `);
}

function base64UrlToBytes(value) {
  const normalized = String(value).replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

async function browserPushSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

async function renderNotifications() {
  loadingPage("Уведомления");
  try {
    const status = await api("/api/push");
    const subscription = await browserPushSubscription();
    const supported = "Notification" in window && "PushManager" in window;
    const permission = supported ? Notification.permission : "unsupported";
    const deviceState = !supported
      ? "не поддерживается"
      : subscription
        ? "подключён"
        : permission === "denied"
          ? "запрещён в браузере"
          : "не подключён";
    const accountStatus = status.configured
      ? "Активных подписок аккаунта: " + status.subscriptions.length
      : "Web Push пока не настроен на сервере.";
    app.innerHTML = shell(
      '<main><section class="page-header"><div>'
      + '<p class="eyebrow">WEB PUSH</p><h1>Уведомления</h1>'
      + '<p class="lede">Одно уведомление приходит только после завершения всех пачек и финального сопоставления.</p>'
      + '</div></section><section class="card"><h2>Этот браузер</h2>'
      + '<p>Статус: <strong>' + escapeHtml(deviceState) + '</strong></p>'
      + '<p class="muted">Для iPhone/iPad установите сайт на экран «Домой», затем включите уведомления здесь.</p>'
      + '<div class="action-row">'
      + '<button type="button" data-action="push-enable" '
      + (!status.configured || !supported || permission === "denied" || subscription ? "disabled" : "")
      + '>Включить уведомления</button>'
      + '<button class="secondary" type="button" data-action="push-disable" '
      + (subscription ? "" : "disabled")
      + '>Отключить на этом устройстве</button></div>'
      + '<p class="muted">' + escapeHtml(accountStatus) + '</p></section></main>'
    );
  } catch (exception) {
    showToast(exception.message);
  }
}

async function enablePushNotifications(button) {
  button.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("Браузер не разрешил уведомления");
    const status = await api("/api/push");
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToBytes(status.public_key),
    });
    const payload = subscription.toJSON();
    payload.user_agent_label = navigator.userAgent.slice(0, 128);
    await api("/api/push/subscriptions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast("Уведомления включены");
    await renderNotifications();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function disablePushNotifications(button) {
  button.disabled = true;
  try {
    const subscription = await browserPushSubscription();
    if (subscription) {
      await api("/api/push/subscriptions/unsubscribe", {
        method: "POST",
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      });
      await subscription.unsubscribe();
    }
    showToast("Уведомления отключены");
    await renderNotifications();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

function renderLogin(message = "") {
  app.innerHTML = `
    <main class="login-page">
      <section class="login-panel">
        <p class="eyebrow">ВАША ФОНОТЕКА · БЕЗ ПОТЕРЬ</p>
        <h1>Музыка уже дома.</h1>
        <p class="lede">Войдите через Google — новый аккаунт будет зарегистрирован автоматически с ролью пользователя. Пароль и токены Google обрабатываются только на стороне Google.</p>
        <a class="button google-sign-in" href="/api/auth/google/start">Войти через Google</a>
        <p class="form-error" role="alert">${escapeHtml(message)}</p>
        <p class="muted login-help">Владелец управляет системными настройками, обычный пользователь — только своей фонотекой.</p>
        <a class="recovery-link" href="#/recovery">Аварийное восстановление владельца</a>
      </section>
    </main>
  `;
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // Rendering the login page is the desired fallback even if the session expired.
  }
  state.authenticated = false;
  state.currentUser = null;
  state.csrfToken = null;
  state.playerSecret = null;
  window.location.hash = "";
  renderLogin();
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString("ru-RU") : "—";
}

async function renderPlayers() {
  loadingPage("Настраиваем плееры");
  try {
    const data = await api("/api/player-credentials");
    const secret = state.playerSecret;
    app.innerHTML = shell(`
      <main>
        <section class="page-header">
          <div>
            <p class="eyebrow">OPENSubsonic · SYMFONIUM</p>
            <h1>Плееры</h1>
            <p class="lede">Отдельный ключ для каждого телефона. Отзыв ключа блокирует новые запросы, но не удаляет уже скачанную музыку.</p>
          </div>
        </section>
        ${secret ? `
          <section class="card player-secret-card" aria-live="polite">
            <h2>Сохраните ключ сейчас</h2>
            <p>После ухода со страницы ключ больше не показывается.</p>
            <label>Адрес сервера
              <span class="copy-row"><input readonly value="${escapeHtml(secret.server_url)}"><button type="button" data-action="copy-player-value" data-copy="server">Копировать</button></span>
            </label>
            <label>API Key
              <span class="copy-row"><input readonly value="${escapeHtml(secret.api_key)}"><button type="button" data-action="copy-player-value" data-copy="key">Копировать</button></span>
            </label>
          </section>` : ""}
        <section class="card">
          <h2>Новый ключ</h2>
          <form id="player-credential-form" class="inline-form">
            <label>Название устройства<input name="label" maxlength="128" required placeholder="Мой телефон"></label>
            <button type="submit">Создать ключ</button>
            <p class="form-error" role="alert"></p>
          </form>
        </section>
        <section class="card player-help">
          <h2>Настройка Symfonium</h2>
          <ol>
            <li>Добавьте провайдер OpenSubsonic и укажите полный HTTPS-адрес сервера.</li>
            <li>Выберите API Key. Legacy auth и Compatibility mode оставьте выключенными.</li>
            <li>Включите Ignore server transcoding / Original.</li>
            <li>Импортируйте плейлист в режиме Read only и настройте automatic offline cache.</li>
          </ol>
        </section>
        <section class="card">
          <div class="card-heading"><h2>Выданные ключи</h2>${data.items.some((row) => !row.revoked_at) ? '<button class="danger ghost" type="button" data-action="player-revoke-all">Отозвать все</button>' : ""}</div>
          ${data.items.length ? `<div class="credential-list">${data.items.map((row) => `
            <article class="credential-row">
              <div><strong>${escapeHtml(row.label)}</strong><p class="muted">Создан: ${formatDate(row.created_at)} · Последнее использование: ${formatDate(row.last_used_at)}</p></div>
              ${row.revoked_at ? '<span class="status-pill disabled">Отозван</span>' : `<button class="danger ghost" type="button" data-act�}���$z{-���jםnull) {
  let consecutiveFetchFailures = 0;
  for (;;) {
    let job;
    try {
      job = await api(`/api/jobs/${jobId}`);
      if (consecutiveFetchFailures > 0) {
        showToast("Связь восстановлена. Задание продолжается…", 5000);
        consecutiveFetchFailures = 0;
      }
    } catch (exception) {
      // A long Qobuz download keeps running in Celery even if nginx/backend is
      // briefly unavailable. Do not turn one failed poll into a false job
      // failure: reconnect for about a minute and keep polling the same job.
      if (!(exception instanceof TypeError)) throw exception;
      consecutiveFetchFailures += 1;
      if (consecutiveFetchFailures === 1) {
        showToast("Связь с сервисом прервалась. Задание продолжает работу, переподключаемся…", 10_000);
      }
      if (consecutiveFetchFailures >= 12) {
        throw new Error("Нет связи с сервисом. Задание может продолжаться в фоне — обновите страницу через минуту.");
      }
      const retryDelay = Math.min(1000 * (2 ** (consecutiveFetchFailures - 1)), 5000);
      await new Promise((resolve) => window.setTimeout(resolve, retryDelay));
      continue;
    }
    if (onProgress) onProgress(job);
    if (job.paused_at) return job;
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error(job.error || failureLabel);
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
  }
}

function updateQobuzJobView(job) {
  const panel = document.querySelector("#qobuz-download-progress");
  if (panel) panel.outerHTML = qobuzProgressPanel(job);

  const entries = qobuzItemMap(job);
  document.querySelectorAll(".track-row[data-item-id]").forEach((row) => {
    const actions = row.querySelector(".track-actions");
    if (!actions) return;
    const current = actions.querySelector("[data-qobuz-item-status]");
    const entry = entries.get(Number(row.dataset.itemId));
    if (!entry?.status) {
      current?.remove();
      return;
    }
    const label = qobuzDownloadLabels[entry.status] || entry.status;
    if (current) {
      current.className = `status-badge qobuz-download ${entry.status}`;
      current.textContent = label;
    } else {
      actions.insertAdjacentHTML("afterbegin", qobuzTrackBadge(entry));
    }
  });

  const button = document.querySelector('[data-action="qobuz-fetch"]');
  if (button) {
    const open = ["pending", "running"].includes(job.status);
    button.disabled = open;
    if (job.paused_at) button.textContent = "Qobuz: загрузка на паузе";
    else if (open) button.textContent = "Qobuz: загрузка выполняется…";
  }
}

function updateYandexJobView(job) {
  const panel = document.querySelector("#yandex-download-progress");
  if (panel) panel.outerHTML = yandexProgressPanel(job);

  const entries = yandexItemMap(job);
  document.querySelectorAll(".track-row[data-item-id]").forEach((row) => {
    const actions = row.querySelector(".track-actions");
    if (!actions) return;
    const current = actions.querySelector("[data-yandex-item-status]");
    const entry = entries.get(Number(row.dataset.itemId));
    if (!entry?.status) {
      current?.remove();
      return;
    }
    const label = yandexDownloadLabels[entry.status] || entry.status;
    const quality = entry.codec && entry.bitrate_kbps
      ? ` · ${String(entry.codec).toUpperCase()} ${entry.bitrate_kbps} kbps`
      : "";
    if (current) {
      current.className = `status-badge qobuz-download ${entry.status}`;
      current.textContent = label + quality;
    } else {
      actions.insertAdjacentHTML("afterbegin", yandexTrackBadge(entry));
    }
  });

  const button = document.querySelector('[data-action="yandex-fetch"]');
  if (button) {
    const active = ["pending", "running"].includes(job.status);
    button.disabled = active;
    if (active) button.textContent = "Яндекс: загрузка выполняется…";
  }
}

async function watchQobuzJob(jobId, playlistId, watchToken) {
  try {
    const finished = await waitForJob(
      jobId,
      "Скачивание с Qobuz завершилось ошибкой",
      (job) => {
        if (watchToken === state.qobuzWatchToken) updateQobuzJobView(job);
      },
    );
    if (finished.paused_at) {
      showToast("Qobuz безопасно приостановлен. Готовые файлы уже в Drive.", 8000);
      await renderPlaylist(playlistId);
      return;
    }
    if (watchToken !== state.qobuzWatchToken) return;
    const downloads = qobuzJobDownloads(finished);
    showToast(
      `Qobuz: в хранилище ${downloads.stored ?? downloads.downloaded ?? 0} · неоднозначно ${downloads.ambiguous ?? 0} · не найдено ${downloads.not_found ?? 0} · ошибок ${(downloads.failed ?? 0) + (downloads.import_failed ?? 0)}`,
      8000,
    );
    await renderPlaylist(playlistId);
  } catch (exception) {
    if (watchToken !== state.qobuzWatchToken) return;
    showToast(exception.message);
    const button = document.querySelector('[data-action="qobuz-fetch"]');
    if (button) button.disabled = false;
  }
}

async function watchYandexJob(jobId, playlistId, watchToken) {
  try {
    const finished = await waitForJob(
      jobId,
      "Скачивание из Яндекс Музыки завершилось ошибкой",
      (job) => {
        if (watchToken === state.yandexWatchToken) updateYandexJobView(job);
      },
    );
    if (watchToken !== state.yandexWatchToken) return;
    const downloads = qobuzJobDownloads(finished);
    showToast(
      `Яндекс: в хранилище ${downloads.stored ?? downloads.downloaded ?? 0} · неоднозначно ${downloads.ambiguous ?? 0} · не найдено ${downloads.not_found ?? 0} · ошибок ${(downloads.failed ?? 0) + (downloads.import_failed ?? 0)}`,
      8000,
    );
    await renderPlaylist(playlistId);
  } catch (exception) {
    if (watchToken !== state.yandexWatchToken) return;
    showToast(exception.message);
    const button = document.querySelector('[data-action="yandex-fetch"]');
    if (button) button.disabled = false;
  }
}

async function pauseQobuzDownload(button) {
  button.disabled = true;
  try {
    await api(`/api/qobuz/downloads/${Number(button.dataset.jobId)}/pause`, {
      method: "POST",
    });
    showToast("Пауза запрошена. Текущая пачка будет сохранена в Drive.", 8000);
    await route();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function resumeQobuzDownload(button) {
  button.disabled = true;
  try {
    await api(`/api/qobuz/downloads/${Number(button.dataset.jobId)}/resume`, {
      method: "POST",
    });
    showToast("Загрузка продолжена с оставшихся треков.", 6000);
    await route();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}


// Запуск докачки MISSING-треков плейлиста с Qobuz. Создаём задание
// (POST /api/qobuz/fetch-missing → 202), затем polling его статуса через
// общий waitForJob (GET /api/jobs/{id} раз в 800 мс — тот же механизм, что и
// у матчинга). По завершении показываем краткую сводку из payload.downloads
// (скачано / не найдено / ошибок) и перезагружаем плейлист: статусы треков
// уже обновил повторный матчинг внутри задания. При ошибке кнопка
// разблокируется, чтобы можно было повторить; при успехе страница
// перерисуется, и кнопка исчезнет вместе с MISSING-треками.
async function qobuzFetchMissing(button) {
  button.disabled = true;
  try {
    const playlistId = Number(button.dataset.playlistId);
    const job = await api("/api/qobuz/fetch-missing", {
      method: "POST",
      body: JSON.stringify({ playlist_id: playlistId }),
    });
    showToast("Скачивание с Qobuz запущено. Прогресс появился в плейлисте.", 6000);
    await renderPlaylist(playlistId);
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function yandexFetchMissing(button) {
  button.disabled = true;
  try {
    const playlistId = Number(button.dataset.playlistId);
    await api("/api/yandex-download/fetch-missing", {
      method: "POST",
      body: JSON.stringify({ playlist_id: playlistId }),
    });
    showToast("Проверка и загрузка из Яндекс Музыки запущена.", 6000);
    await renderPlaylist(playlistId);
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function resolveCandidate(button) {
  button.disabled = true;
  try {
    const trackId = button.dataset.trackId;
    await api(`/api/matching/${button.dataset.matchId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ track_id: trackId ? Number(trackId) : null }),
    });
    showToast(trackId ? "Совпадение подтверждено" : "Трек отмечен как отсутствующий");
    await renderReview();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function route() {
  if (window.location.hash === "#/recovery") {
    await renderRecovery();
    return;
  }
  if (!state.authenticated) {
    renderLogin();
    return;
  }
  if (window.location.hash !== "#/players") state.playerSecret = null;
  const match = window.location.hash.match(/^#\/playlist\/(\d+)$/);
  if (match) {
    await renderPlaylist(Number(match[1]));
  } else if (window.location.hash === "#/users" && state.currentUser?.role === "owner") {
    await renderUsers();
  } else if (window.location.hash === "#/storage" && state.currentUser?.role === "owner") {
    await renderStorage();
  } else if (window.location.hash === "#/providers" && state.currentUser?.role === "owner") {
    await renderProviders();
  } else if (window.location.hash === "#/review") {
    await renderReview();
  } else if (window.location.hash === "#/players") {
    await renderPlayers();
  } else if (window.location.hash === "#/notifications") {
    await renderNotifications();
  } else {
    if (window.location.hash !== "#/playlists") {
      window.location.hash = "#/playlists";
      return;
    }
    await renderPlaylists();
  }
}

app.addEventListener("submit", (event) => {
  if (event.target.id === "recovery-login-form") {
    event.preventDefault();
    recoveryLogin(event.target);
  }
  if (event.target.id === "recovery-owner-form") {
    event.preventDefault();
    updateRecoveryOwner(event.target);
  }
  if (event.target.id === "player-credential-form") {
    event.preventDefault();
    createPlayerCredential(event.target);
  }
  if (["qobuz-credential-form", "yandex-credential-form"].includes(event.target.id)) {
    event.preventDefault();
    rotateProvider(event.target);
  }
  if (event.target.id === "playlist-url-form") {
    event.preventDefault();
    importPlaylistUrl(event.target);
  }
  if (event.target.id === "playlist-converter-form") {
    event.preventDefault();
    importPlaylistContent(event.target);
  }
  if (event.target.id === "google-oauth-form") {
    event.preventDefault();
    configureGoogleOAuth(event.target);
  }
  if (event.target.matches(".storage-account-form")) {
    event.preventDefault();
    updateStorageAccount(event.target);
  }
});

app.addEventListener("change", (event) => {
  if (event.target.matches(".user-role-select")) {
    changeUserRole(event.target);
  }
  if (event.target.id === "playlist-converter-file") {
    loadPlaylistFile(event.target).catch((exception) => {
      event.target.closest("form").querySelector(".form-error").textContent = exception.message;
    });
  }
});

app.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "logout") logout();
  if (action === "user-disable") disableUser(button);
  if (action === "user-revoke-sessions") revokeUserSessions(button);
  if (["recovery-revoke", "recovery-logout"].includes(action)) recoveryAction(action);
  if (action === "match") startMatching(button);
  if (action === "acquisition-start") queueAcquisition(button);
  if (action === "acquisition-pause") controlAcquisition(button, "pause");
  if (action === "acquisition-resume") controlAcquisition(button, "resume");
  if (action === "push-enable") enablePushNotifications(button);
  if (action === "push-disable") disablePushNotifications(button);
  if (action === "qobuz-fetch") qobuzFetchMissing(button);
  if (action === "qobuz-pause") pauseQobuzDownload(button);
  if (action === "qobuz-resume") resumeQobuzDownload(button);
  if (action === "yandex-fetch") yandexFetchMissing(button);
  if (action === "provider-health") runProviderHealth(button);
  if (action === "spotify-connect") connectSpotify(button);
  if (action === "spotify-import-all") importSpotifyLibrary(button);
  if (action === "storage-connect") connectGoogle(button);
  if (action === "storage-health") runStorageHealth(button);
  if (action === "storage-migrate") migrateLocalStorage(button);
  if (action === "resolve") resolveCandidate(button);
  if (action === "player-revoke") revokePlayerCredential(button);
  if (action === "player-revoke-all") revokeAllPlayerCredentials(button);
  if (action === "copy-player-value") {
    const value = button.dataset.copy === "server" ? state.playerSecret?.server_url : state.playerSecret?.api_key;
    if (value) navigator.clipboard.writeText(value).then(() => showToast("Скопировано")).catch(() => showToast("Не удалось скопировать"));
  }
  if (action === "filter") {
    state.statusFilter = button.dataset.status;
    applyFilter();
  }
});

window.addEventListener("hashchange", route);

async function start() {
  if (window.location.hash === "#/recovery") {
    await renderRecovery();
  } else {
    try {
      const currentUser = await api("/api/auth/me", { allowUnauthorized: true });
      state.authenticated = true;
      state.currentUser = currentUser;
      state.csrfToken = currentUser.csrf_token;
      await route();
    } catch (exception) {
      state.authenticated = false;
      state.currentUser = null;
      state.csrfToken = null;
      renderLogin(exception.message);
    }
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }
}

start();
