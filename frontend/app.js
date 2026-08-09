const app = document.querySelector("#app");
const toast = document.querySelector("#toast");

const state = {
  authenticated: false,
  statusFilter: "ALL",
};

const statusLabels = {
  READY: "Готов",
  NEEDS_REVIEW: "Проверить",
  MISSING: "Нет в архиве",
  UNMATCHED: "Не обработан",
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
  const { allowUnauthorized = false, ...requestOptions } = options;
  const headers = new Headers(requestOptions.headers || {});
  if (requestOptions.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...requestOptions,
    credentials: "same-origin",
    headers,
  });
  if (response.status === 401 && !allowUnauthorized) {
    state.authenticated = false;
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

function renderLogin(message = "") {
  app.innerHTML = `
    <main class="login-page">
      <section class="login-panel">
        <p class="eyebrow">ВАША ФОНОТЕКА · БЕЗ ПОТЕРЬ</p>
        <h1>Музыка уже дома.</h1>
        <p class="lede">Введите токен сервиса, чтобы открыть плейлисты и скачать оригинальные файлы без конвертации.</p>
        <form id="login-form">
          <label for="token">APP_AUTH_TOKEN</label>
          <input id="token" name="token" type="password" autocomplete="current-password" required minlength="16" autofocus>
          <button type="submit">Открыть архив</button>
          <p class="form-error" role="alert">${escapeHtml(message)}</p>
        </form>
      </section>
    </main>
  `;
}

async function login(form) {
  const button = form.querySelector("button");
  const error = form.querySelector(".form-error");
  button.disabled = true;
  error.textContent = "";
  try {
    await api("/api/auth/login", {
      method: "POST",
      allowUnauthorized: true,
      body: JSON.stringify({ token: new FormData(form).get("token") }),
    });
    state.authenticated = true;
    if (window.location.hash === "#/playlists") {
      await route();
    } else {
      window.location.hash = "#/playlists";
    }
  } catch (exception) {
    error.textContent = exception.message;
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // Rendering the login page is the desired fallback even if the session expired.
  }
  state.authenticated = false;
  window.location.hash = "";
  renderLogin();
}

function playlistCard(playlist) {
  const { summary } = playlist;
  return `
    <article class="card">
      <div>
        <span class="source-badge">${escapeHtml(playlist.service)}</span>
        <h3>${escapeHtml(playlist.name)}</h3>
      </div>
      <div>
        <div class="progress" aria-label="Собрано ${summary.collected_percent}%">
          <span style="width: ${Math.max(0, Math.min(100, summary.collected_percent))}%"></span>
        </div>
        <div class="summary-line">
          <span>${summary.ready} из ${playlist.track_count} готовы</span>
          <span>${summary.review} review · ${summary.missing} нет</span>
        </div>
        <div class="action-row" style="margin-top: 20px">
          <a class="button secondary small" href="#/playlist/${playlist.id}">Открыть</a>
        </div>
      </div>
    </article>
  `;
}

// Карточка источника Qobuz на странице плейлистов. Показывает состояние
// интеграции по GET /api/qobuz/status: выключена (enabled=false → серое
// «не настроен»), включена без креденшелов, либо готова к проверке логина.
// Кнопка «Подключить» доступна только когда сервис включён и креденшелы
// заданы в .env — сама проверка идёт через POST /api/qobuz/connect.
function qobuzSourceCard(status) {
  const stateText = !status
    ? "статус недоступен"
    : !status.enabled
      ? "не настроен"
      : status.configured
        ? "готов к подключению"
        : "включён, но без QOBUZ_EMAIL/QOBUZ_PASSWORD";
  const button = status?.enabled && status?.configured
    ? `<button class="ghost small" type="button" data-action="qobuz-connect">Подключить</button>`
    : "";
  return `
    <section class="card qobuz-source">
      <div class="summary-line">
        <span><span class="source-badge">qobuz</span> Докачка missing-треков</span>
        <span class="muted" id="qobuz-status">${escapeHtml(stateText)}</span>
      </div>
      <div class="action-row" style="margin-top: 12px">${button}</div>
    </section>
  `;
}

async function renderPlaylists() {
  loadingPage("Плейлисты в вашем архиве");
  try {
    const [data, qobuzStatus] = await Promise.all([
      api("/api/playlists?limit=200"),
      // Статус Qobuz подтягиваем мягко: если роутер недоступен, карточка
      // просто покажет «статус недоступен», а список плейлистов не пострадает.
      api("/api/qobuz/status").catch(() => null),
    ]);
    app.innerHTML = shell(`
      <main>
        <section class="page-header">
          <div>
            <p class="eyebrow">КОЛЛЕКЦИЯ</p>
            <h1>Плейлисты в вашем архиве</h1>
            <p class="lede">Сразу видно, что найдено в lossless-библиотеке, а что требует вашего решения.</p>
          </div>
          <div class="action-row">
            <button type="button" data-action="match" data-playlist-id="">Сопоставить всё</button>
            <a class="button secondary" href="#/review">Открыть review</a>
          </div>
        </section>
        ${qobuzSourceCard(qobuzStatus)}
        ${data.items.length ? `<section class="playlist-grid">${data.items.map(playlistCard).join("")}</section>` : `
          <section class="empty-state">
            <h2>Плейлистов пока нет</h2>
            <p>Подключите Spotify или Яндекс.Музыку через API и запустите импорт — здесь появится прогресс коллекции.</p>
          </section>
        `}
      </main>
    `);
  } catch (exception) {
    if (state.authenticated) showToast(exception.message);
  }
}

async function fetchAllItems(playlistId) {
  const items = [];
  let offset = 0;
  while (true) {
    const page = await api(`/api/playlists/${playlistId}/items?limit=500&offset=${offset}`);
    items.push(...page.items);
    offset += page.items.length;
    if (!page.items.length || offset >= page.total) return items;
  }
}

function statusClass(status) {
  return status.toLowerCase();
}

function trackRow(item) {
  const download = item.status === "READY"
    ? `<a class="button secondary small" href="/api/download/track/${item.id}" download>Скачать</a>`
    : item.status === "NEEDS_REVIEW"
      ? `<a class="button ghost small" href="#/review">Выбрать</a>`
      : "";
  return `
    <article class="track-row" data-status="${escapeHtml(item.status)}">
      <div class="track-position">${String(item.position + 1).padStart(2, "0")}</div>
      <div>
        <h3 class="track-title">${escapeHtml(item.title_raw || "Без названия")}</h3>
        <p class="track-meta">${escapeHtml(item.artist_raw || "Неизвестный исполнитель")} · ${escapeHtml(item.album_raw || "Альбом не указан")}</p>
      </div>
      <div class="track-actions action-row">
        <span class="status-badge ${statusClass(item.status)}">${statusLabels[item.status] || item.status}</span>
        ${download}
      </div>
    </article>
  `;
}

async function renderPlaylist(playlistId) {
  loadingPage("Открываем плейлист");
  try {
    const [playlist, items] = await Promise.all([
      api(`/api/playlists/${playlistId}`),
      fetchAllItems(playlistId),
    ]);
    const statuses = ["ALL", "READY", "NEEDS_REVIEW", "MISSING", "UNMATCHED"];
    app.innerHTML = shell(`
      <main>
        <section class="page-header">
          <div>
            <p class="eyebrow">${escapeHtml(playlist.service)} · ${playlist.track_count} ТРЕКОВ</p>
            <h1>${escapeHtml(playlist.name)}</h1>
            <p class="lede">${playlist.summary.ready} готовы · ${playlist.summary.review} требуют review · ${playlist.summary.missing} отсутствуют.</p>
          </div>
          <div class="action-row">
            <button type="button" data-action="match" data-playlist-id="${playlist.id}">Сопоставить</button>
            ${/* Кнопка докачки с Qobuz появляется, только когда в плейлисте есть
                 MISSING-треки: POST /api/qobuz/fetch-missing поставит задание,
                 которое скачает недостающее в staging, перенесёт в библиотеку
                 и повторит матчинг (pipeline на стороне worker'а). */""}
            ${playlist.summary.missing > 0 ? `<button class="secondary" type="button" data-action="qobuz-fetch" data-playlist-id="${playlist.id}">⬇ Скачать missing с Qobuz (${playlist.summary.missing})</button>` : ""}
            <a class="button secondary" href="/api/download/playlist/${playlist.id}" download>Скачать ZIP</a>
            <a class="button ghost" href="/api/download/playlist/${playlist.id}/m3u8" download>M3U8</a>
          </div>
        </section>
        <div class="filter-row" role="group" aria-label="Фильтр статусов">
          ${statuses.map((status) => `
            <button class="ghost small" type="button" data-action="filter" data-status="${status}" aria-pressed="${state.statusFilter === status}">
              ${status === "ALL" ? "Все" : statusLabels[status]}
            </button>
          `).join("")}
        </div>
        <section class="track-list" id="track-list">
          ${items.map(trackRow).join("")}
        </section>
      </main>
    `);
    applyFilter();
  } catch (exception) {
    if (state.authenticated) showToast(exception.message);
  }
}

function applyFilter() {
  document.querySelectorAll(".track-row").forEach((row) => {
    row.hidden = state.statusFilter !== "ALL" && row.dataset.status !== state.statusFilter;
  });
  document.querySelectorAll('[data-action="filter"]').forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.status === state.statusFilter));
  });
}

