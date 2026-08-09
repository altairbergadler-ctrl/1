"""Qobuz downloads behind the RESTRICT rules of docs/qobuz-dl-assessment.md.

The qobuz-dl package is imported lazily inside factory functions so the app
and tests start without the optional dependency. Secrets, passwords and auth
tokens are never logged, stored in the database, or included in exceptions.
"""

# ============================================================================
# Обёртка над сторонним пакетом qobuz-dl (пин qobuz-dl==0.9.9.10).
#
# Вся интеграция выполнена на условиях RESTRICT из
# docs/qobuz-dl-assessment.md (итоговое решение — раздел 7):
#   1. Секреты (пароль / user_auth_token / app-secrets) живут только в .env;
#      они не пишутся в БД, не логируются и не включаются в тексты исключений.
#   2. Скачивание идёт ТОЛЬКО в staging-каталог (QOBUZ_STAGING_PATH); в
#      библиотеку файлы переносит import_files_to_library() после верификации
#      и только из Celery-worker'а (см. docker-compose.yml: rw-mount у worker).
#   3. Один активный qobuz-job, лимит треков за запуск и пауза между
#      скачиваниями — защита от rate-limit/бана со стороны Qobuz (раздел 5).
#   4. Авторизация: основной путь — токен браузерной сессии (addendum,
#      раздел 8: Qobuz перевёл вход на OAuth и user/login отвечает 401),
#      fallback — email + MD5(password), как в CLI апстрима.
#
# Пакет импортируется лениво (внутри функций), чтобы приложение и тесты
# поднимались без установленной зависимости (assessment §1: используем
# qobuz-dl только как библиотеку — bundle/qopy/downloader, без CLI).
# ============================================================================

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from mutagen import File as MutagenFile
from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Match, MatchStatus, Playlist, PlaylistItem

# --- Константы интеграции ---------------------------------------------------

# Форматы, которые Qobuz реально отдаёт на скачивание: FLAC (lossless/hi-res)
# либо MP3 (quality=5). Только эти расширения проходят верификацию staging
# (assessment §3.5: allowlist расширений до переноса в библиотеку).
AUDIO_EXTENSIONS = frozenset({".flac", ".mp3"})

# Redis-кэш извлечённых app_id/secrets веб-плеера Qobuz. Извлечение из
# JS-бандла play.qobuz.com — хрупкая точка (assessment §3.2): смена вёрстки
# ломает регулярные выражения, поэтому результат кэшируется на ~7 дней и
# перевытягивается только при промахе кэша или InvalidAppSecretError.
_BUNDLE_CACHE_KEY = "qobuz:bundle:v1"
_BUNDLE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# Пороги выбора кандидата при поиске трека в каталоге Qobuz — сознательно
# совпадают с fuzzy-каскадом services/matcher.py (порог 85, допуск ±5 с),
# чтобы докачка была не строже и не слабее локального матчинга.
_SEARCH_THRESHOLD = 85.0
_DURATION_TOLERANCE_MS = 5_000

# Ограничение MVP: по URL скачиваем только альбомы и отдельные треки.
# playlist/artist/label осознанно не поддерживаются — отклоняем с понятной
# ошибкой (задокументировано в README, раздел «Qobuz»).
_SUPPORTED_URL_TYPES = frozenset({"album", "track"})


class QobuzServiceError(RuntimeError):
    """Base error for Qobuz integration failures."""


class QobuzConfigurationError(QobuzServiceError):
    """Qobuz is disabled, credentials are missing, or the package is absent."""


# QobuzAuthError и QobuzConfigurationError завершают задание БЕЗ retry
# (см. qobuz_download_task): повторная попытка с теми же неверными
# креденшелами бессмысленна и лишь рискует вызвать временный бан аккаунта.
class QobuzAuthError(QobuzServiceError):
    """Qobuz rejected the configured account credentials."""


