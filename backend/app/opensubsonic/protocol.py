from __future__ import annotations

from dataclasses import dataclass
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.config import settings

API_VERSION = "1.16.1"


@dataclass(slots=True)
class OpenSubsonicError(Exception):
    code: int
    message: str
    http_status: int = 200


def _base(status: str) -> dict:
    return {
        "status": status,
        "version": API_VERSION,
        "type": "audiofeel",
        "serverVersion": settings.release_sha[:12],
        "openSubsonic": True,
    }


def payload(data: dict | None = None) -> dict:
    value = _base("ok")
    value.update(data or {})
    return value


def error_payload(code: int, message: str) -> dict:
    value = _base("failed")
    value["error"] = {"code": code, "message": message}
    return value


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _append(parent: Element, name: str, value) -> None:
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            _append(parent, name, item)
        return
    if not isinstance(value, dict):
        child = SubElement(parent, name)
        child.text = _scalar(value)
        return
    child = SubElement(parent, name)
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, (dict, list)):
            _append(child, key, item)
        else:
            child.set(key, _scalar(item))


def response(request: Request, body: dict, *, status_code: int = 200) -> Response:
    if request.query_params.get("f", "xml").casefold() == "json":
        return JSONResponse(
            {"subsonic-response": body},
            status_code=status_code,
            headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
        )
    root = Element(
        "subsonic-response",
        {"xmlns": "http://subsonic.org/restapi"},
    )
    for key, value in body.items():
        if isinstance(value, (dict, list)):
            _append(root, key, value)
        elif value is not None:
            root.set(key, _scalar(value))
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        status_code=status_code,
        media_type="text/xml; charset=utf-8",
        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
    )


def validate_version(request: Request, *, optional: bool = False) -> None:
    raw = request.query_params.get("v")
    if not raw:
        if optional:
            return
        raise OpenSubsonicError(10, "Required parameter is missing")
    try:
        major, minor, *_ = [int(part) for part in raw.split(".")]
    except (TypeError, ValueError):
        raise OpenSubsonicError(0, "Client version is invalid")
    if (major, minor) < (1, 13):
        raise OpenSubsonicError(20, "Client must upgrade")
    if (major, minor) > (1, 16):
        raise OpenSubsonicError(30, "Server must upgrade")
