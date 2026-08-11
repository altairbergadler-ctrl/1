const app = document.querySelector("#app");
const toast = document.querySelector("#toast");

const state = {
  authenticated: false,
  statusFilter: "ALL",
  qobuzWatchToken: 0,
  yandexWatchToken: 0,
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
  importing: "Перенос файлов в хранилище",
  scanning: "Обновление каталога",
  matching: "Сопоставление с плейлистом",
  completed: "Загрузка завершена",
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
          <a class="button ghost small" href="#/storage">Хранилище</a>
          <a class="button ghost small" href="#/providers">Провайдеры</a>
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

// Qobuz настраивается на сервере и остаётся включённым всё время работы стека.
// Ручной шаг подключения в PWA не нужен: fetch/search сами проверяют sidecar и
// credential при каждом задании, а GET /status показывает готовность контура.
function qobuzSourceCard(status) {
  const stateText = !status
    ? "статус недоступен"
    : !status.enabled
      ? "не настроен"
      : status.configured
        ? "включён постоянно"
        : "sidecar включён, но provider credential не настроен";
  return `
    <section class="card qobuz-source">
      <div class="summary-line">
        <span><span class="source-badge">qobuz</span> Докачка missing-треков</span>
        <span class="muted" id="qobuz-status">${escapeHtml(stateText)}</span>
      </div>
    </section>
  `;
}

const providerStateLabels = {
  healthy: "работает",
  expired: "ключ истёк",
  rate_limited: "лимит запросов",
  provider_down: "недоступен",
  not_configured: "не настроен",
};

const providerComponentLabels = {
  account: "Аккаунт",
  provider_api: "API провайдера",
  sidecar: "Sidecar",
  worker: "Worker",
};

function providerHealthCard(item) {
  const components = Object.keys(providerComponentLabels).map((component) => {
    const health = item[component];
    const checked = health.checked_at
      ? new Date(health.checked_at).toLocaleString("ru-RU")
      : "ещё не проверялся";
    return `
      <div class="provider-component">
        <span>${providerComponentLabels[component]}</span>
        <span class="provider-state ${escapeHtml(health.state)}">${escapeHtml(providerStateLabels[health.state] || health.state)}</span>
        <small>${escapeHtml(checked)}</small>
      </div>
    `;
  }).join("");
  return `
    <article class="provider-card">
      <div class="provider-heading">
        <div>
          <span class="source-badge">${escapeHtml(item.provider)}</span>
          <h2>${item.provider === "qobuz" ? "Qobuz" : "Яндекс Музыка"}</h2>
        </div>
        <button class="ghost small" type="button" data-action="provider-health" data-provider="${escapeHtml(item.provider)}">Проверить сейчас</button>
      </div>
      <div class="provider-components">${components}</div>
      <p class="muted provider-version">${item.configured ? `Активна версия ${item.credential_version}` : "Ключ ещё не добавлен"}</p>
      ${providerCredentialGuide(item.provider)}
      ${providerCredentialForm(item.provider)}
    </article>
  `;
}

function providerCredentialGuide(provider) {
  if (provider === "qobuz") {
    return `
      <details class="credential-guide">
        <summary>Где взять token и user ID</summary>
        <div class="credential-guide-body">
          <h3>Если Qobuz уже был подключён</h3>
          <p>Откройте локальный <code>.env</code> прежней установки и перенесите только два значения:</p>
          <dl>
            <div><dt><code>QOBUZ_AUTH_TOKEN</code></dt><dd>в поле «Qobuz token»</dd></div>
            <div><dt><code>QOBUZ_USER_ID</code></dt><dd>в поле «Qobuz user ID»</dd></div>
          </dl>
          <h3>Если нужен новый токен</h3>
          <ol>
            <li>Войдите в свой аккаунт в <a href="https://play.qobuz.com/" target="_blank" rel="noreferrer noopener">Qobuz Web Player</a>.</li>
            <li>Откройте инструменты разработчика браузера, вкладку «Сеть» (Network), выберите Fetch/XHR и обновите страницу.</li>
            <li>Откройте запрос к <code>open.qobuz.com</code>. Скопируйте значение <code>X-User-Auth-Token</code> или <code>user_auth_token</code>, а числовой <code>user_id</code> возьмите из запроса или ответа <code>user/get</code>.</li>
          </ol>
          <p class="credential-warning"><strong>Не используйте:</strong> пароль Qobuz, <code>app_secret</code>, <code>QOBUZ_INTERNAL_TOKEN</code> или ключ шифрования хранилища.</p>
        </div>
      </details>
    `;
  }
  return `
    <details class="credential-guide">
      <summary>Где взять токен Яндекс Музыки</summary>
      <div class="credential-guide-body">
        <h3>Если Яндекс уже был подключён</h3>
        <p>Перенесите значение <code>YANDEX_TOKEN</code> из локального <code>.env</code> прежней установки в поле ниже.</p>
        <h3>Если нужен новый токен</h3>
        <ol>
          <li>Получите пользовательский OAuth-токен через <a href="https://yandex.com/dev/id/doc/en/concepts/ya-oauth-intro" target="_blank" rel="noreferrer noopener">официальную авторизацию Яндекс ID</a> в приложении, которому вы доверяете.</li>
          <li>Разрешите доступ именно для аккаунта с подпиской Яндекс Музыки и вставьте выданный OAuth-токен в поле ниже.</li>
          <li>Нажмите «Проверить и применить»: старый токен останется активным, если новый не пройдёт проверку.</li>
        </ol>
        <p class="credential-warning"><strong>Не используйте:</strong> пароль, cookie <code>Session_id</code>, <code>APP_AUTH_TOKEN</code>, внутренние ключи или токен неизвестного стороннего приложения.</p>
      </div>
    </details>
  `;
}

function providerCredentialForm(provider) {
  if (provider === "qobuz") {
    return `
      <form class="credential-form" id="qobuz-credential-form" autocomplete="off">
        <h3>Обновить доступ</h3>
        <p class="muted">Новый ключ будет проверен до активации. При ошибке текущий останется без изменений.</p>
        <label for="qobuz-token">Qobuz token</label>
        <input id="qobuz-token" name="token" type="password" autocomplete="off" required minlength="8">
        <label for="qobuz-user-id">Qobuz user ID</label>
        <input id="qobuz-user-id" name="user_id" type="password" inputmode="numeric" autocomplete="off" required>
        <button type="submit">Проверить и применить</button>
        <p class="form-error" role="alert"></p>
      </form>
    `;
  }
  return `
    <form class="credential-form" id="yandex-credential-form" autocomplete="off">
      <h3>Обновить доступ</h3>
      <p class="muted">Новый ключ будет проверен до активации. При ошибке текущий останется без изменений.</p>
      <label for="yandex-token">Токен Яндекс Музыки</label>
      <input id="yandex-token" name="token" type="password" autocomplete="off" required minlength="8">
      <button type="submit">Проверить и применить</button>
      <p class="form-error" role="alert"></p>
    </form>
  `;
}

async function renderProviders() {
  loadingPage("Состояние провайдеров");
  try {
    const data = await api("/api/providers/health");
    app.innerHTML = shell(`
      <main>
        <section class="page-header">
          <div>
            <p class="eyebrow">ПОДКЛЮЧЕНИЯ</p>
            <h1>Состояние провайдеров</h1>
            <p class="lede">Аккаунт, API, служебный модуль и worker проверяются отдельно. Значения ключей здесь никогда не отображаются.</p>
          </div>
        </section>
        <section class="provider-grid">${data.items.map(providerHealthCard).join("")}</section>
      </main>
    `);
  } catch (exception) {
    if (state.authenticated) showToast(exception.message);
  }
}

async function rotateProvider(form) {
  const provider = form.id.startsWith("qobuz") ? "qobuz" : "yandex";
  const button = form.querySelector("button[type=submit]");
  const error = form.querySelector(".form-error");
  const values = new FormData(form);
  const payload = provider === "qobuz"
    ? { token: values.get("token"), user_id: values.get("user_id") }
    : { token: values.get("token") };
  form.reset();
  button.disabled = true;
  error.textContent = "";
  try {
    await api(`/api/providers/${provider}/credentials`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    showToast("Новый ключ проверен и применён");
    await renderProviders();
  } catch (exception) {
    error.textContent = exception.message;
    button.disabled = false;
  }
}

async function runProviderHealth(button) {
  button.disabled = true;
  try {
    const job = await api(`/api/providers/${button.dataset.provider}/health-check`, {
      method: "POST",
    });
    await waitForJob(job.id, "Проверка провайдера завершилась ошибкой");
    await renderProviders();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

function formatBytes(value) {
  if (value === null || value === undefined) return "без фиксированного лимита";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"];
  let number = Math.max(0, Number(value) || 0);
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  const digits = number >= 100 || unit === 0 ? 0 : number >= 10 ? 1 : 2;
  return `${number.toFixed(digits)} ${units[unit]}`;
}

function storageAccountCard(account) {
  const usagePercent = account.quota_limit_bytes
    ? Math.min(100, Math.round((account.quota_usage_bytes || 0) * 100 / account.quota_limit_bytes))
    : 0;
  const checked = account.last_checked_at
    ? new Date(account.last_checked_at).toLocaleString("ru-RU")
    : "ещё не проверялся";
  return `
    <article class="storage-account-card">
      <div class="provider-heading">
        <div>
          <span class="source-badge">google drive</span>
          <h2>${escapeHtml(account.label || account.email)}</h2>
          <p class="muted storage-email">${escapeHtml(account.email)}</p>
        </div>
        <span class="provider-state ${escapeHtml(account.state)}">${escapeHtml(providerStateLabels[account.state] || account.state)}</span>
      </div>
      ${account.quota_limit_bytes ? `
        <div class="progress" aria-label="Использовано ${usagePercent}%"><span style="width:${usagePercent}%"></span></div>
      ` : ""}
      <div class="storage-quota">
        <span><strong>${formatBytes(account.quota_usage_bytes)}</strong><small>использовано</small></span>
        <span><strong>${formatBytes(account.free_bytes)}</strong><small>свободно</small></span>
      </div>
      <p class="muted provider-version">Проверено: ${escapeHtml(checked)} · версия доступа ${account.credential_version}</p>
      <form class="storage-account-form" data-account-id="${account.id}">
        <label class="storage-toggle">
          <input name="enabled" type="checkbox" ${account.enabled ? "checked" : ""}>
          Использовать для новых файлов
        </label>
        <label>Приоритет
          <input name="priority" type="number" min="-1000" max="1000" value="${account.priority}">
        </label>
        <div class="action-row">
          <button class="secondary small" type="submit">Сохранить</button>
          <button class="ghost small" type="button" data-action="storage-health" data-account-id="${account.id}">Проверить</button>
        </div>
        <p class="form-error" role="alert"></p>
      </form>
    </article>
  `;
}

function googleDriveGuide(configured) {
  return `
    <details class="credential-guide storage-guide" ${configured ? "" : "open"}>
      <summary>${configured ? "Где находятся Client ID и Client secret" : "Первичная настройка Google Drive — по шагам"}</summary>
      <div class="credential-guide-body">
        ${configured ? `
          <p class="storage-ready-note"><strong>У вас уже всё подключено.</strong> Повторно искать Client ID и Client secret не нужно. Чтобы добавить ещё один диск, нажмите «Добавить аккаунт Google Drive» и войдите в другой Google-аккаунт.</p>
        ` : ""}
        <h3>Если настраиваете впервые</h3>
        <ol>
          <li>Откройте <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer noopener">Google Cloud Console</a>. В верхней панели выберите проект Audiofeel или создайте новый.</li>
          <li>На странице <a href="https://console.cloud.google.com/apis/library/drive.googleapis.com" target="_blank" rel="noreferrer noopener">Google Drive API</a> нажмите <strong>Enable</strong>. Если вместо неё показана кнопка <strong>Disable</strong>, API уже включён.</li>
          <li>Откройте <a href="https://console.cloud.google.com/auth/audience" target="_blank" rel="noreferrer noopener">Google Auth Platform → Audience</a>. Для режима <strong>Testing</strong> в блоке <strong>Test users</strong> нажмите <strong>Add users</strong> и добавьте Google-адрес каждого подключаемого аккаунта.</li>
          <li>Откройте <a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noreferrer noopener">Google Auth Platform → Clients</a>, нажмите <strong>Create client</strong> и выберите тип <strong>Web application</strong>.</li>
          <li><strong>Authorized JavaScript origins</strong> оставьте пустым. В <strong>Authorized redirect URIs</strong> нажмите <strong>Add URI</strong> и вставьте точно:<br><code>https://audiofeel.su/api/storage/google/callback</code></li>
          <li>Нажмите <strong>Create</strong>. В появившемся окне сразу скопируйте <strong>Client ID</strong> и <strong>Client secret</strong>. Google показывает полный secret только при создании — после закрытия окна его уже нельзя посмотреть.</li>
        </ol>
        <h3>Что куда вставлять в Audiofeel</h3>
        <dl class="storage-field-map">
          <div><dt>Google: <strong>Client ID</strong></dt><dd>→ поле Audiofeel «Client ID из Google»</dd></div>
          <div><dt>Google: <strong>Client secret</strong></dt><dd>→ поле Audiofeel «Client secret из окна Create / Add secret»</dd></div>
          <div><dt>Google: <strong>Authorized redirect URI</strong></dt><dd>→ только адрес callback выше; в Audiofeel его вставлять не надо</dd></div>
        </dl>
        <h3>Если окно с Client secret уже закрыто</h3>
        <ol>
          <li>Откройте <strong>Google Auth Platform → Clients</strong> и нажмите на имя клиента, например <strong>Audiofeel Web</strong>.</li>
          <li>Полный <strong>Client ID</strong> находится справа в блоке <strong>Additional information</strong>.</li>
          <li>В блоке <strong>Client secrets</strong> старый secret виден только как маска — её вставлять нельзя. Нажмите <strong>Add client secret</strong> и сразу скопируйте новый secret из одноразового окна.</li>
          <li>В Audiofeel раскройте «Заменить Client ID и Client secret», вставьте оба значения и пройдите вход Google. Старые рабочие данные сохранятся, если новые не пройдут проверку.</li>
        </ol>
        <p class="credential-warning">Audiofeel запрашивает ограниченный доступ <code>drive.file</code>: приложение видит только созданные и выбранные через него файлы. Пароль Google вводится только на странице Google.</p>
      </div>
    </details>
  `;
}

function consumeStorageCallbackNotice() {
  const query = new URLSearchParams(window.location.search);
  if (query.has("storage_connected")) {
    showToast("Google Drive подключён и проверен");
  } else if (query.has("storage_error")) {
    const labels = {
      oauth_denied: "Подключение Google отменено",
      credential_rejected: "Google отклонил новые данные — прежний доступ сохранён",
      provider_unavailable: "Google временно недоступен — прежний доступ сохранён",
    };
    showToast(labels[query.get("storage_error")] || "Не удалось подключить Google Drive");
  } else {
    return;
  }
  window.history.replaceState(null, "", `${window.location.pathname}#/storage`);
}

async function renderStorage() {
  loadingPage("Хранилище Google Drive");
  try {
    const data = await api("/api/storage");
    app.innerHTML = shell(`
      <main>
        <section class="page-header">
          <div>
            <p class="eyebrow">БИБЛИОТЕКА · GOOGLE DRIVE</p>
            <h1>Общее облачное хранилище</h1>
            <p class="lede">Каждый подключённый аккаунт добавляет свой свободный объём. Google Drive хранит постоянную библиотеку, а локальный диск используется только как временный буфер до проверенной загрузки.</p>
          </div>
          <div class="action-row">
            ${data.oauth.configured ? '<button type="button" data-action="storage-connect">Добавить аккаунт Google Drive</button>' : ""}
            ${data.accounts.length ? '<button class="secondary" type="button" data-action="storage-migrate">Перенести временные файлы</button>' : ""}
          </div>
        </section>
        <section class="storage-summary">
          <span><strong>${data.accounts.length}</strong><small>аккаунтов</small></span>
          <span><strong>${formatBytes(data.total_usage_bytes)}</strong><small>использовано</small></span>
          <span><strong>${formatBytes(data.accounts.length ? data.total_free_bytes : 0)}</strong><small>свободно суммарно</small></span>
        </section>
        ${googleDriveGuide(data.oauth.configured)}
        <section class="storage-config-card">
          <div>
            <p class="eyebrow">OAUTH-ПРИЛОЖЕНИЕ</p>
            <h2>${data.oauth.configured ? "Доступ настроен" : "Первичная настройка"}</h2>
            <p class="muted">Сохранённые Client secret и refresh tokens никогда не возвращаются в API. Новые данные проходят вход Google до замены действующих.</p>
            ${data.oauth.configured ? '<p class="storage-ready-note"><strong>Сейчас ничего вводить не нужно.</strong> Форма справа нужна только при замене самого OAuth-клиента или утраченного Client secret.</p>' : ""}
          </div>
          <details class="storage-oauth-form" ${data.oauth.configured ? "" : "open"}>
            <summary>${data.oauth.configured ? "Заменить Client ID и Client secret" : "Ввести данные из Google"}</summary>
            <form id="google-oauth-form" autocomplete="off">
              <label for="google-client-id">Client ID из Google</label>
              <input id="google-client-id" name="client_id" type="text" autocomplete="off" required minlength="20" placeholder="…apps.googleusercontent.com" aria-describedby="google-client-id-help">
              <small id="google-client-id-help">Берётся в Google Auth Platform → Clients → ваш клиент → Additional information → Client ID.</small>
              <label for="google-client-secret">Client secret из окна Create / Add secret</label>
              <input id="google-client-secret" name="client_secret" type="password" autocomplete="new-password" required minlength="8" aria-describedby="google-client-secret-help">
              <small id="google-client-secret-help">Маска вида •••• или **** не подходит. Если полный secret потерян, в Google нажмите Add client secret и скопируйте новый сразу.</small>
              <button type="submit">Проверить через Google и применить</button>
              <p class="form-error" role="alert"></p>
            </form>
          </details>
        </section>
        ${data.accounts.length
          ? `<section class="storage-account-grid">${data.accounts.map(storageAccountCard).join("")}</section>`
          : '<section class="empty-state"><h2>Аккаунты ещё не подключены</h2><p>Подготовьте OAuth-приложение по памятке и пройдите вход Google.</p></section>'}
      </main>
    `);
    consumeStorageCallbackNotice();
  } catch (exception) {
    if (state.authenticated) showToast(exception.message);
  }
}

async function configureGoogleOAuth(form) {
  const button = form.querySelector("button[type=submit]");
  const error = form.querySelector(".form-error");
  const values = new FormData(form);
  const payload = {
    client_id: values.get("client_id"),
    client_secret: values.get("client_secret"),
  };
  form.reset();
  button.disabled = true;
  error.textContent = "";
  try {
    const result = await api("/api/storage/google/oauth-config", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    window.location.assign(result.authorization_url);
  } catch (exception) {
    error.textContent = exception.message;
    button.disabled = false;
  }
}

async function connectGoogle(button) {
  button.disabled = true;
  try {
    const result = await api("/api/storage/google/connect", { method: "POST" });
    window.location.assign(result.authorization_url);
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function updateStorageAccount(form) {
  const button = form.querySelector("button[type=submit]");
  const error = form.querySelector(".form-error");
  const values = new FormData(form);
  button.disabled = true;
  error.textContent = "";
  try {
    await api(`/api/storage/accounts/${form.dataset.accountId}`, {
      method: "PATCH",
      body: JSON.stringify({
        enabled: values.get("enabled") === "on",
        priority: Number(values.get("priority")),
      }),
    });
    showToast("Настройки диска сохранены");
    await renderStorage();
  } catch (exception) {
    error.textContent = exception.message;
    button.disabled = false;
  }
}

async function runStorageHealth(button) {
  button.disabled = true;
  try {
    const job = await api(`/api/storage/accounts/${button.dataset.accountId}/health-check`, { method: "POST" });
    await waitForJob(job.id, "Проверка Google Drive завершилась ошибкой");
    showToast("Google Drive проверен");
    await renderStorage();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function migrateLocalStorage(button) {
  button.disabled = true;
  try {
    const job = await api("/api/storage/migrate-local", { method: "POST" });
    showToast("Перенос в Google Drive запущен", 6000);
    const finished = await waitForJob(job.id, "Перенос библиотеки завершился ошибкой");
    const summary = finished.payload?.storage || {};
    showToast(`Google Drive: загружено ${summary.uploaded || 0}, локально очищено ${summary.evicted || 0}, ошибок ${summary.failed || 0}`, 8000);
    await renderStorage();
  } catch (exception) {
    showToast(exception.message);
    button.disabled = false;
  }
}

async function renderPlaylists() {
  loadingPage("Плейлисты в вашем архиве");
  try {
    const [data, qobuzStatus, yandexStatus] = await Promise.all([
      api("/api/playlists?limit=200"),
      // Статус Qobuz подтягиваем мягко: если роутер недоступен, карточка
      // просто покажет «статус недоступен», а список плейлистов не пострадает.
      api("/api/qobuz/status").catch(() => null),
      api("/api/yandex-download/status").catch(() => null),
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
        ${yandexStatus?.enabled ? yandexSourceCard(yandexStatus) : ""}
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

function qobuzJobDownloads(job) {
  return job?.payload?.downloads || {};
}

function qobuzItemMap(job) {
  return new Map(
    (qobuzJobDownloads(job).items || []).map((item) => [Number(item.item_id), item]),
  );
}

function yandexItemMap(job) {
  return new Map(
    (qobuzJobDownloads(job).items || []).map((item) => [Number(item.item_id), item]),
  );
}

function qobuzTrackBadge(entry) {
  if (!entry?.status) return "";
  const label = qobuzDownloadLabels[entry.status] || entry.status;
  return `<span class="status-badge qobuz-download ${escapeHtml(entry.status)}" data-qobuz-item-status>${escapeHtml(label)}</span>`;
}

function yandexTrackBadge(entry) {
  if (!entry?.status) return "";
  const label = entry.status === "failed" && entry.error_detail
    ? `Ошибка: ${entry.error_detail}`
    : yandexDownloadLabels[entry.status] || entry.status;
  const quality = entry.codec && entry.bitrate_kbps
    ? ` · ${String(entry.codec).toUpperCase()} ${entry.bitrate_kbps} kbps`
    : "";
  return `<span class="status-badge qobuz-download ${escapeHtml(entry.status)}" data-yandex-item-status>${escapeHtml(label + quality)}</span>`;
}

function qobuzProgressPanel(job) {
  if (!job || job.payload?.mode !== "fetch_missing") return "";
  const payload = job.payload || {};
  const downloads = qobuzJobDownloads(job);
  const batchTotal = Number(downloads.eligible_total ?? downloads.batch_total ?? downloads.total_missing ?? 0);
  const processed = Number(downloads.processed ?? downloads.attempted ?? 0);
  const percent = batchTotal > 0 ? Math.min(100, Math.round((processed / batchTotal) * 100)) : 0;
  const phase = payload.phase || (job.status === "pending" ? "queued" : job.status);
  const phaseLabel = job.status === "failed"
    ? "Загрузка остановлена"
    : (qobuzPhaseLabels[phase] || "Обработка загрузки");
  const stateLabel = job.status === "done"
    ? "завершено"
    : job.status === "failed"
      ? "ошибка"
      : "выполняется";
  const totalMissing = Number(downloads.total_missing ?? batchTotal);
  const batchCount = Number(downloads.batch_count ?? (batchTotal ? 1 : 0));
  const currentBatch = Number(downloads.current_batch ?? (batchCount ? 1 : 0));
  const currentBatchSize = Number(downloads.current_batch_size ?? batchTotal);
  const batchProcessed = Number(downloads.batch_processed ?? processed);
  const alreadyChecked = Number(downloads.skipped_same_source ?? 0);
  const batchLabel = batchCount > 1
    ? `Пачка ${currentBatch} из ${batchCount} · ${batchProcessed} / ${currentBatchSize}`
    : `${processed} / ${batchTotal} треков`;
  const scopeLabel = alreadyChecked > 0
    ? `${batchTotal} новых проверок · ${alreadyChecked} уже проверено в Qobuz`
    : `${batchTotal} новых проверок из ${totalMissing} отсутствующих треков`;
  return `
    <section class="qobuz-progress-card ${escapeHtml(job.status)}" id="qobuz-download-progress" aria-live="polite">
      <div class="qobuz-progress-heading">
        <div>
          <p class="eyebrow">QOBUZ · JOB #${job.id}</p>
          <h2>${escapeHtml(phaseLabel)}</h2>
        </div>
        <span class="status-badge qobuz-job-state ${escapeHtml(job.status)}">${escapeHtml(stateLabel)}</span>
      </div>
      <div class="progress" aria-label="Обработано ${processed} из ${batchTotal}">
        <span style="width: ${percent}%"></span>
      </div>
      <div class="summary-line">
        <span>${escapeHtml(scopeLabel)}</span>
        <strong>${processed} / ${batchTotal}</strong>
      </div>
      <p class="qobuz-batch-line">${escapeHtml(batchLabel)}${downloads.batch_state === "paused" ? ` · пауза ${downloads.batch_pause_seconds ?? 0} сек.` : ""}</p>
      <div class="qobuz-progress-stats">
        <span><strong>${downloads.stored ?? downloads.downloaded ?? 0}</strong> в хранилище</span>
        <span><strong>${downloads.not_found ?? 0}</strong> не найдено</span>
        <span><strong>${downloads.ambiguous ?? 0}</strong> требуют выбора</span>
        <span><strong>${(downloads.failed ?? 0) + (downloads.import_failed ?? 0)}</strong> ошибок</span>
      </div>
      ${job.error ? `<p class="qobuz-progress-error">${escapeHtml(job.error)}</p>` : ""}
    </section>
  `;
}

function yandexProgressPanel(job) {
  if (!job) return "";
  const compatibleJob = {
    ...job,
    payload: { ...(job.payload || {}), mode: "fetch_missing" },
  };
  return qobuzProgressPanel(compatibleJob)
    .replaceAll("QOBUZ", "YANDEX")
    .replaceAll("Qobuz", "Яндекс Музыке")
    .replace('id="qobuz-download-progress"', 'id="yandex-download-progress"');
}

function yandexSourceCard(status) {
  const stateText = !status
    ? "статус недоступен"
    : !status.enabled
      ? "загрузка выключена"
      : status.configured
        ? "готов · FLAC предпочтительно, AAC/MP3 fallback"
        : "подключите Яндекс Музыку как источник плейлистов";
  return `
    <section class="card qobuz-source">
      <div class="summary-line">
        <span><span class="source-badge">yandex</span> Дополнительный источник missing-треков</span>
        <span class="muted" id="yandex-download-status">${escapeHtml(stateText)}</span>
      </div>
      <p class="muted" style="margin-top: 10px">Сначала FLAC. Если его нет, сохраняется лучший доступный AAC/MP3 без перекодирования.</p>
    </section>
  `;
}

function trackRow(item, qobuzItems = new Map(), yandexItems = new Map()) {
  const download = item.status === "READY"
    ? `<a class="button secondary small" href="/api/download/track/${item.id}" download>Скачать</a>`
    : item.status === "NEEDS_REVIEW"
      ? `<a class="button ghost small" href="#/review">Выбрать</a>`
      : "";
  return `
    <article class="track-row" data-status="${escapeHtml(item.status)}" data-item-id="${item.id}">
      <div class="track-position">${String(item.position + 1).padStart(2, "0")}</div>
      <div>
        <h3 class="track-title">${escapeHtml(item.title_raw || "Без названия")}</h3>
        <p class="track-meta">${escapeHtml(item.artist_raw || "Неизвестный исполнитель")} · ${escapeHtml(item.album_raw || "Альбом не указан")}</p>
      </div>
      <div class="track-actions action-row">
        ${yandexTrackBadge(yandexItems.get(Number(item.id)))}
        ${qobuzTrackBadge(qobuzItems.get(Number(item.id)))}
        <span class="status-badge ${statusClass(item.status)}">${statusLabels[item.status] || item.status}</span>
        ${download}
      </div>
    </article>
  `;
}

async function renderPlaylist(playlistId) {
  const qobuzWatchToken = ++state.qobuzWatchToken;
  const yandexWatchToken = ++state.yandexWatchToken;
  loadingPage("Открываем плейлист");
  try {
    const [
      playlist,
      items,
      qobuzStatus,
      yandexStatus,
      qobuzJob,
      yandexJob,
      qobuzEligibility,
      yandexEligibility,
    ] = await Promise.all([
      api(`/api/playlists/${playlistId}`),
      fetchAllItems(playlistId),
      api("/api/qobuz/status").catch(() => null),
      api("/api/yandex-download/status").catch(() => null),
      api(`/api/qobuz/download-status/${playlistId}`).catch(() => null),
      api(`/api/yandex-download/download-status/${playlistId}`).catch(() => null),
      api(`/api/qobuz/download-eligibility/${playlistId}`).catch(() => null),
      api(`/api/yandex-download/download-eligibility/${playlistId}`).catch(() => null),
    ]);
    const qobuzItems = qobuzItemMap(qobuzJob);
    const yandexItems = yandexItemMap(yandexJob);
    const qobuzDownloadActive = ["pending", "running"].includes(qobuzJob?.status);
    const yandexDownloadActive = ["pending", "running"].includes(yandexJob?.status);
    const qobuzEligible = Number(qobuzEligibility?.eligible ?? playlist.summary.missing);
    const yandexEligible = Number(yandexEligibility?.eligible ?? playlist.summary.missing);
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
            ${playlist.summary.missing > 0 && qobuzStatus?.enabled && qobuzStatus?.configured ? `<button class="secondary" type="button" data-action="qobuz-fetch" data-playlist-id="${playlist.id}" ${qobuzDownloadActive || qobuzEligible === 0 ? "disabled" : ""}>${qobuzDownloadActive ? "Qobuz: загрузка выполняется…" : qobuzEligible === 0 ? "Qobuz: новых треков нет" : `⬇ Проверить новые в Qobuz (${qobuzEligible})`}</button>` : ""}
            ${playlist.summary.missing > 0 && yandexStatus?.enabled && yandexStatus?.configured ? `<button class="secondary" type="button" data-action="yandex-fetch" data-playlist-id="${playlist.id}" ${yandexDownloadActive || yandexEligible === 0 ? "disabled" : ""}>${yandexDownloadActive ? "Яндекс: загрузка выполняется…" : yandexEligible === 0 ? "Яндекс: новых треков нет" : `⬇ Проверить новые в Яндекс Музыке (${yandexEligible})`}</button>` : ""}
            <a class="button secondary" href="/api/download/playlist/${playlist.id}" download>Скачать ZIP</a>
            <a class="button ghost" href="/api/download/playlist/${playlist.id}/m3u8" download>M3U8</a>
          </div>
        </section>
        ${qobuzProgressPanel(qobuzJob)}
        ${yandexProgressPanel(yandexJob)}
        <div class="filter-row" role="group" aria-label="Фильтр статусов">
          ${statuses.map((status) => `
            <button class="ghost small" type="button" data-action="filter" data-status="${status}" aria-pressed="${state.statusFilter === status}">
              ${status === "ALL" ? "Все" : statusLabels[status]}
            </button>
          `).join("")}
        </div>
        <section class="track-list" id="track-list">
          ${items.map((item) => trackRow(item, qobuzItems, yandexItems)).join("")}
        </section>
      </main>
    `);
    applyFilter();
    if (qobuzDownloadActive) {
      void watchQobuzJob(qobuzJob.id, playlistId, qobuzWatchToken);
    }
    if (yandexDownloadActive) {
      void watchYandexJob(yandexJob.id, playlistId, yandexWatchToken);
    }
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

async function waitForJob(jobId, failureLabel = "Задание завершилось ошибкой", onProgress = null) {
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
    const active = ["pending", "running"].includes(job.status);
    button.disabled = active;
    if (active) button.textContent = "Qobuz: загрузка выполняется…";
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
  if (!state.authenticated) return;
  const match = window.location.hash.match(/^#\/playlist\/(\d+)$/);
  if (match) {
    await renderPlaylist(Number(match[1]));
  } else if (window.location.hash === "#/storage") {
    await renderStorage();
  } else if (window.location.hash === "#/providers") {
    await renderProviders();
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
  if (event.target.id === "login-form") {
    event.preventDefault();
    login(event.target);
  }
  if (["qobuz-credential-form", "yandex-credential-form"].includes(event.target.id)) {
    event.preventDefault();
    rotateProvider(event.target);
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

app.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "logout") logout();
  if (action === "match") startMatching(button);
  if (action === "qobuz-fetch") qobuzFetchMissing(button);
  if (action === "yandex-fetch") yandexFetchMissing(button);
  if (action === "provider-health") runProviderHealth(button);
  if (action === "storage-connect") connectGoogle(button);
  if (action === "storage-health") runStorageHealth(button);
  if (action === "storage-migrate") migrateLocalStorage(button);
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
