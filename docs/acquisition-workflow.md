# Автоматическая очередь загрузки и финальные уведомления

## Назначение

После импорта плейлиста из Spotify, Яндекс Музыки, CSV, M3U/M3U8 или текста
сервис автоматически создаёт пользовательское задание `acquisition_workflow`.
Пользователь сразу может закрыть страницу: выполнение хранится в PostgreSQL и
продолжается через Celery.

## Последовательность одного задания

1. Начальный matching определяет текущее состояние элементов.
2. В обычном режиме в работу попадают только `MISSING`; режим
   «Обновлять качество» также проверяет `READY`.
3. Для каждой позиции сначала проверяется Qobuz, затем Яндекс Музыка.
4. Из доступных кандидатов выбирается лучшее качество по lossless, bit depth,
   sample rate и bitrate. При равном результате приоритет детерминирован.
5. В режиме обновления новый файл скачивается только если он лучше лучшего уже
   сохранённого файла. Старые копии не удаляются; каталог и выдача выбирают
   лучший существующий вариант.
6. После каждой пачки выполняются импорт, scan, загрузка в Google Drive,
   проверка удалённого SHA-1/размера и удаление подтверждённой локальной копии.
7. После всех пачек выполняется итоговый matching.
8. Только после итогового matching отправляется одно browser push-уведомление.

Проверка провайдеров разделена от playlist OAuth. Системные Qobuz/Yandex
credentials зашифрованы и доступны только серверным компонентам. Пользователь
не получает и не видит чужие credentials.

## Пачка и пауза

`ACQUISITION_BATCH_SIZE=25` означает 25 позиций плейлиста, а не 25 успешно
скачанных файлов. Часть позиций может отсутствовать у обоих провайдеров, быть
неоднозначной, уже обработанной или не превосходить текущее качество.

Таймер межпакетной паузы начинается перед drain-фазой. Импорт, scan, Drive
upload, проверка и локальная очистка выполняются внутри этого окна. Следующая
пачка разрешена только после успешного drain и истечения оставшейся части
паузы. При свободном месте ниже `QOBUZ_MIN_FREE_BYTES` (production: 5 GiB)
задание безопасно ставится на паузу.

Состояние восстановления хранит ID файлов каталога, поэтому повтор после уже
выполненной Drive-эвакуации не считает очищенный локальный путь потерянным и
не скачивает обработанную позицию заново.

## Очередь нескольких пользователей

Одновременно выполняется не более одной provider/download-пачки. Dispatcher
выбирает только готовые `pending` задания, а после выдачи пачки перемещает все
ожидающие задания этого пользователя в конец очереди. Это даёт round-robin
между пользователями даже если один пользователь поставил много плейлистов.

Уникальный частичный индекс запрещает два активных acquisition-задания для
одного плейлиста. Повторный запрос возвращает существующее задание. Ошибки
пачки повторяются через состояние и `next_run_at` в PostgreSQL; отдельный
Celery retry для той же пачки не создаётся.

Пауза пользователя применяется после активной пачки. На паузе новое задание
этого плейлиста не стартует. Продолжение возвращает ту же запись в очередь и
использует сохранённый курсор и provider attempts.

## Статус в PWA и API

Карточка плейлиста показывает:
Зависшая lease автоматически возвращается в очередь после configured stale
timeout. Web Push endpoint дополнительно ограничен известными browser push
hosts и TCP 443, поэтому подписка не даёт произвольный server-side HTTPS вызов.

- обработанные позиции и всего позиций;
- скачанные файлы;
- подтверждённые загрузки в Drive;
- удалённые локальные копии;
- текущий этап;
- свободное место на диске;
- режим «только отсутствующие» или «обновлять качество».

Основные API:

- `POST /api/acquisition/playlists/{playlist_id}`;
- `GET /api/acquisition/playlists/{playlist_id}`;
- `POST /api/acquisition/jobs/{job_id}/pause`;
- `POST /api/acquisition/jobs/{job_id}/resume`.

Все endpoints проверяют владельца. Существующий чужой ID не раскрывается.

## Browser Push

Используется стандартный Web Push с VAPID. Firebase/Google Cloud приложение
не требуется. Private P-256 VAPID key хранится отдельным Docker secret и не
выводится в API или логах. Endpoint и ключи browser subscription шифруются
существующим server-side vault key; в таблице остаются hash и безопасные
метаданные.

Условия работы:

- production должен быть открыт по HTTPS;
- пользователь один раз нажимает «Включить уведомления» и подтверждает
  разрешение браузера;
- на iPhone/iPad PWA сначала добавляется на домашний экран;
- промежуточные этапы и отдельные пачки уведомлений не создают;
- недоступный push endpoint не переводит успешно завершённую загрузку в ошибку.

ACQUISITION_JOB_STALE_SECONDS=21600
Настройки:

```env
ACQUISITION_ENABLED=true
ACQUISITION_BATCH_SIZE=25
WEB_PUSH_ALLOWED_HOST_SUFFIXES=fcm.googleapis.com,push.services.mozilla.com,updates.push.services.mozilla.com,web.push.apple.com,notify.windows.com
ACQUISITION_DISPATCH_INTERVAL_SECONDS=15
WEB_PUSH_ENABLED=true
WEB_PUSH_VAPID_PRIVATE_KEY_HOST_FILE=/etc/audiofeel/secrets/web-push-vapid-private.pem
WEB_PUSH_VAPID_SUBJECT=mailto:admin@example.com
WEB_PUSH_TTL_SECONDS=86400
```

Создание ключа на production выполняется без вывода private key:

```bash
install -d -m 0700 /etc/audiofeel/secrets
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -out /etc/audiofeel/secrets/web-push-vapid-private.pem
chmod 0400 /etc/audiofeel/secrets/web-push-vapid-private.pem
```

## Миграция и приёмка

Alembic `0012_web_push_subscriptions` добавляет `jobs.next_run_at`,
encrypted push subscriptions и уникальность активного задания плейлиста.
Перед применением обязателен PostgreSQL custom dump и проверка
`pg_restore --list`.

Минимальный production gate:

1. `alembic current` и `alembic heads` равны
   `0012_web_push_subscriptions`.
2. Backend, frontend, PostgreSQL, Redis, worker, beat и sidecars healthy.
3. Celery отвечает `pong`, worker слушает `celery` и `acquisition`.
4. Google Drive account healthy.
5. На тестовом плейлисте подтверждён полный цикл
   provider check → download → import → scan → Drive verify → local eviction →
   final matching.
6. После запроса паузы следующая пачка не начинается.
7. После продолжения provider attempts и курсор не создают повторную загрузку.
8. Push проверяется только как финальное событие; browser permission требует
   пользовательского жеста и не автоматизируется сервером.

