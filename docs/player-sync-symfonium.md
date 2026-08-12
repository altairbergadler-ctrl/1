# Symfonium: безопасная настройка и phone acceptance

Статус: server-side production gate выполнен 2026-08-12, Alembic находится на
`0010_open_subsonic_players`, публичный `/rest/*` и отключение access logs
проверены. Phone acceptance начат: Symfonium 14.1.0 установлен, отдельный player
credential создан и provider добавлен. Первая sync выявила обязательные для
клиента пустые `getStarred2`, `getBookmarks` и `getGenres`; совместимость
исправлена в code gate `5d3b6b2`, который развёрнут в production. Initial sync
после него завершилась успешно, но импортировала `0` треков из-за специального
`search3 query=""`; нормализация этого запроса исправлена и развёрнута в code
gate `a6b885b`. Следующая sync дошла до альбомов и остановилась на неизвестном
годе, сериализованном как `null`; необязательные неизвестные metadata теперь
опускаются в production gate `041b02a`. Initial sync нужно повторить.

## До подключения

1. Создать и проверить PostgreSQL backup, применить migration
   `0010_open_subsonic_players` и убедиться, что все production-контейнеры healthy.
2. Проверить HTTPS напрямую на `https://audiofeel.su`; не использовать HTTP URL
   с redirect.
3. Убедиться, что Caddy access log для сайта выключен, Uvicorn запущен с
   `--no-access-log`, а frontend proxy не пишет access log.
4. В PWA открыть «Плееры», создать отдельное устройство с понятным label и
   скопировать Server URL и одноразовый API key разными кнопками. Не вставлять
   key в URL, QR, заметки или диагностические сообщения.

## Настройка Symfonium

Добавить media provider типа OpenSubsonic/Subsonic:

- Server URL: HTTPS URL из PWA;
- authentication: OpenSubsonic API key;
- API key: одноразовое значение из PWA;
- качество streaming/download: Original или Lossless;
- transcoding/max bitrate: отключить.

После первого подключения импортировать нужный серверный playlist как
`Read only` или `Online first`. Сервер помечает playlist как `readonly`; изменение
playlist с телефона в scope этой итерации не входит.

Для автоматического офлайна включить у импортированного playlist
`Configure auto offline cache`. Названия пунктов могут отличаться между версиями
Symfonium; итоговая проверка должна подтвердить именно automatic cache, а не
разовую ручную загрузку или общий rolling cache.

### Совместимость с Symfonium 14.1.0

Перед загрузкой основного каталога клиент запрашивает `getStarred2`,
`getBookmarks` и `getGenres`. У Music Service нет серверного состояния избранного,
закладок и жанров, поэтому read-only adapter возвращает корректные пустые
контейнеры этих коллекций в JSON и XML. Ответ `Method is not implemented` на
любой из трёх методов прерывает initial sync до запроса песен.

Для полного обхода каталога Symfonium передаёт в `search3` буквальное значение
`query=""`, а затем отдельно перебирает исполнителей, альбомы и песни страницами.
Adapter трактует это значение как пустой wildcard-запрос. Если воспринимать две
кавычки как обычный поисковый текст, sync завершается без ошибки, но с `0` треков.

Неизвестные необязательные числовые metadata (`year`, track/disc number,
bit depth и sample rate) не должны сериализоваться как JSON `null`: Symfonium
14.1.0 ожидает число либо отсутствие поля. Gate `041b02a` применяет это правило
к album и song DTO; production-каталог проверен на отсутствие `null`.

## Acceptance: initial / add / remove / offline

Фиксировать только обезличенные counts, timestamps, HTTP status и public media
IDs. API key, email, query string и Google identity в отчёт не включать.

1. **Initial sync:** выполнить sync provider, проверить порядок и дубликаты
   READY items, дождаться automatic offline cache и воспроизвести начало/середину
   FLAC. На сервере подтвердить Range `206` и отсутствие transcoding.
2. **Add:** в тестовом пользовательском playlist довести новый item до READY,
   выполнить обычную sync в Symfonium и доказать, что трек появился и скачался
   автоматически без ручной download-команды.
3. **Remove:** удалить item только из серверного playlist, выполнить sync и
   подтвердить исчезновение из импортированного playlist. Физический `File` на
   сервере не должен удалиться. Поведение уже скачанной копии на телефоне нужно
   записать фактически — сервер не обещает её удаление.
4. **Offline:** отключить сеть на телефоне и воспроизвести несколько скачанных
   треков, включая Hi-Res при наличии тестового материала.
5. **Revoke:** вернуть сеть, отозвать credential в PWA и подтвердить, что новый
   `ping`, browse и stream больше не проходят. Уже скачанные байты остаются
   доступными телефону.
6. **Disable:** только на изолированном fixture либо отдельно разрешённом test
   user подтвердить одновременный revoke Google sessions и player credentials.

## Безопасная диагностика

- Не копировать полный `/rest/*` URL: query может содержать `apiKey`.
- Для ошибок фиксировать endpoint без query, status, protocol error code и время.
- Не включать debug access logs. Временная диагностика должна логировать только
  route template и имена параметров, никогда их значения.
- Foreign public ID и несуществующий ID должны давать одинаковую нейтральную
  OpenSubsonic error envelope.
- Если metadata показывает один format/size, а download отдаёт другой источник,
  остановить acceptance: catalog и delivery обязаны использовать один resolver.

## Критерий завершения

Phone acceptance считается пройденной только при доказанном initial/add/remove/
offline/revoke цикле и проверке production SHA. До этого локальная реализация не
считается принятой в production независимо от автоматических тестов.
