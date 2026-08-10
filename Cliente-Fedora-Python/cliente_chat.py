#!/usr/bin/env python3
"""Cliente de escritorio Tkinter para el chat TCP basado en JSON por líneas.

Uso:
    python3 cliente_chat.py --host 127.0.0.1 --port 5000 --user-id ana

El cliente no implementa el servidor. La interfaz se ejecuta en el hilo
principal de Tkinter y la lectura del socket se realiza en un hilo separado.
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import threading
import uuid
from typing import Any, Iterable

import tkinter as tk
from tkinter import simpledialog


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


def _parse_directory_items(
    raw_items: Any,
    identifier_key: str,
    label_keys: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Extrae únicamente identificadores publicados por un evento opcional.

    Formato esperado por la UI:
    - grupos: strings o objetos con groupId y opcionalmente name.
    - personas/miembros: strings o objetos con userId y opcionalmente
      displayName.
    """

    if not isinstance(raw_items, list):
        return []

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        identifier: str | None = None
        label: str | None = None

        if isinstance(item, str):
            identifier = item.strip()
        elif isinstance(item, dict):
            raw_identifier = item.get(identifier_key)
            if isinstance(raw_identifier, str):
                identifier = raw_identifier.strip()
            for label_key in label_keys:
                raw_label = item.get(label_key)
                if isinstance(raw_label, str) and raw_label.strip():
                    label = raw_label.strip()
                    break

        if identifier and identifier not in seen:
            entries.append((identifier, label or identifier))
            seen.add(identifier)

    return entries


def parse_group_list(event: JsonObject) -> list[tuple[str, str]]:
    """Lee GROUP_LIST sin crear grupos locales ficticios."""

    return _parse_directory_items(
        event.get("groups"),
        "groupId",
        ("name", "groupName", "label"),
    )


def parse_users_list(event: JsonObject) -> list[tuple[str, str]]:
    """Lee USERS_LIST sin crear personas locales ficticias."""

    return _parse_directory_items(
        event.get("users"),
        "userId",
        ("displayName", "name", "label"),
    )


def parse_group_members(event: JsonObject) -> list[tuple[str, str]]:
    """Lee GROUP_MEMBERS sin crear miembros locales ficticios."""

    return _parse_directory_items(
        event.get("members"),
        "userId",
        ("displayName", "name", "label"),
    )