function candidateCard(matchId, candidate) {
  const quality = [
    candidate.bit_depth ? `${candidate.bit_depth} bit` : null,
    candidate.sample_rate ? `${Math.round(candidate.sample_rate / 100) / 10} kHz` : null,
    candidate.format?.toUpperCase(),
  ].filter(Boolean).join(" · ");
  return `
    <article class="candidate">
      <div>
        <h4 class="candidate-title">${escapeHtml(candidate.artist)} — ${escapeHtml(candidate.title)}</h4>
        <p class="candidate-meta">${escapeHtml(candidate.album)} · уверенность ${Math.round(candidate.confidence * 100)}%</p>
        <span class="quality">${escapeHtml(quality || "lossless")}</span>
      </div>
      <button class="small" type="button" data-action="resolve" data-match-id="${matchId}" data-track-id="${candidate.track_id}">Это он</button>
    </article>
  `;
}

async function renderReview() {
  loadingPage("Неоднозначные совпадения");
  try {
    const data = await api("/api/matching/review?limit=200");
    app.innerHTML = shell(`
      <main>
        <section class="page-header">
          <div>
            <p class="eyebrow">РУЧНАЯ ПРОВЕРКА · ${data.total}</p>
            <h1>Сомнительные совпадения</h1>
            <p class="lede">Сервис показывает только близкие варианты. Подтвердите нужную запись или отметьте, что трека в архиве нет.</p>
          </div>
          <a class="button secondary" href="#/playlists">К плейлистам</a>
        </section>
        ${data.items.length ? `<section class="review-list">${data.items.map((item) => `
          <article class="review-card">
            <div class="review-heading">
              <div>
                <span class="source-badge">${escapeHtml(item.playlist_name)}</span>
                <h3>${escapeHtml(item.artist_raw || "Неизвестный исполнитель")} — ${escapeHtml(item.title_raw || "Без названия")}</h3>
                <p class="track-meta">${escapeHtml(item.album_raw || "Альбом не указан")}</p>
              </div>
              <button class="ghost small" type="button" data-action="resolve" data-match-id="${item.match_id}" data-track-id="">Нет в архиве</button>
            </div>
            <div class="candidate-list">
              ${item.candidates.length ? item.candidates.map((candidate) => candidateCard(item.match_id, candidate)).join("") : `<p class="muted">Кандидаты больше не доступны — запустите матчинг повторно.</p>`}
            </div>
          </article>
        `).join("")}</section>` : `
          <section class="empty-state">
            <h2>Очередь review пуста</h2>
            <p>Все совпадения уже определены или отмечены как отсутствующие.</p>
          </section>
        `}
      </main>
    `);
  } catch (exception) {
    if (state.authenticated) showToast(exception.message);
  }
}

