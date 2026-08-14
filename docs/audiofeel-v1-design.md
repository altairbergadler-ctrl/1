# Audiofeel 1.0: frontend design contract

## Цель

Audiofeel 1.0 делает переданный HTML-прототип основным визуальным языком
production PWA, сохраняя действующий backend, каталог, acquisition queue,
Google Drive, bit-perfect delivery, OpenSubsonic и модель доступа.

Canonical release version — `1.0.0`; интерфейс показывает короткую `1.0`.
Единый backend source находится в `backend/app/version.py`, API health
возвращает `app_version` и immutable deployment `release_sha`.

## Реализация

- `frontend/app.js` продолжает получать все данные из production API.
- `frontend/redesign.css` — основной visual layer поверх небольшого
  backwards-compatible `styles.css`.
- Desktop: sticky sidebar, route breadcrumbs, отдельные media/system groups.
- Tablet: компактный sidebar с иконками и сохранённой доступностью разделов.
- Mobile: scrollable bottom navigation; системные owner routes не исчезают.
- Playlist cards показывают реальные provider, track count, collected percent
  и сегменты READY/NEEDS_REVIEW/MISSING/UNMATCHED.
- Сетка реальных плейлистов остаётся в первом экране; длинные формы импорта и
  provider-source cards находятся в доступном раскрывающемся блоке и полностью
  сохраняют прежние действия.
- Existing review, providers, storage, users, players, notifications, imports
  и acquisition controls только restyled; их API semantics не меняются.
- `frontend/service-worker.js` использует cache `audiofeel-v20`, включает
  версионированные stylesheet/app bundle и четыре font subsets.
- При активации поверх `lossless-archive-*` или прежнего `audiofeel-*` cache
  worker удаляет старый shell, перехватывает clients и один раз перезагружает
  открытые окна. Это не позволяет старой SPA оставаться после deploy.

## Design tokens

- Background: `#0A0B0E`.
- Surfaces: `#121419`, `#181B22`, `#1F232B`.
- Primary text: `#ECEDF0`; secondary: `#A9B0BC`.
- Accent: amber `#E9A63F`; semantic green/yellow/red/blue remain distinct
  and status labels never rely on color alone.
- UI font: Inter; headings: Literata.
- Radius scale: 8/12/20 px; pill only for statuses, filters and version.

Fonts are self-hosted WOFF2 in `frontend/fonts/` under their OFL files.
Production HTML performs no runtime request to Google Fonts.

## Security and product boundaries

The prototype is a reference, not production data. Its inline users,
credentials, provider values, storage figures and music examples are never
copied.

- Roles remain exactly `owner | user`; visual `admin` is not introduced.
- Stored provider/Drive credentials remain write-only and cannot be revealed
  or reconstructed from masked values.
- OpenSubsonic API key is shown only once immediately after creation and stays
  in JS memory; it is not placed in Web Storage or persistent DOM attributes.
- No audio-preview endpoint, bulk mutation, persistent quality-follow mode or
  other unsupported prototype action is introduced.
- Route visibility does not replace backend authorization.
- Service Worker excludes `/api/`, `/rest` and credential-bearing query
  strings from cache.

## Verification gate

Before publication:

1. JavaScript syntax, manifest JSON and `git diff --check`.
2. Backend test suite including health version and local PWA contract.
3. Isolated Compose build/start with separate project name and no production
   ports or volumes.
4. Browser render at desktop, tablet and mobile widths; check login and an
   authenticated fixture/API-backed state without secrets.
5. Recheck Alembic head, production services, Celery, Drive, disk and paused
   jobs. Jobs №28 and №31 must remain paused.
6. Only after explicit approval: commit, push, deploy from immutable SHA,
   recreate services from one release, verify public health and deployed SHA.
7. Create annotated `v1.0.0` tag only after the production deployment gate
   passes.

## Rollback

The redesign does not add a database migration. Rollback switches the
`/opt/audiofeel/app` symlink to the previous immutable release and recreates
the frontend/backend stack from that release. PWA cache name changes, so
clients receive the previous app shell after service-worker activation.
