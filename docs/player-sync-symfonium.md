# Symfonium: безопасная настройка и phone acceptance

Статус: server-side production gate выполнен 2026-08-12, Alembic находится на
`0010_open_subsonic_players`, публичный `/rest/*` и отключение access logs
проверены. На реальном телефоне runbook ещё не выполнялся. Покупка/установка
приложения и создание реального player credential требуют отдельного
подтверждения.

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
