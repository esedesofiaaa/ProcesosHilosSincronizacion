"""Framing TCP y utilidades de protocolo JSON del cliente."""

from __future__ import annotations

import json
from typing import Any


JsonObject = dict[str, Any]


class ProtocolDecodeError(ValueError):
    """Indica que una línea recibida no pudo convertirse en un objeto JSON."""


class JsonLineDecoder:
    """Reconstruye objetos JSON desde un flujo TCP fragmentado o agrupado."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def has_pending_bytes(self) -> bool:
        """Indica si quedó una línea incompleta cuando termina el socket."""

        return bool(self._buffer)

    def feed(
        self, chunk: bytes
    ) -> list[JsonObject | ProtocolDecodeError]:
        """Procesa bytes y devuelve mensajes válidos o errores por línea.

        Un mismo ``chunk`` puede contener media línea, varias líneas o una
        combinación de ambas. Un error no descarta las líneas posteriores del
        mismo ``chunk``.
        """

        self._buffer.extend(chunk)
        results: list[JsonObject | ProtocolDecodeError] = []

        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index == -1:
                break

            line = bytes(self._buffer[:newline_index])
            del self._buffer[: newline_index + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line.strip():
                continue

            try:
                text = line.decode("utf-8")
                payload = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                results.append(
                    ProtocolDecodeError(f"JSON recibido inválido: {exc}")
                )
                continue

            if not isinstance(payload, dict):
                results.append(
                    ProtocolDecodeError(
                        "El mensaje JSON recibido no es un objeto"
                    )
                )
                continue

            results.append(payload)

        return results


def encode_message(message: JsonObject) -> bytes:
    """Serializa un objeto como JSON UTF-8 terminado por ``\\n``."""

    if not isinstance(message, dict):
        raise TypeError("El mensaje debe ser un objeto JSON")

    serialized = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return serialized.encode("utf-8") + b"\n"


def _parse_directory_entries(
    raw_entries: Any,
    *,
    identifier_key: str,
    label_keys: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Normaliza entradas de directorio recibidas en el protocolo."""

    if not isinstance(raw_entries, list):
        return []

    parsed: list[tuple[str, str]] = []
    for entry in raw_entries:
        if isinstance(entry, str):
            identifier = entry.strip()
            label = identifier
        elif isinstance(entry, dict):
            value = entry.get(identifier_key)
            if not isinstance(value, str):
                continue
            identifier = value.strip()
            label = identifier
            for key in label_keys:
                candidate = entry.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    label = candidate.strip()
                    break
        else:
            continue

        if identifier:
            parsed.append((identifier, label))

    return parsed


def parse_group_list(event: JsonObject) -> list[tuple[str, str]]:
    """Obtiene ``(groupId, nombre)`` desde un ``GROUP_LIST``."""

    return _parse_directory_entries(
        event.get("groups"),
        identifier_key="groupId",
        label_keys=("name", "displayName"),
    )


def parse_users_list(event: JsonObject) -> list[tuple[str, str]]:
    """Obtiene ``(userId, nombre)`` desde un ``USERS_LIST``."""

    return _parse_directory_entries(
        event.get("users"),
        identifier_key="userId",
        label_keys=("displayName", "name"),
    )


def parse_group_members(event: JsonObject) -> list[tuple[str, str]]:
    """Obtiene ``(userId, nombre)`` desde un ``GROUP_MEMBERS``."""

    return _parse_directory_entries(
        event.get("members"),
        identifier_key="userId",
        label_keys=("displayName", "name"),
    )