# Всё остальное (сеть, протухший bundle, нестримабельный контент) —
# QobuzProviderError: такие сбои потенциально временные, поэтому задание
# уходит в стандартный retry-паттерн Celery-задач.
class QobuzProviderError(QobuzServiceError):
    """Qobuz could not complete a remote operation."""


# Унифицированная карточка результата поиска по каталогу Qobuz (трек или
# альбом). url собираем сами в каноническом виде play.qobuz.com/<type>/<id> —
# его фронтенд показывает пользователю и принимает обратно в download-url.
@dataclass(frozen=True, slots=True)
class QobuzSearchCandidate:
    qobuz_id: str
    artist: str
    title: str
    album: str
    duration_ms: int | None
    isrc: str | None
    hires: bool
    url: str


# Признак «интеграция настроена»: флаг включён И задан хотя бы один способ
# авторизации — либо токен браузерной сессии (основной путь, addendum
# assessment §8), либо пара email+password (fallback на случай, если Qobuz
# снова починит классический user/login).
def is_qobuz_configured(config: Any = settings) -> bool:
    if not config.qobuz_enabled:
        return False
    if str(getattr(config, "qobuz_auth_token", "") or "").strip():
        return True
    return bool(
        str(config.qobuz_email or "").strip() and config.qobuz_password
    )


def _qobuz_modules() -> SimpleNamespace:
    """Import qobuz-dl lazily; the app must boot without the package."""

    # Ленивый импорт: qobuz-dl — опциональная зависимость (assessment §1:
    # используется только как библиотека). При отсутствии пакета приложение и
    # весь тестовый набор обязаны подниматься, а понятная ошибка должна
    # появляться только при реальной попытке обратиться к Qobuz.
    try:
        from qobuz_dl import bundle as bundle_module
        from qobuz_dl import downloader as downloader_module
        from qobuz_dl import qopy as qopy_module
        from qobuz_dl.exceptions import (
            AuthenticationError,
            IneligibleError,
            InvalidAppIdError,
            InvalidAppSecretError,
            InvalidQuality,
            NonStreamable,
        )
        from qobuz_dl.utils import get_url_info
    except ImportError as exc:
        raise QobuzConfigurationError(
            "The qobuz-dl package is not installed in this environment"
        ) from exc
    return SimpleNamespace(
        bundle=bundle_module,
        downloader=downloader_module,
        qopy=qopy_module,
        get_url_info=get_url_info,
        AuthenticationError=AuthenticationError,
        IneligibleError=IneligibleError,
        InvalidAppIdError=InvalidAppIdError,
        InvalidAppSecretError=InvalidAppSecretError,
        InvalidQuality=InvalidQuality,
        NonStreamable=NonStreamable,
    )


