"""Cliente TCP del chat y lector de eventos JSON."""

from __future__ import annotations

import queue
import socket
import threading
import uuid
from typing import Any

from chat_protocol import JsonLineDecoder, JsonObject, ProtocolDecodeError, encode_message


class ChatClient:
    """Capa TCP del cliente.

    ``events`` es una cola segura para hilos. El hilo lector agrega allí las
    respuestas del servidor y la interfaz Tkinter las consume con ``after``;
    ningún hilo de red modifica widgets.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user_id: str,
        *,
        connection_timeout: float = 5.0,
    ) -> None:
        if not host.strip():
            raise ValueError("El host no puede estar vacío")
        if not 1 <= port <= 65535:
            raise ValueError("El puerto debe estar entre 1 y 65535")
        if not user_id.strip():
            raise ValueError("El userId no puede estar vacío")

        self.host = host
        self.port = port
        self.user_id = user_id.strip()
        self.connection_timeout = connection_timeout
        self.events: queue.Queue[JsonObject] = queue.Queue()

        self._socket: socket.socket | None = None
        self._socket_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

    @property
    def is_open(self) -> bool:
        """Indica si existe una conexión TCP activa en el cliente."""

        with self._socket_lock:
            return self._socket is not None and not self._stop_event.is_set()

    def connect(self) -> None:
        """Abre TCP, inicia el lector y envía ``CONNECT`` como primer mensaje."""

        with self._socket_lock:
            if self._socket is not None:
                raise RuntimeError("El cliente ya tiene una conexión TCP")

        self._stop_event.clear()
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connection_timeout
            )
            sock.settimeout(None)
        except OSError as exc:
            self._queue_client_error(
                "CONNECT_FAILED", f"No se pudo conectar a {self.host}:{self.port}: {exc}"
            )
            raise

        with self._socket_lock:
            self._socket = sock

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="chat-reader",
            daemon=True,
        )
        self._reader_thread.start()

        try:
            self._send_operation("CONNECT", userId=self.user_id)
        except (ConnectionError, OSError) as exc:
            self._queue_client_error(
                "CONNECT_FAILED", f"No se pudo enviar CONNECT: {exc}"
            )
            self.close()
            raise

    def close(self) -> None:
        """Cierra la conexión y despierta al hilo lector si estaba bloqueado."""

        self._stop_event.set()
        with self._socket_lock:
            sock = self._socket
            self._socket = None

        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        reader = self._reader_thread
        if (
            reader is not None
            and reader.is_alive()
            and reader is not threading.current_thread()
        ):
            reader.join(timeout=1.0)

    def send_private(self, recipient: str, text: str) -> str:
        """Envía un mensaje privado y devuelve su ``requestId``."""

        normalized_recipient = recipient.strip()
        if not normalized_recipient:
            raise ValueError("El destinatario no puede estar vacío")
        if normalized_recipient == self.user_id:
            raise ValueError("No puedes enviarte un mensaje privado a ti mismo")
        if not text.strip():
            raise ValueError("El mensaje privado no puede estar vacío")

        return self._send_operation(
            "PRIVATE_SEND", to=normalized_recipient, text=text
        )

    def join_group(self, group_id: str) -> str:
        """Solicita unirse al grupo indicado."""

        return self._send_operation("GROUP_JOIN", groupId=group_id)

    def leave_group(self, group_id: str) -> str:
        """Solicita salir del grupo indicado."""

        return self._send_operation("GROUP_LEAVE", groupId=group_id)

    def send_group(self, group_id: str, text: str) -> str:
        """Envía un mensaje al grupo indicado."""

        return self._send_operation("GROUP_SEND", groupId=group_id, text=text)

    def _send_operation(self, operation: str, **fields: Any) -> str:
        request_id = f"r-{uuid.uuid4().hex}"
        payload: JsonObject = {
            "type": operation,
            "requestId": request_id,
            **fields,
        }
        self._send_payload(payload)
        return request_id

    def _send_payload(self, payload: JsonObject) -> None:
        wire_message = encode_message(payload)
        with self._send_lock:
            with self._socket_lock:
                sock = self._socket
            if sock is None or self._stop_event.is_set():
                raise ConnectionError("No hay una conexión TCP activa")
            try:
                sock.sendall(wire_message)
            except OSError:
                self._stop_event.set()
                raise

    def _reader_loop(self) -> None:
        with self._socket_lock:
            sock = self._socket
        if sock is None:
            return

        decoder = JsonLineDecoder()
        try:
            while not self._stop_event.is_set():
                # El socket quedó en modo bloqueante tras connect(); close()
                # lo despierta con shutdown, no con un timeout.
                chunk = sock.recv(4096)
                if not chunk:
                    break

                for result in decoder.feed(chunk):
                    if isinstance(result, ProtocolDecodeError):
                        self._queue_client_error(
                            "CLIENT_INVALID_JSON", str(result)
                        )
                    else:
                        self.events.put(result)

            if decoder.has_pending_bytes and not self._stop_event.is_set():
                self._queue_client_error(
                    "CLIENT_INCOMPLETE_MESSAGE",
                    "La conexión terminó con un mensaje JSON sin salto de línea",
                )
        except OSError as exc:
            if not self._stop_event.is_set():
                self._queue_client_error(
                    "CLIENT_NETWORK_ERROR", f"Error leyendo del socket: {exc}"
                )
        finally:
            with self._socket_lock:
                if self._socket is sock:
                    self._socket = None
            try:
                sock.close()
            except OSError:
                pass

            if not self._stop_event.is_set():
                self.events.put(
                    {
                        "type": "DISCONNECTED",
                        "message": "El servidor cerró la conexión",
                    }
                )

    def _queue_client_error(self, code: str, message: str) -> None:
        self.events.put(
            {
                "type": "CLIENT_ERROR",
                "code": code,
                "message": message,
            }
        )