class ChatApp:
    """Interfaz de chat de escritorio; no modifica la capa TCP."""

    COLORS = {
        "background": "#f3f6fb",
        "surface": "#ffffff",
        "sidebar": "#172033",
        "sidebar_hover": "#263652",
        "sidebar_text": "#f4f7fb",
        "sidebar_muted": "#aab6c8",
        "border": "#e2e8f0",
        "text": "#172033",
        "muted": "#718096",
        "accent": "#2f80ed",
        "accent_dark": "#2368c4",
        "bubble_in": "#ffffff",
        "bubble_out": "#dceeff",
        "system": "#eef2f7",
        "success": "#23a36d",
        "danger": "#d9534f",
    }

    def __init__(self, root: tk.Tk, client: ChatClient) -> None:
        self.root = root
        self.client = client
        self._connected = False
        self._closing = False
        self._connect_thread: threading.Thread | None = None

        self._groups: dict[str, str] = {}
        self._manual_groups: set[str] = set()
        self._users: dict[str, str] = {}
        self._members_by_group: dict[str, list[tuple[str, str]]] = {}
        self._group_order: list[str] = []
        self._people_order: list[str] = []
        self._member_order: list[str] = []
        self._messages: dict[tuple[str, str], list[JsonObject]] = {}
        self._selected: tuple[str, str] | None = None
        self._pending_requests: dict[str, JsonObject] = {}
        self._unread: set[tuple[str, str]] = set()

        self.root.title(f"Chat de escritorio · {client.user_id}")
        self.root.geometry("1180x720")
        self.root.minsize(900, 560)
        self.root.configure(bg=self.COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_sidebar()
        self._build_conversation()
        self._build_participants()
        self._refresh_group_list()
        self._refresh_people_list()
        self._refresh_members()
        self._render_selected_conversation()
        self._poll_events()

    def start_connection(self) -> None:
        """Conecta en segundo plano para no bloquear la ventana."""

        self.connection_status_var.set(
            f"Conectando a {self.client.host}:{self.client.port}..."
        )
        self._connect_thread = threading.Thread(
            target=self._connect_in_background,
            name="chat-connect",
            daemon=True,
        )
        self._connect_thread.start()

    def _connect_in_background(self) -> None:
        try:
            self.client.connect()
        except (ConnectionError, OSError, RuntimeError):
            # El detalle ya fue publicado en client.events.
            pass

    def _build_sidebar(self) -> None:
        colors = self.COLORS
        self.sidebar = tk.Frame(
            self.root,
            bg=colors["sidebar"],
            width=250,
            highlightthickness=0,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.rowconfigure(3, weight=1)
        self.sidebar.rowconfigure(5, weight=1)

        profile = tk.Frame(self.sidebar, bg=colors["sidebar"], padx=18, pady=18)
        profile.grid(row=0, column=0, sticky="ew")
        profile.columnconfigure(1, weight=1)
        avatar = tk.Label(
            profile,
            text=self.client.user_id[:1].upper(),
            bg=colors["accent"],
            fg="white",
            width=3,
            height=1,
            font=("TkDefaultFont", 13, "bold"),
        )
        avatar.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        tk.Label(
            profile,
            text=self.client.user_id,
            bg=colors["sidebar"],
            fg=colors["sidebar_text"],
            font=("TkDefaultFont", 10, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew")
        self.connection_status_var = tk.StringVar(value="Desconectado")
        tk.Label(
            profile,
            textvariable=self.connection_status_var,
            bg=colors["sidebar"],
            fg=colors["sidebar_muted"],
            font=("TkDefaultFont", 9),
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", pady=(2, 0))

        separator = tk.Frame(self.sidebar, bg="#2b3952", height=1)
        separator.grid(row=1, column=0, sticky="ew", padx=18)

        groups_header = tk.Frame(self.sidebar, bg=colors["sidebar"], padx=0, pady=0)
        groups_header.grid(row=2, column=0, sticky="ew", padx=18, pady=(18, 7))
        groups_header.columnconfigure(0, weight=1)
        tk.Label(
            groups_header,
            text="GRUPOS",
            bg=colors["sidebar"],
            fg=colors["sidebar_muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.add_group_button = self._make_sidebar_button(
            groups_header,
            text="+",
            command=self._add_group_from_dialog,
            width=3,
        )
        self.add_group_button.grid(row=0, column=1, sticky="e")

        self.group_listbox = self._make_sidebar_listbox(self.sidebar, height=8)
        self.group_listbox.grid(row=3, column=0, sticky="nsew", padx=12)
        self.group_listbox.bind("<<ListboxSelect>>", self._on_group_selected)

        people_header = tk.Frame(self.sidebar, bg=colors["sidebar"], padx=0, pady=0)
        people_header.grid(row=4, column=0, sticky="ew", padx=18, pady=(18, 7))
        tk.Label(
            people_header,
            text="PERSONAS",
            bg=colors["sidebar"],
            fg=colors["sidebar_muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).pack(anchor="w")

        self.people_listbox = self._make_sidebar_listbox(self.sidebar, height=8)
        self.people_listbox.grid(row=5, column=0, sticky="nsew", padx=12, pady=(0, 14))
        self.people_listbox.bind("<<ListboxSelect>>", self._on_person_selected)

    def _build_conversation(self) -> None:
        colors = self.COLORS
        self.center = tk.Frame(self.root, bg=colors["background"])
        self.center.grid(row=0, column=1, sticky="nsew")
        self.center.rowconfigure(1, weight=1)
        self.center.columnconfigure(0, weight=1)

        header = tk.Frame(
            self.center,
            bg=colors["surface"],
            height=76,
            highlightbackground=colors["border"],
            highlightthickness=1,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(1, weight=1)

        self.header_avatar = tk.Label(
            header,
            text="·",
            bg="#e7edf6",
            fg=colors["accent"],
            width=3,
            height=1,
            font=("TkDefaultFont", 13, "bold"),
        )
        self.header_avatar.grid(row=0, column=0, rowspan=2, padx=(22, 12), pady=17)
        self.header_title_var = tk.StringVar(value="Selecciona un grupo o persona")
        self.header_subtitle_var = tk.StringVar(
            value="Las conversaciones aparecerán aquí"
        )
        tk.Label(
            header,
            textvariable=self.header_title_var,
            bg=colors["surface"],
            fg=colors["text"],
            font=("TkDefaultFont", 12, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="sw", pady=(15, 0))
        tk.Label(
            header,
            textvariable=self.header_subtitle_var,
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 9),
            anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(2, 15))

        actions = tk.Frame(header, bg=colors["surface"], padx=18)
        actions.grid(row=0, column=2, rowspan=2, sticky="e")
        self.join_button = self._make_action_button(
            actions, "Unirme", self._join_selected_group
        )
        self.join_button.pack(side="left", padx=(0, 6))
        self.leave_button = self._make_action_button(
            actions, "Salir", self._leave_selected_group, secondary=True
        )
        self.leave_button.pack(side="left")

        messages = tk.Frame(self.center, bg=colors["background"], padx=22, pady=18)
        messages.grid(row=1, column=0, sticky="nsew")
        messages.rowconfigure(0, weight=1)
        messages.columnconfigure(0, weight=1)

        self.message_canvas = tk.Canvas(
            messages,
            bg=colors["background"],
            bd=0,
            highlightthickness=0,
        )
        self.message_canvas.grid(row=0, column=0, sticky="nsew")
        message_scroll = tk.Scrollbar(
            messages,
            orient="vertical",
            command=self.message_canvas.yview,
            bd=0,
            relief="flat",
        )
        message_scroll.grid(row=0, column=1, sticky="ns")
        self.message_canvas.configure(yscrollcommand=message_scroll.set)
        self.message_inner = tk.Frame(self.message_canvas, bg=colors["background"])
        self.message_window = self.message_canvas.create_window(
            (0, 0),
            window=self.message_inner,
            anchor="nw",
        )
        self.message_inner.bind(
            "<Configure>",
            lambda _event: self.message_canvas.configure(
                scrollregion=self.message_canvas.bbox("all")
            ),
        )
        self.message_canvas.bind(
            "<Configure>",
            lambda event: self.message_canvas.itemconfigure(
                self.message_window,
                width=event.width,
            ),
        )

        composer = tk.Frame(
            self.center,
            bg=colors["surface"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            padx=18,
            pady=13,
        )
        composer.grid(row=2, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        self.composer_target_var = tk.StringVar(value="Selecciona una conversación")
        tk.Label(
            composer,
            textvariable=self.composer_target_var,
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.composer_entry = tk.Entry(
            composer,
            bg="#f7f9fc",
            fg=colors["text"],
            insertbackground=colors["text"],
            relief="flat",
            highlightbackground=colors["border"],
            highlightcolor=colors["accent"],
            highlightthickness=1,
            font=("TkDefaultFont", 10),
        )
        self.composer_entry.grid(row=1, column=0, sticky="ew", ipady=9, padx=(0, 10))
        self.composer_entry.bind("<Return>", self._send_current_message)
        self.send_button = self._make_action_button(
            composer,
            "Enviar",
            self._send_current_message,
        )
        self.send_button.grid(row=1, column=1, sticky="e", ipadx=10, ipady=4)

    def _build_participants(self) -> None:
        colors = self.COLORS
        self.participants = tk.Frame(
            self.root,
            bg=colors["surface"],
            width=225,
            highlightbackground=colors["border"],
            highlightthickness=1,
            padx=16,
            pady=18,
        )
        self.participants.grid(row=0, column=2, sticky="nsew")
        self.participants.grid_propagate(False)
        self.participants.rowconfigure(2, weight=1)

        self.participant_title_var = tk.StringVar(value="MIEMBROS")
        tk.Label(
            self.participants,
            textvariable=self.participant_title_var,
            bg=colors["surface"],
            fg=colors["text"],
            font=("TkDefaultFont", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        self.participant_hint_var = tk.StringVar(value="Selecciona un grupo para ver sus miembros")
        tk.Label(
            self.participants,
            textvariable=self.participant_hint_var,
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 8),
            justify="left",
            wraplength=185,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 12))

        member_box = tk.Frame(self.participants, bg=colors["surface"])
        member_box.grid(row=2, column=0, sticky="nsew")
        member_box.rowconfigure(0, weight=1)
        member_box.columnconfigure(0, weight=1)
        self.member_listbox = tk.Listbox(
            member_box,
            bg=colors["surface"],
            fg=colors["text"],
            selectbackground="#e5effd",
            selectforeground=colors["accent_dark"],
            activestyle="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("TkDefaultFont", 9),
        )
        self.member_listbox.grid(row=0, column=0, sticky="nsew")
        member_scroll = tk.Scrollbar(
            member_box,
            orient="vertical",
            command=self.member_listbox.yview,
            bd=0,
            relief="flat",
        )
        member_scroll.grid(row=0, column=1, sticky="ns")
        self.member_listbox.configure(yscrollcommand=member_scroll.set)
        self.member_listbox.bind("<<ListboxSelect>>", self._on_member_selected)

        self.participant_footer = tk.Label(
            self.participants,
            text="Los listados son opcionales en el protocolo actual.",
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 8),
            justify="left",
            wraplength=185,
            anchor="w",
        )
        self.participant_footer.grid(row=3, column=0, sticky="ew", pady=(16, 0))

        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

    def _make_sidebar_listbox(self, parent: tk.Widget, height: int) -> tk.Listbox:
        colors = self.COLORS
        return tk.Listbox(
            parent,
            height=height,
            bg=colors["sidebar"],
            fg=colors["sidebar_text"],
            selectbackground=colors["accent"],
            selectforeground="white",
            activestyle="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            exportselection=False,
            font=("TkDefaultFont", 10),
        )

    def _make_sidebar_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        width: int = 10,
    ) -> tk.Button:
        colors = self.COLORS
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=colors["sidebar_hover"],
            fg=colors["sidebar_text"],
            activebackground=colors["accent"],
            activeforeground="white",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("TkDefaultFont", 9, "bold"),
            cursor="hand2",
        )

    def _make_action_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        *,
        secondary: bool = False,
    ) -> tk.Button:
        colors = self.COLORS
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#eef4fc" if secondary else colors["accent"],
            fg=colors["accent_dark"] if secondary else "white",
            activebackground="#dce9f9" if secondary else colors["accent_dark"],
            activeforeground=colors["accent_dark"] if secondary else "white",
            disabledforeground="#9aa5b3",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("TkDefaultFont", 9, "bold"),
            cursor="hand2",
        )

    def _poll_events(self) -> None:
        if self._closing:
            return

        while True:
            try:
                event = self.client.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)

        self.root.after(100, self._poll_events)

    def _handle_event(self, event: JsonObject) -> None:
        event_type = event.get("type")

        if event_type == "GROUP_LIST":
            self._handle_group_list(event)
            return
        if event_type == "GROUP_MEMBERS":
            self._handle_group_members(event)
            return
        if event_type == "USERS_LIST":
            self._handle_users_list(event)
            return
        if event_type == "CONNECTED":
            self._connected = True
            self.connection_status_var.set(
                "Conectado · esperando listados opcionales"
            )
            self._update_controls()
            return
        if event_type == "ACK":
            self._handle_ack(event)
            return
        if event_type == "PRIVATE_MESSAGE":
            self._handle_private_message(event)
            return
        if event_type == "GROUP_MESSAGE":
            self._handle_group_message(event)
            return
        if event_type == "ERROR":
            self._handle_server_error(event)
            return
        if event_type == "CLIENT_ERROR":
            self.connection_status_var.set("Error de conexión")
            self._add_system_to_current(
                f"Error local {event.get('code', 'UNKNOWN')}: "
                f"{event.get('message', 'Error de cliente')}"
            )
            return
        if event_type == "DISCONNECTED":
            self._connected = False
            self.connection_status_var.set("Desconectado")
            self._update_controls()
            self._add_system_to_current(
                event.get("message", "El servidor cerró la conexión")
            )
            return

        self.connection_status_var.set(
            f"Evento no visualizado: {event_type or 'sin tipo'}"
        )

    def _handle_group_list(self, event: JsonObject) -> None:
        if "groups" not in event:
            self.connection_status_var.set(
                "GROUP_LIST recibido sin el campo groups"
            )
            return

        entries = parse_group_list(event)
        # El listado del servidor manda, pero no puede borrar grupos añadidos a
        # mano ni aquellos donde ya hay conversación abierta: perderlos dejaría
        # mensajes visibles sin forma de seleccionarlos.
        preserved = {
            group_id: label
            for group_id, label in self._groups.items()
            if group_id in self._manual_groups
            or ("group", group_id) in self._messages
        }
        self._groups = dict(entries)
        self._groups.update(preserved)
        self._refresh_group_list()
        self.connection_status_var.set(
            f"Grupos publicados por el servidor: {len(entries)}"
        )
        self._refresh_members()

    def _handle_group_members(self, event: JsonObject) -> None:
        group_id = event.get("groupId")
        if not isinstance(group_id, str) or not group_id.strip():
            self.connection_status_var.set(
                "GROUP_MEMBERS recibido sin groupId"
            )
            return

        group_id = group_id.strip()
        self._ensure_group(group_id, group_id)
        self._members_by_group[group_id] = parse_group_members(event)
        self._refresh_members()
        self.connection_status_var.set(
            f"Miembros recibidos para {self._groups[group_id]}"
        )

    def _handle_users_list(self, event: JsonObject) -> None:
        if "users" not in event:
            self.connection_status_var.set(
                "USERS_LIST recibido sin el campo users"
            )
            return

        # Igual que con los grupos: quien ya escribió por privado sigue en la
        # lista aunque el servidor deje de publicarlo, para no dejar huérfana
        # una conversación con mensajes.
        preserved = {
            user_id: label
            for user_id, label in self._users.items()
            if ("private", user_id) in self._messages
        }
        self._users = dict(parse_users_list(event))
        self._users.update(preserved)
        if self._selected and self._selected[0] == "private":
            selected_id = self._selected[1]
            self._users.setdefault(selected_id, selected_id)
        self._refresh_people_list()
        self.connection_status_var.set(
            f"Personas publicadas por el servidor: {len(self._users)}"
        )

    def _handle_ack(self, event: JsonObject) -> None:
        request_id = event.get("requestId")
        pending = (
            self._pending_requests.pop(request_id, None)
            if isinstance(request_id, str)
            else None
        )
        operation = str(event.get("operation", "operación"))
        if not pending:
            self.connection_status_var.set(
                f"✓ ACK {operation} (requestId={request_id or '-'})"
            )
            return

        # El ACK de un envío solo confirma la recepción del servidor; anunciarlo
        # como burbuja llenaría el hilo de ruido. Solo las operaciones que
        # cambian la pertenencia al grupo se anotan en la conversación.
        if operation in {"GROUP_JOIN", "GROUP_LEAVE"}:
            if operation == "GROUP_JOIN":
                self._ensure_group(pending["id"], pending["id"])
            self._add_system_message(
                (pending["kind"], pending["id"]),
                f"✓ {operation} aceptado",
            )
        else:
            self.connection_status_var.set(f"✓ {operation} aceptado")

    def _handle_server_error(self, event: JsonObject) -> None:
        request_id = event.get("requestId")
        pending = (
            self._pending_requests.pop(request_id, None)
            if isinstance(request_id, str)
            else None
        )
        message = (
            f"Error {event.get('code', 'UNKNOWN')}: "
            f"{event.get('message', 'Error recibido')}"
        )
        if pending:
            # El mensaje privado se pintó de forma optimista al enviarlo; si el
            # servidor lo rechaza hay que desmentirlo en la propia burbuja.
            if pending["operation"] == "PRIVATE_SEND":
                self._mark_message_failed(
                    (pending["kind"], pending["id"]), request_id
                )
            self._add_system_message((pending["kind"], pending["id"]), message)
        else:
            self._add_system_to_current(message)
        self.connection_status_var.set(message)

    def _handle_private_message(self, event: JsonObject) -> None:
        sender = event.get("from")
        if not isinstance(sender, str) or not sender.strip():
            self.connection_status_var.set(
                "PRIVATE_MESSAGE recibido sin remitente"
            )
            return

        sender = sender.strip()
        self._ensure_user(sender, sender)
        self._add_message(
            ("private", sender),
            direction="incoming",
            sender=sender,
            text=str(event.get("text", "")),
        )

    def _handle_group_message(self, event: JsonObject) -> None:
        group_id = event.get("groupId")
        sender = event.get("from")
        if not isinstance(group_id, str) or not group_id.strip():
            self.connection_status_var.set(
                "GROUP_MESSAGE recibido sin groupId"
            )
            return

        group_id = group_id.strip()
        sender_text = str(sender) if isinstance(sender, str) else "?"
        self._ensure_group(group_id, group_id)
        if sender_text != "?" and sender_text != self.client.user_id:
            self._ensure_user(sender_text, sender_text)
        self._add_message(
            ("group", group_id),
            direction="outgoing" if sender_text == self.client.user_id else "incoming",
            sender="Tú" if sender_text == self.client.user_id else sender_text,
            text=str(event.get("text", "")),
        )

    def _ensure_group(self, group_id: str, label: str | None = None) -> None:
        group_id = group_id.strip()
        if not group_id:
            return
        if group_id not in self._groups:
            self._groups[group_id] = (label or group_id).strip()
            self._refresh_group_list()
        elif label and label.strip() and self._groups[group_id] == group_id:
            self._groups[group_id] = label.strip()
            self._refresh_group_list()

    def _ensure_user(self, user_id: str, label: str | None = None) -> None:
        user_id = user_id.strip()
        if not user_id or user_id == self.client.user_id:
            return
        self._users.setdefault(user_id, (label or user_id).strip())
        self._refresh_people_list()

    def _refresh_group_list(self) -> None:
        self._group_order = list(self._groups)
        self.group_listbox.delete(0, tk.END)
        if not self._group_order:
            self.group_listbox.insert(tk.END, "Esperando GROUP_LIST...")
            self.group_listbox.itemconfig(0, fg=self.COLORS["sidebar_muted"])
            return

        for group_id in self._group_order:
            label = self._groups[group_id]
            marker = "  •" if ("group", group_id) in self._unread else ""
            self.group_listbox.insert(tk.END, f"#  {label}{marker}")

        if self._selected and self._selected[0] == "group":
            self._select_listbox_item(
                self.group_listbox,
                self._group_order,
                self._selected[1],
            )

    def _refresh_people_list(self) -> None:
        self._people_order = [
            user_id for user_id in self._users if user_id != self.client.user_id
        ]
        self.people_listbox.delete(0, tk.END)
        if not self._people_order:
            self.people_listbox.insert(tk.END, "Esperando USERS_LIST...")
            self.people_listbox.itemconfig(0, fg=self.COLORS["sidebar_muted"])
            return

        for user_id in self._people_order:
            label = self._users[user_id]
            marker = "  •" if ("private", user_id) in self._unread else ""
            self.people_listbox.insert(tk.END, f"●  {label}{marker}")

        if self._selected and self._selected[0] == "private":
            self._select_listbox_item(
                self.people_listbox,
                self._people_order,
                self._selected[1],
            )

    @staticmethod
    def _select_listbox_item(
        listbox: tk.Listbox,
        ordered_ids: list[str],
        selected_id: str,
    ) -> None:
        if selected_id in ordered_ids:
            index = ordered_ids.index(selected_id)
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(index)
            listbox.see(index)

    def _refresh_members(self) -> None:
        self.member_listbox.delete(0, tk.END)
        self._member_order = []

        if self._selected and self._selected[0] == "group":
            group_id = self._selected[1]
            self.participant_title_var.set("MIEMBROS")
            self.participant_hint_var.set(
                f"Integrantes de {self._groups.get(group_id, group_id)}"
            )
            members = self._members_by_group.get(group_id)
            if members is None:
                self.member_listbox.insert(
                    tk.END, "Esperando GROUP_MEMBERS..."
                )
                self.member_listbox.itemconfig(
                    0, fg=self.COLORS["muted"]
                )
                return
            if not members:
                self.member_listbox.insert(tk.END, "Sin miembros publicados")
                self.member_listbox.itemconfig(0, fg=self.COLORS["muted"])
                return
            for user_id, label in members:
                self._member_order.append(user_id)
                self.member_listbox.insert(tk.END, f"●  {label}")
            return

        if self._selected and self._selected[0] == "private":
            user_id = self._selected[1]
            self.participant_title_var.set("PERSONA")
            self.participant_hint_var.set(
                "Haz clic en personas de la barra lateral para cambiar de conversación."
            )
            self._member_order = [user_id]
            self.member_listbox.insert(
                tk.END, f"●  {self._users.get(user_id, user_id)}"
            )
            return

        self.participant_title_var.set("MIEMBROS")
        self.participant_hint_var.set(
            "Selecciona un grupo para ver sus miembros."
        )
        self.member_listbox.insert(tk.END, "Aún no hay una conversación")
        self.member_listbox.itemconfig(0, fg=self.COLORS["muted"])

    def _on_group_selected(self, _event: tk.Event) -> None:
        selection = self.group_listbox.curselection()
        if not selection or selection[0] >= len(self._group_order):
            return
        self._select_group(self._group_order[selection[0]])

    def _on_person_selected(self, _event: tk.Event) -> None:
        selection = self.people_listbox.curselection()
        if not selection or selection[0] >= len(self._people_order):
            return
        self._select_private(self._people_order[selection[0]])

    def _on_member_selected(self, _event: tk.Event) -> None:
        if not self._selected or self._selected[0] != "group":
            return
        selection = self.member_listbox.curselection()
        if not selection or selection[0] >= len(self._member_order):
            return
        self._select_private(self._member_order[selection[0]])

    def _select_group(self, group_id: str) -> None:
        if group_id not in self._groups:
            return
        self._selected = ("group", group_id)
        self._unread.discard(self._selected)
        self._refresh_group_list()
        self._render_selected_conversation()

    def _select_private(self, user_id: str) -> None:
        if not user_id or user_id == self.client.user_id:
            return
        self._ensure_user(user_id, user_id)
        self._selected = ("private", user_id)
        self._unread.discard(self._selected)
        self._refresh_people_list()
        self._render_selected_conversation()

    def _render_selected_conversation(self) -> None:
        self._update_header()
        self._refresh_members()
        self._render_messages()
        self._update_controls()

    def _update_header(self) -> None:
        if self._selected is None:
            self.header_avatar.configure(text="·", bg="#e7edf6")
            self.header_title_var.set("Selecciona un grupo o persona")
            self.header_subtitle_var.set(
                "La conversación aparecerá en este espacio"
            )
            self.composer_target_var.set("Selecciona una conversación")
            return

        kind, identifier = self._selected
        if kind == "group":
            self.header_avatar.configure(text="#", bg="#e6f0ff")
            self.header_title_var.set(self._groups.get(identifier, identifier))
            self.header_subtitle_var.set(
                f"Grupo · {identifier} · elige un miembro para escribirle en privado"
            )
            self.composer_target_var.set(
                f"Mensaje para #{self._groups.get(identifier, identifier)}"
            )
        else:
            label = self._users.get(identifier, identifier)
            self.header_avatar.configure(
                text=label[:1].upper(),
                bg="#e8f7ef",
            )
            self.header_title_var.set(label)
            self.header_subtitle_var.set(
                f"Conversación privada · {identifier}"
            )
            self.composer_target_var.set(f"Mensaje privado para {label}")

    def _render_messages(self) -> None:
        for child in self.message_inner.winfo_children():
            child.destroy()

        if self._selected is None:
            self._add_empty_conversation(
                "Tu chat está listo",
                "Selecciona una persona o un grupo desde las barras laterales.",
            )
            return

        messages = self._messages.get(self._selected, [])
        if not messages:
            title = (
                "Aún no hay mensajes en este grupo"
                if self._selected[0] == "group"
                else "Aún no hay mensajes con esta persona"
            )
            self._add_empty_conversation(
                title,
                "Los mensajes recibidos y enviados aparecerán aquí.",
            )
            return

        for message in messages:
            self._add_message_bubble(message)

        self.message_inner.update_idletasks()
        self.message_canvas.configure(
            scrollregion=self.message_canvas.bbox("all")
        )
        self.message_canvas.yview_moveto(1.0)

    def _add_empty_conversation(self, title: str, detail: str) -> None:
        empty = tk.Frame(self.message_inner, bg=self.COLORS["background"])
        empty.pack(expand=True, fill="both", pady=120)
        tk.Label(
            empty,
            text=title,
            bg=self.COLORS["background"],
            fg=self.COLORS["text"],
            font=("TkDefaultFont", 12, "bold"),
        ).pack()
        tk.Label(
            empty,
            text=detail,
            bg=self.COLORS["background"],
            fg=self.COLORS["muted"],
            font=("TkDefaultFont", 9),
        ).pack(pady=(7, 0))

    def _add_message_bubble(self, message: JsonObject) -> None:
        direction = message["direction"]
        row = tk.Frame(self.message_inner, bg=self.COLORS["background"])
        row.pack(fill="x", pady=(0, 10))

        if direction == "system":
            label = tk.Label(
                row,
                text=message["text"],
                bg=self.COLORS["system"],
                fg=self.COLORS["muted"],
                font=("TkDefaultFont", 8),
                padx=12,
                pady=6,
                wraplength=430,
            )
            label.pack(anchor="center")
            return

        outgoing = direction == "outgoing"
        failed = bool(message.get("failed"))
        bubble = tk.Frame(
            row,
            bg=self.COLORS["bubble_out"] if outgoing else self.COLORS["bubble_in"],
            padx=13,
            pady=8,
            highlightbackground=(
                self.COLORS["danger"]
                if failed
                else ("#c8e1fb" if outgoing else self.COLORS["border"])
            ),
            highlightthickness=1,
        )
        bubble.pack(
            anchor="e" if outgoing else "w",
            padx=(90 if outgoing else 0, 0 if outgoing else 90),
        )
        tk.Label(
            bubble,
            text=message.get("sender", "Tú" if outgoing else ""),
            bg=bubble["bg"],
            fg=self.COLORS["accent_dark"] if outgoing else self.COLORS["muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            bubble,
            text=message["text"],
            bg=bubble["bg"],
            fg=self.COLORS["text"],
            font=("TkDefaultFont", 10),
            justify="left",
            anchor="w",
            wraplength=430,
        ).pack(anchor="w", pady=(3, 0))

        if failed:
            tk.Label(
                bubble,
                text="⚠ El servidor rechazó este mensaje",
                bg=bubble["bg"],
                fg=self.COLORS["danger"],
                font=("TkDefaultFont", 8, "bold"),
                anchor="w",
            ).pack(anchor="w", pady=(4, 0))

    def _add_message(
        self,
        conversation: tuple[str, str],
        *,
        direction: str,
        sender: str,
        text: str,
        request_id: str | None = None,
    ) -> None:
        self._messages.setdefault(conversation, []).append(
            {
                "direction": direction,
                "sender": sender,
                "text": text,
                "requestId": request_id,
                "failed": False,
            }
        )
        if direction == "incoming" and conversation != self._selected:
            self._unread.add(conversation)
        if conversation == self._selected:
            self._render_messages()
        self._refresh_group_list()
        self._refresh_people_list()

    def _mark_message_failed(
        self,
        conversation: tuple[str, str],
        request_id: Any,
    ) -> None:
        if not isinstance(request_id, str):
            return
        for message in self._messages.get(conversation, []):
            if message.get("requestId") == request_id:
                message["failed"] = True
                break
        if conversation == self._selected:
            self._render_messages()

    def _add_system_message(
        self,
        conversation: tuple[str, str],
        text: str,
    ) -> None:
        self._messages.setdefault(conversation, []).append(
            {
                "direction": "system",
                "sender": "Sistema",
                "text": text,
            }
        )
        if conversation == self._selected:
            self._render_messages()

    def _add_system_to_current(self, text: str) -> None:
        if self._selected is not None:
            self._add_system_message(self._selected, str(text))
        else:
            self.connection_status_var.set(str(text))

    def _add_group_from_dialog(self) -> None:
        if not self._connected:
            self.connection_status_var.set(
                "Conecta primero para unirte a un grupo"
            )
            return
        group_id = simpledialog.askstring(
            "Agregar grupo",
            "Escribe el groupId que debe usar el servidor:",
            parent=self.root,
        )
        if not group_id or not group_id.strip():
            return
        group_id = group_id.strip()
        self._manual_groups.add(group_id)
        self._ensure_group(group_id, group_id)
        self._select_group(group_id)
        self._request_group_action("GROUP_JOIN", group_id)

    def _join_selected_group(self) -> None:
        if self._selected and self._selected[0] == "group":
            self._request_group_action("GROUP_JOIN", self._selected[1])

    def _leave_selected_group(self) -> None:
        if self._selected and self._selected[0] == "group":
            self._request_group_action("GROUP_LEAVE", self._selected[1])

    def _request_group_action(self, operation: str, group_id: str) -> None:
        try:
            if operation == "GROUP_JOIN":
                request_id = self.client.join_group(group_id)
            else:
                request_id = self.client.leave_group(group_id)
        except (ConnectionError, OSError) as exc:
            self._add_system_to_current(f"No se pudo enviar {operation}: {exc}")
            return

        self._pending_requests[request_id] = {
            "kind": "group",
            "id": group_id,
            "operation": operation,
        }
        self.connection_status_var.set(
            f"{operation} enviado para {group_id}"
        )

    def _send_current_message(self, _event: tk.Event | None = None) -> str:
        if not self._connected or self._selected is None:
            self.connection_status_var.set(
                "Selecciona una conversación después de conectar"
            )
            return "break"

        text = self.composer_entry.get()
        if not text.strip():
            return "break"

        kind, identifier = self._selected
        try:
            if kind == "private":
                request_id = self.client.send_private(identifier, text)
                self._add_message(
                    self._selected,
                    direction="outgoing",
                    sender="Tú",
                    text=text,
                    request_id=request_id,
                )
            else:
                request_id = self.client.send_group(identifier, text)
                # GROUP_MESSAGE incluye al remitente según el contrato actual;
                # se espera ese evento para no duplicar el mensaje localmente.
        except (ConnectionError, OSError) as exc:
            self._add_system_to_current(f"No se pudo enviar el mensaje: {exc}")
            return "break"

        self._pending_requests[request_id] = {
            "kind": kind,
            "id": identifier,
            "operation": "PRIVATE_SEND" if kind == "private" else "GROUP_SEND",
        }
        self.composer_entry.delete(0, tk.END)
        return "break"

    def _update_controls(self) -> None:
        connected = self._connected
        selected_group = self._selected is not None and self._selected[0] == "group"
        can_send = connected and self._selected is not None
        self.add_group_button.configure(
            state=tk.NORMAL if connected else tk.DISABLED
        )
        self.join_button.configure(
            state=tk.NORMAL if connected and selected_group else tk.DISABLED
        )
        self.leave_button.configure(
            state=tk.NORMAL if connected and selected_group else tk.DISABLED
        )
        self.send_button.configure(
            state=tk.NORMAL if can_send else tk.DISABLED
        )
        self.composer_entry.configure(
            state=tk.NORMAL if can_send else tk.DISABLED
        )

    def _on_close(self) -> None:
        self._closing = True
        self.client.close()
        self.root.destroy()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cliente Fedora de chat TCP con JSON UTF-8 delimitado por newline."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host del servidor TCP (por defecto: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Puerto del servidor TCP (por defecto: 5000)",
    )
    parser.add_argument(
        "--user-id",
        "--user",
        dest="user_id",
        required=True,
        help="Identificador del usuario que se enviará en CONNECT",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        client = ChatClient(args.host, args.port, args.user_id)
    except ValueError as exc:
        parser.error(str(exc))

    root = tk.Tk()
    ChatApp(root, client).start_connection()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