async function startMatching(button) {
  button.disabled = true;
  try {
    const playlistId = button.dataset.playlistId;
    const job = await api("/api/matching/run", {
      method: "POST",
      body: JSON.stringify({ playlist_id: playlistId ? Number(playlistId) : null }),
    });
    showToast("Матчинг запущен. Ждём результат…", 10_000);
    await waitForJob(job.id);
    showToast("Матчинг завершён");
    await route();
  } catch (exception) {
    showToast(exception.message);
  } finally {
    button.disabled = false;
  }
}

async function waitForJob(jobId, failureLabel = "Задание завершилось ошибкой") {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error(job.error || failureLabel);
    await new Promise((resolve) => window.setTimeout(resolve, 800));
  }
}

// Проверка подключения Qobuz: POST /api/qobuz/connect создаёт клиента
// (фактический логин). При успехе показываем label тарифа (например Studio) —
// подтверждение, что подписка активна; при 400/502/503 текст ошибки сервера
// уходит в toast. Кнопка разблокируется только при ошибке: при успехе
// состояние уже отражено в строке статуса, повторный клик не нужен.
async function qobuzConnect(button) {
  button.disabled = true;
  try {
    const result = await api("/api/qobuz/connect", { method: "POST" });
    const status = document.querySelector("#qobuz-status");
    if (status) status.textContent = result.label ? `подключён · ${result.label}` : "подключён";
    showToast(result.label ? `Qobuz подключён: ${result.label}` : "Qobuz подключён");
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
    showToast("Скачивание с Qobuz запущено. Ждём результат…", 10_000);
    const finished = await waitForJob(job.id, "Скачивание с Qobuz завершилось ошибкой");
    const downloads = finished.payload?.downloads || {};
    showToast(
      `Qobuz: скачано ${downloads.downloaded ?? 0} · не найдено ${downloads.not_found ?? 0} · ошибок ${downloads.failed ?? 0}`,
      8000,
    );
    await route();
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
  if (!state.authenticated) return;
  const match = window.location.hash.match(/^#\/playlist\/(\d+)$/);
  if (match) {
    await renderPlaylist(Number(match[1]));
  } else if (window.location.hash === "#/review") {
    await renderReview();
  } else {
    if (window.location.hash !== "#/playlists") {
      window.location.hash = "#/playlists";
      return;
    }
    await renderPlaylists();
  }
}

app.addEventListener("submit", (event) => {
  if (event.target.id !== "login-form") return;
  event.preventDefault();
  login(event.target);
});

app.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "logout") logout();
  if (action === "match") startMatching(button);
  if (action === "qobuz-connect") qobuzConnect(button);
  if (action === "qobuz-fetch") qobuzFetchMissing(button);
  if (action === "resolve") resolveCandidate(button);
  if (action === "filter") {
    state.statusFilter = button.dataset.status;
    applyFilter();
  }
});

window.addEventListener("hashchange", route);

async function start() {
  try {
    await api("/api/playlists?limit=1");
    state.authenticated = true;
    await route();
  } catch (exception) {
    if (!document.querySelector("#login-form")) renderLogin(exception.message);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }
}

start();