def _load_cached_bundle() -> tuple[str, list[str]] | None:
    """Read app_id/secrets from Redis; cache failures are non-fatal."""

    # Кэш bundle (app_id + secrets веб-плеера) в Redis — митигация хрупкого
    # извлечения из JS-бандла (assessment §3.2). Redis недоступен? Работаем
    # без кэша: любая ошибка Redis глотается, bundle просто будет вытянут
    # заново. Паттерн доступа к Redis повторяет services/spotify.py.
    try:
        from redis import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        raw = client.get(_BUNDLE_CACHE_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        app_id = str(data["app_id"])
        secrets = [str(secret) for secret in data["secrets"]]
    except (KeyError, TypeError, ValueError):
        return None
    if not app_id or not secrets:
        return None
    return app_id, secrets


def _store_bundle_cache(app_id: str, secrets: Iterable[str]) -> None:
    try:
        from redis import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.set(
            _BUNDLE_CACHE_KEY,
            json.dumps({"app_id": app_id, "secrets": list(secrets)}),
            ex=_BUNDLE_CACHE_TTL_SECONDS,
        )
    except Exception:
        pass


def _drop_bundle_cache() -> None:
    try:
        from redis import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.delete(_BUNDLE_CACHE_KEY)
    except Exception:
        pass


def _fetch_bundle() -> tuple[str, list[str]]:
    modules = _qobuz_modules()
    try:
        # Bundle() качает login-страницу play.qobuz.com и её JS-бандл,
        # извлекая appId и набор seed-секретов регулярками (assessment §3.2).
        # Любой сбой (сеть, смена вёрстки) — это сбой провайдера, а не
        # креденшелов, поэтому QobuzProviderError → задание уйдёт в retry.
        bundle = modules.bundle.Bundle()
        app_id = str(bundle.get_app_id())
        secrets = [str(secret) for secret in bundle.get_secrets().values()]
    except Exception as exc:
        raise QobuzProviderError(
            "Qobuz app bundle could not be extracted from play.qobuz.com"
        ) from exc
    if not app_id or not secrets:
        raise QobuzProviderError("Qobuz app bundle extraction returned no secrets")
    _store_bundle_cache(app_id, secrets)
    return app_id, secrets


def _get_bundle(*, force_refresh: bool = False) -> tuple[str, list[str]]:
    # Сначала Redis-кэш; force_refresh=True используется второй попыткой
    # create_qobuz_client после InvalidAppSecretError (протухший секрет в
    # кэше — штатный сценарий ротации bundle на стороне Qobuz).
    if not force_refresh:
        cached = _load_cached_bundle()
        if cached is not None:
            return cached
    return _fetch_bundle()


def _fetch_user_label(client) -> str | None:
    """Best-effort membership label via user/get; never fatal."""

    # Тариф аккаунта (short_label, например "Studio") нужен только как
    # человекочитаемое подтверждение подписки в ответе /connect. При token-
    # авторизации qopy.Client.label не заполняется (нет вызова user/login),
    # поэтому читаем user/get отдельно; QOBUZ_USER_ID берём из .env.
    # Любая ошибка — не фатальна: клиент просто получит label "token".
    user_id = str(settings.qobuz_user_id or "").strip()
    if not user_id:
        return None
    try:
        data = client.api_call("user/get", user_id=user_id)
        return (
            data.get("credential", {})
            .get("parameters", {})
            .get("short_label")
        )
    except Exception:
        return None


def _build_token_client(modules, app_id: str, secrets: list[str]):
    """Authenticate with a browser-session user_auth_token.

    Qobuz moved web login to OAuth, so the classic user/login flow can reject
    even valid email+password pairs. The play.qobuz.com session token is sent
    as the X-User-Auth-Token header instead; cfg_setup still validates the
    extracted app secrets via a signed track/getFileUrl probe.
    """

    # Workaround из addendum (assessment §8): Qobuz перевёл веб-вход на OAuth,
    # и user/login стабильно отвечает 401 даже с верной парой email+пароль.
    # Поэтому собираем qopy.Client вручную через __new__, МИНУЯ его __init__
    # (там auth() → user/login). Токен сессии владелец извлекает из Local
    # Storage браузера (ключ localuser) и кладёт в .env; токен — секрет того
    # же класса, что пароль: не логируем и не включаем в исключения.
    import requests

    token = settings.qobuz_auth_token.strip()
    client = modules.qopy.Client.__new__(modules.qopy.Client)
    client.secrets = secrets
    client.id = str(app_id)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0",
            "X-App-Id": str(app_id),
            "Content-Type": "application/json;charset=UTF-8",
            "X-User-Auth-Token": token,
        }
    )
    client.session = session
    client.base = "https://www.qobuz.com/api.json/0.2/"
    client.sec = None
    client.uat = token
    client.label = None
    try:
        # cfg_setup() перебирает извлечённые из bundle секреты и проверяет
        # каждый подписанным запросом track/getFileUrl — заодно это живой
        # тест того, что и app_id/secrets, и токен сессии рабочие.
        client.cfg_setup()
    except modules.InvalidAppSecretError:
        # Секреты bundle протухли → create_qobuz_client сбросит кэш и
        # перевытянет bundle один раз.
        raise
    except requests.exceptions.HTTPError as exc:
        # 4xx от API при валидных секретах = токен протух/отозван
        # (assessment §8: восстановление — повторное извлечение из браузера).
        # В сообщении — только инструкция, сам токен не включаем.
        raise QobuzAuthError(
            "Qobuz rejected the session token; re-extract QOBUZ_AUTH_TOKEN from the browser"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise QobuzProviderError("Qobuz is unavailable") from exc
    label = _fetch_user_label(client)
    client.label = label or "token"
    return client


def create_qobuz_client():
    """Build an authenticated qopy.Client from env settings only.

    Token auth (QOBUZ_AUTH_TOKEN) is preferred; the email+password fallback
    sends the password exclusively as an MD5 hex digest, exactly like the
    upstream CLI does before initializing qopy.Client.
    """

    modules = _qobuz_modules()
    if not is_qobuz_configured():
        raise QobuzConfigurationError(
            "QOBUZ_ENABLED plus QOBUZ_AUTH_TOKEN (or QOBUZ_EMAIL and "
            "QOBUZ_PASSWORD) must be configured"
        )
    # Приоритет — токен браузерной сессии (OAuth-workaround, assessment §8).
    # MD5 пароля вычисляем только если токена нет: лишний раз не трогаем
    # секрет, который не понадобится.
    use_token = bool(str(settings.qobuz_auth_token or "").strip())
    password_md5 = (
        None
        if use_token
        else hashlib.md5(settings.qobuz_password.encode("utf-8")).hexdigest()
    )
    # Две попытки: первая может упасть на InvalidAppSecretError из-за
    # протухшего кэша bundle — тогда сбрасываем кэш и повторяем с свежим.
    # Пароль наружу уходит только как MD5-хеш по HTTPS (assessment §3.1,
    # §5: открытый пароль за пределы процесса не передаётся).
    for attempt in range(2):
        app_id, secrets = _get_bundle(force_refresh=attempt == 1)
        try:
            if use_token:
                return _build_token_client(modules, app_id, secrets)
            return modules.qopy.Client(
                settings.qobuz_email.strip(),
                password_md5,
                app_id,
                secrets,
            )
        except modules.InvalidAppSecretError as exc:
            if attempt == 0:
                _drop_bundle_cache()
                continue
            raise QobuzProviderError(
                "Qobuz app secret is invalid even after a bundle refresh"
            ) from exc
        except (modules.AuthenticationError, modules.IneligibleError) as exc:
            raise QobuzAuthError(
                "Qobuz rejected the configured credentials"
            ) from exc
        except modules.InvalidAppIdError as exc:
            raise QobuzAuthError("Qobuz rejected the extracted app id") from exc
        except QobuzServiceError:
            raise
        except Exception as exc:
            raise QobuzProviderError("Qobuz client initialization failed") from exc
    raise QobuzProviderError("Qobuz client initialization failed")


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


# --- Маппинг ответов поиска Qobuz в унифицированные карточки ----------------
#
# qopy.Client.search_tracks/search_albums возвращают сырой JSON Qobuz API
# вида {"tracks"|"albums": {"items": [...]}}. Здесь он приводится к
# QobuzSearchCandidate: у трека исполнитель лежит в performer.name, длительность
# — в секундах (переводим в мс, как везде в проекте), признак Hi-Res — в
# hires_streamable. Кривые/неполные элементы молча пропускаем: поиск — это
# best-effort витрина, а не повод падать.


def _track_candidate(raw: Any) -> QobuzSearchCandidate | None:
    if not isinstance(raw, dict):
        return None
    qobuz_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not qobuz_id or not title:
        return None
    performer = raw.get("performer") if isinstance(raw.get("performer"), dict) else {}
    album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    duration_seconds = _positive_int(raw.get("duration"))
    return QobuzSearchCandidate(
        qobuz_id=qobuz_id,
        artist=str(performer.get("name") or "").strip(),
        title=title,
        album=str(album.get("title") or "").strip(),
        duration_ms=duration_seconds * 1000 if duration_seconds else None,
        isrc=str(raw.get("isrc") or "").strip() or None,
        hires=bool(raw.get("hires_streamable")),
        url=f"https://play.qobuz.com/track/{qobuz_id}",
    )


def _album_candidate(raw: Any) -> QobuzSearchCandidate | None:
    if not isinstance(raw, dict):
        return None
    qobuz_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not qobuz_id or not title:
        return None
    artist = raw.get("artist") if isinstance(raw.get("artist"), dict) else {}
    duration_seconds = _positive_int(raw.get("duration"))
    return QobuzSearchCandidate(
        qobuz_id=qobuz_id,
        artist=str(artist.get("name") or "").strip(),
        title=title,
        album=title,
        duration_ms=duration_seconds * 1000 if duration_seconds else None,
        isrc=None,
        hires=bool(raw.get("hires_streamable")),
        url=f"https://play.qobuz.com/album/{qobuz_id}",
    )


def _search_items(response: Any, key: str) -> list[Any]:
    if not isinstance(response, dict):
        return []
    container = response.get(key)
    if not isinstance(container, dict):
        return []
    items = container.get("items")
    return items if isinstance(items, list) else []


def search_tracks(client: Any, query: str, limit: int) -> list[QobuzSearchCandidate]:
    try:
        # Любой сбой вызова (сеть, 5xx, протухшая сессия в виде 4xx) —
        # сбой провайдера, а не приложения: маппим в QobuzProviderError,
        # текст ответа API в сообщение не включаем (там могут быть
        # чувствительные поля).
        response = client.search_tracks(query, limit)
    except QobuzServiceError:
        raise
    except Exception as exc:
        raise QobuzProviderError("Qobuz track search failed") from exc
    return [
        candidate
        for candidate in (_track_candidate(raw) for raw in _search_items(response, "tracks"))
        if candidate is not None
    ]


def search_albums(client: Any, query: str, limit: int) -> list[QobuzSearchCandidate]:
    try:
        response = client.search_albums(query, limit)
    except QobuzServiceError:
        raise
    except Exception as exc:
        raise QobuzProviderError("Qobuz album search failed") from exc
    return [
        candidate
        for candidate in (_album_candidate(raw) for raw in _search_items(response, "albums"))
        if candidate is not None
    ]


def select_best_track_candidate(
    *,
    artist_raw: str | None,
    title_raw: str | None,
    duration_ms: int | None,
    candidates: Iterable[QobuzSearchCandidate],
) -> QobuzSearchCandidate | None:
    """Pick the best fuzzy match like the library matcher does (85 / ±5 s)."""

    # Выбор лучшего кандидата из поисковой выдачи Qobuz. Логика сознательно
    # повторяет fuzzy-каскад services/matcher.py: rapidfuzz token_set_ratio по
    # связке "artist title", порог 85, допуск по длительности ±5 с (допуск
    # применяется, только когда обе длительности известны — иначе не
    # отсекаем потенциально верный вариант). Докачиваем только уверенное
    # совпадение: лучше оставить трек MISSING, чем скачать чужую запись.
    item_key = f"{artist_raw or ''} {title_raw or ''}".strip()
    if not item_key:
        return None
    best: QobuzSearchCandidate | None = None
    best_score = 0.0
    for candidate in candidates:
        if (
            duration_ms is not None
            and candidate.duration_ms is not None
            and abs(duration_ms - candidate.duration_ms) > _DURATION_TOLERANCE_MS
        ):
            continue
        score = float(
            token_set_ratio(item_key, f"{candidate.artist} {candidate.title}")
        )
        if score < _SEARCH_THRESHOLD or score <= best_score:
            continue
        best = candidate
        best_score = score
    return best


def _staging_snapshot(staging: Path) -> set[Path]:
    # Снапшот файлов staging ДО скачивания. downloader.Download молча глотает
    # часть ошибок (assessment §3.3, дефект 3: ошибки тегирования пишутся в
    # лог, а не в исключение), поэтому факт результата определяем не по
    # возврату функции, а по разности снапшотов «после − до».
    if not staging.exists():
        return set()
    return {
        path.resolve()
        for path in staging.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def verify_staging_files(files: Iterable[Path]) -> tuple[list[Path], list[dict]]:
    """Keep only parseable audio; everything else stays for the report."""

    # Верификация перед переносом в библиотеку (assessment §3.5 и §5: защита
    # от битых/подменённых файлов). Критерии приёмки КАЖДОГО файла:
    #   1. расширение в allowlist (.flac/.mp3 — cover.jpg, booklet.pdf и
    #      прочие не-аудио артефакты downloader'а отбраковываются здесь);
    #   2. файл существует и размер > 0;
    #   3. mutagen.File реально парсит контейнер;
    #   4. info.length > 0 — в файле есть звуковая дорожка, а не мусор.
    # Отбракованное остаётся в staging и попадает в отчёт rejected.
    verified: list[Path] = []
    rejected: list[dict] = []
    for file in files:
        path = Path(file)
        reason: str | None = None
        if path.suffix.casefold() not in AUDIO_EXTENSIONS:
            reason = "unsupported extension"
        elif not path.is_file() or path.stat().st_size <= 0:
            reason = "empty or missing file"
        else:
            try:
                audio = MutagenFile(path)
            except Exception:
                audio = None
            length = getattr(getattr(audio, "info", None), "length", None)
            if audio is None:
                reason = "mutagen could not parse the file"
            elif not isinstance(length, (int, float)) or length <= 0:
                reason = "audio has no duration"
        if reason is None:
            verified.append(path)
        else:
            rejected.append({"path": str(path), "reason": reason})
    return verified, rejected


def _new_verified_files(staging: Path, before: set[Path]) -> tuple[list[Path], list[dict]]:
    new_files = sorted(_staging_snapshot(staging) - before)
    return verify_staging_files(new_files)


def _map_download_error(modules: SimpleNamespace, exc: Exception) -> QobuzServiceError:
    # Единая точка маппинга исключений qobuz-dl в наши типы. Важно для
    # retry-политики задания: QobuzAuthError/QobuzConfigurationError падают
    # БЕЗ retry (неверные креденшелы/качество повтором не лечатся),
    # QobuzProviderError — временный сбой, уходит в retry.
    # Тексты сообщений никогда не включают ответы API с секретами/токенами.
    if isinstance(exc, QobuzServiceError):
        return exc
    if isinstance(exc, (modules.AuthenticationError, modules.IneligibleError)):
        return QobuzAuthError("Qobuz rejected the configured credentials")
    if isinstance(exc, modules.InvalidAppSecretError):
        return QobuzProviderError("Qobuz app secret was rejected during download")
    if isinstance(exc, modules.InvalidQuality):
        return QobuzConfigurationError(
            "QOBUZ_QUALITY must be one of 5, 6, 7 or 27"
        )
    if isinstance(exc, modules.NonStreamable):
        return QobuzProviderError("Qobuz item is not streamable")
    return QobuzProviderError("Qobuz download failed")


def download_track_to_staging(
    client: Any,
    track_id: str,
    staging_dir: str | Path,
    quality: int,
    embed_art: bool,
) -> list[Path]:
    modules = _qobuz_modules()
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    before = _staging_snapshot(staging)
    try:
        # downloader.Download из qobuz-dl: скачивает во временный .tmp,
        # тегирует mutagen и переименовывает в "Исполнитель - Альбом (год)
        # [24B-96kHz]/NN. Title.flac" ВНУТРИ staging (assessment §3.3).
        # downgrade_quality=True: если релиз недоступен в QOBUZ_QUALITY,
        # докачиваем лучшее доступное качество вместо молчаливого пропуска —
        # MISSING-трек лучше иметь в CD-качестве, чем не иметь вовсе.
        modules.downloader.Download(
            client,
            str(track_id),
            str(staging),
            int(quality),
            embed_art=embed_art,
            downgrade_quality=True,
        ).download_id_by_type(track=True)
    except Exception as exc:
        raise _map_download_error(modules, exc) from exc
    # Возвращаем только НОВЫЕ и ПРОВЕРЕННЫЕ файлы (разность снапшотов +
    # mutagen-верификация); отбракованное остаётся в staging для отчёта.
    verified, _rejected = _new_verified_files(staging, before)
    return verified


def download_url_to_staging(
    client: Any,
    url: str,
    staging_dir: str | Path,
    quality: int,
    embed_art: bool,
) -> list[Path]:
    modules = _qobuz_modules()
    # get_url_info из qobuz-dl парсит ссылки вида play.qobuz.com/<type>/<id>
    # (а также open./www. и региональные префиксы). None — не ссылка Qobuz.
    info = modules.get_url_info(str(url or ""))
    if not info:
        raise QobuzProviderError("URL is not a recognized Qobuz link")
    kind, item_id = info
    if kind not in _SUPPORTED_URL_TYPES:
        # Ограничение MVP: только album и track. playlist/artist/label
        # осознанно отклоняем понятной ошибкой (задокументировано в README),
        # а не пытаемся качать тысячи треков одной ссылкой.
        raise QobuzProviderError(
            f"Qobuz '{kind}' URLs are not supported in the MVP; "
            "only album and track URLs can be downloaded"
        )
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    before = _staging_snapshot(staging)
    try:
        modules.downloader.Download(
            client,
            str(item_id),
            str(staging),
            int(quality),
            embed_art=embed_art,
            downgrade_quality=True,
        ).download_id_by_type(track=kind == "track")
    except Exception as exc:
        raise _map_download_error(modules, exc) from exc
    verified, _rejected = _new_verified_files(staging, before)
    return verified


def _cleanup_empty_staging_dirs(staging: Path) -> None:
    # После успешного переноса убираем опустевшие папки альбомов в staging,
    # чтобы каталог не зарастал пустыми оболочками. Непустые (конфликты,
    # cover.jpg, booklet.pdf, отбракованные файлы) rmdir не трогает — OSError
    # просто игнорируется.
    if not staging.exists():
        return
    directories = sorted(
        (path for path in staging.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def import_files_to_library(
    files: Iterable[Path],
    staging_dir: str | Path,
    library_path: str | Path,
) -> dict:
    """Move verified audio into the library without overwriting anything."""

    # Перенос верифицированного аудио из staging в библиотеку (assessment
    # §3.5: staging → верификация → перемещение). Правила безопасности:
    #   1. относительная структура папок сохраняется (папка альбома, созданная
    #      downloader'ом, воспроизводится в библиотеке);
    #   2. containment-проверка, как в services/delivery.py: resolved-путь
    #      назначения обязан остаться внутри MUSIC_LIBRARY_PATH — защита от
    #      выхода за корень через «../» в именах;
    #   3. существующие файлы НИКОГДА не перезаписываются: конфликт имён
    #      остаётся лежать в staging и попадает в отчёт conflicts — повторная
    #      докачка не затирает уже собранную коллекцию.
    staging_root = Path(staging_dir).expanduser().resolve()
    library_root = Path(library_path).expanduser().resolve()
    library_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, list] = {"imported": [], "conflicts": [], "rejected": []}
    for file in files:
        source = Path(file).expanduser().resolve()
        try:
            relative = source.relative_to(staging_root)
        except ValueError:
            report["rejected"].append(
                {"path": str(file), "reason": "file is outside the staging area"}
            )
            continue
        target = (library_root / relative).resolve()
        if not target.is_relative_to(library_root):
            report["rejected"].append(
                {"path": str(file), "reason": "target escapes the library root"}
            )
            continue
        if target.exists():
            report["conflicts"].append(
                {"path": str(file), "target": str(target), "reason": "already exists"}
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        report["imported"].append(str(target))
    _cleanup_empty_staging_dirs(staging_root)
    return report


def _missing_items(db: Session, playlist: Playlist, limit: int) -> list[PlaylistItem]:
    # Выбираем только иtems, у которых ЕСТЬ Match-запись со статусом MISSING:
    # join с matches — намеренный фильтр. UNMATCHED-иtems (без Match-записи)
    # не трогаем: они ещё не проходили матчинг, и качать их вслепую нельзя —
    # вдруг трек уже есть в библиотеке, просто не сматчен. Ограничение
    # QOBUZ_MAX_TRACKS_PER_RUN — анти-бан лимит за один запуск (assessment §5).
    statement = (
        select(PlaylistItem)
        .join(Match, Match.playlist_item_id == PlaylistItem.id)
        .where(
            PlaylistItem.playlist_id == playlist.id,
            Match.status == MatchStatus.missing,
        )
        .order_by(PlaylistItem.position, PlaylistItem.id)
        .limit(limit)
    )
    return list(db.scalars(statement))


def fetch_missing_tracks(
    db: Session,
    playlist: Playlist,
    client: Any,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[dict, list[Path]]:
    """Download MISSING playlist items into the staging area, one by one."""

    # Докачка MISSING-треков плейлиста из каталога Qobuz. Обработка строго
    # последовательная (assessment §5: один активный job + задержка между
    # скачиваниями — защита аккаунта от rate-limit/бана; §3.3, дефект 1:
    # зависший download без timeout митигируется stale-таймаутом задания и
    # heartbeat'ами, которые worker шлёт через progress_callback после
    # каждого трека).
    #
    # Для каждого иtema: поиск "{artist_raw} {title_raw}" (top-5) →
    # select_best_track_candidate (порог 85, ±5 с) → скачивание в staging.
    # Ошибка одного трека не роняет весь прогон: фиксируется в items[].status
    # (downloaded / not_found / failed) и счётчиках сводки.
    max_tracks = settings.qobuz_max_tracks_per_run
    items = _missing_items(db, playlist, max_tracks)
    summary: dict[str, Any] = {
        "playlist_id": playlist.id,
        "total_missing": len(items),
        "attempted": 0,
        "downloaded": 0,
        "not_found": 0,
        "failed": 0,
        "items": [],
    }
    collected: list[Path] = []
    for item in items:
        entry: dict[str, Any] = {
            "item_id": item.id,
            "artist": item.artist_raw,
            "title": item.title_raw,
        }
        query = f"{item.artist_raw or ''} {item.title_raw or ''}".strip()
        try:
            if not query:
                raise QobuzProviderError("Playlist item has no artist/title query")
            summary["attempted"] += 1
            candidates = search_tracks(client, query, limit=5)
            best = select_best_track_candidate(
                artist_raw=item.artist_raw,
                title_raw=item.title_raw,
                duration_ms=item.duration_ms,
                candidates=candidates,
            )
            if best is None:
                summary["not_found"] += 1
                entry["status"] = "not_found"
            else:
                entry["qobuz_track_id"] = best.qobuz_id
                files = download_track_to_staging(
                    client,
                    best.qobuz_id,
                    settings.qobuz_staging_path,
                    settings.qobuz_quality,
                    settings.qobuz_embed_art,
                )
                if files:
                    collected.extend(files)
                    summary["downloaded"] += 1
                    entry["status"] = "downloaded"
                else:
                    # downloader отработал без исключений, но новых
                    # проверенных аудиофайлов не появилось (демо-фрагмент,
                    # нестримабельность, битый файл) — фиксируем failed,
                    # битые артефакты остаются в staging для отчёта.
                    summary["failed"] += 1
                    entry["status"] = "failed"
                    entry["error"] = "download produced no verified audio"
            # Обязательная пауза между скачиваниями (assessment §5, §7 п. 4:
            # лимиты и задержки против rate-limit/бана аккаунта Qobuz).
            time.sleep(settings.qobuz_request_delay_seconds)
        except QobuzServiceError as exc:
            summary["failed"] += 1
            entry["status"] = "failed"
            entry["error"] = type(exc).__name__
        summary["items"].append(entry)
        if progress_callback is not None:
            progress_callback(
                {key: value for key, value in summary.items() if key != "items"}
            )
    return summary, collected
