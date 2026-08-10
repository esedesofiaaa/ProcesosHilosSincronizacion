"""Estado de sesión y reglas de negocio del cliente de chat."""

from __future__ import annotations

from typing import Any

from chat_client import ChatClient
from chat_protocol import (
    JsonObject,
    parse_group_list,
    parse_group_members,
    parse_users_list,
)


Conversation = tuple[str, str]


class ChatSession:
    """Modelo de sesión y coordinador de operaciones del chat.

    Esta clase no conoce Tkinter. Mantiene directorios, conversaciones,
    solicitudes pendientes y transforma eventos del servidor en cambios de
    estado. La vista solo tiene que renderizar el estado resultante.
    """

    def __init__(self, client: ChatClient) -> None:
        self.client = client
        self.is_connected = False

        self.groups: dict[str, str] = {}
        self.manual_groups: set[str] = set()
        self.users: dict[str, str] = {}
        self.members_by_group: dict[str, list[tuple[str, str]]] = {}
        self.messages: dict[Conversation, list[JsonObject]] = {}
        self.selected: Conversation | None = None
        self.pending_requests: dict[str, JsonObject] = {}
        self.unread: set[Conversation] = set()

    def handle_event(self, event: JsonObject) -> str | None:
        """Procesa un evento del servidor y devuelve un estado para la vista."""

        event_type = event.get("type")

        if event_type == "GROUP_LIST":
            return self._handle_group_list(event)
        if event_type == "GROUP_MEMBERS":
            return self._handle_group_members(event)
        if event_type == "USERS_LIST":
            return self._handle_users_list(event)
        if event_type == "CONNECTED":
            self.is_connected = True
            return "Conectado · esperando listados opcionales"
        if event_type == "ACK":
            return self._handle_ack(event)
        if event_type == "PRIVATE_MESSAGE":
            self._handle_private_message(event)
            return None
        if event_type == "GROUP_MESSAGE":
            self._handle_group_message(event)
            return None
        if event_type == "ERROR":
            return self._handle_server_error(event)
        if event_type == "CLIENT_ERROR":
            self.add_system_to_current(
                f"Error local {event.get('code', 'UNKNOWN')}: "
                f"{event.get('message', 'Error de cliente')}"
            )
            return "Error de conexión"
        if event_type == "DISCONNECTED":
            self.is_connected = False
            self.add_system_to_current(
                event.get("message", "El servidor cerró la conexión")
            )
            return "Desconectado"

        return f"Evento no visualizado: {event_type or 'sin tipo'}"

    def select_group(self, group_id: str) -> bool:
        if group_id not in self.groups:
            return False
        self.selected = ("group", group_id)
        self.unread.discard(self.selected)
        return True

    def select_private(self, user_id: str) -> bool:
        if not user_id or user_id == self.client.user_id:
            return False
        self._ensure_user(user_id, user_id)
        self.selected = ("private", user_id)
        self.unread.discard(self.selected)
        return True

    def add_manual_group(self, group_id: str) -> None:
        self.manual_groups.add(group_id)
        self._ensure_group(group_id, group_id)
        self.select_group(group_id)

    def request_group_action(self, operation: str, group_id: str) -> str:
        if operation == "GROUP_JOIN":
            request_id = self.client.join_group(group_id)
        else:
            request_id = self.client.leave_group(group_id)

        self.pending_requests[request_id] = {
            "kind": "group",
            "id": group_id,
            "operation": operation,
        }
        return request_id

    def send_message(self, kind: str, identifier: str, text: str) -> str:
        """Envía un mensaje y registra el estado local correspondiente."""

        if kind == "private":
            request_id = self.client.send_private(identifier, text)
            self.record_message(
                (kind, identifier),
                direction="outgoing",
                sender="Tú",
                text=text,
                request_id=request_id,
            )
        else:
            request_id = self.client.send_group(identifier, text)
            # GROUP_MESSAGE incluye el eco del servidor; no se duplica aquí.

        self.pending_requests[request_id] = {
            "kind": kind,
            "id": identifier,
            "operation": "PRIVATE_SEND" if kind == "private" else "GROUP_SEND",
        }
        return request_id

    def messages_for(self, conversation: Conversation | None) -> list[JsonObject]:
        if conversation is None:
            return []
        return self.messages.get(conversation, [])

    def _handle_group_list(self, event: JsonObject) -> str:
        if "groups" not in event:
            return "GROUP_LIST recibido sin el campo groups"

        entries = parse_group_list(event)
        preserved = {
            group_id: label
            for group_id, label in self.groups.items()
            if group_id in self.manual_groups
            or ("group", group_id) in self.messages
        }
        self.groups = dict(entries)
        self.groups.update(preserved)
        return f"Grupos publicados por el servidor: {len(entries)}"

    def _handle_group_members(self, event: JsonObject) -> str:
        group_id = event.get("groupId")
        if not isinstance(group_id, str) or not group_id.strip():
            return "GROUP_MEMBERS recibido sin groupId"

        group_id = group_id.strip()
        self._ensure_group(group_id, group_id)
        self.members_by_group[group_id] = parse_group_members(event)
        return f"Miembros recibidos para {self.groups[group_id]}"

    def _handle_users_list(self, event: JsonObject) -> str:
        if "users" not in event:
            return "USERS_LIST recibido sin el campo users"

        preserved = {
            user_id: label
            for user_id, label in self.users.items()
            if ("private", user_id) in self.messages
        }
        self.users = dict(parse_users_list(event))
        self.users.update(preserved)
        if self.selected and self.selected[0] == "private":
            selected_id = self.selected[1]
            self.users.setdefault(selected_id, selected_id)
        return f"Personas publicadas por el servidor: {len(self.users)}"

    def _handle_ack(self, event: JsonObject) -> str:
        request_id = event.get("requestId")
        pending = (
            self.pending_requests.pop(request_id, None)
            if isinstance(request_id, str)
            else None
        )
        operation = str(event.get("operation", "operación"))
        if not pending:
            return f"✓ ACK {operation} (requestId={request_id or '-'})"

        if operation in {"GROUP_JOIN", "GROUP_LEAVE"}:
            if operation == "GROUP_JOIN":
                self._ensure_group(pending["id"], pending["id"])
            self.add_system_message(
                (pending["kind"], pending["id"]),
                f"✓ {operation} aceptado",
            )
        return f"✓ {operation} aceptado"

    def _handle_server_error(self, event: JsonObject) -> str:
        request_id = event.get("requestId")
        pending = (
            self.pending_requests.pop(request_id, None)
            if isinstance(request_id, str)
            else None
        )
        message = (
            f"Error {event.get('code', 'UNKNOWN')}: "
            f"{event.get('message', 'Error recibido')}"
        )
        if pending:
            if pending["operation"] == "PRIVATE_SEND":
                self.mark_message_failed(
                    (pending["kind"], pending["id"]), request_id
                )
            self.add_system_message((pending["kind"], pending["id"]), message)
        else:
            self.add_system_to_current(message)
        return message

    def _handle_private_message(self, event: JsonObject) -> None:
        sender = event.get("from")
        if not isinstance(sender, str) or not sender.strip():
            return

        sender = sender.strip()
        self._ensure_user(sender, sender)
        self.record_message(
            ("private", sender),
            direction="incoming",
            sender=sender,
            text=str(event.get("text", "")),
        )

    def _handle_group_message(self, event: JsonObject) -> None:
        group_id = event.get("groupId")
        sender = event.get("from")
        if not isinstance(group_id, str) or not group_id.strip():
            return

        group_id = group_id.strip()
        sender_text = str(sender) if isinstance(sender, str) else "?"
        self._ensure_group(group_id, group_id)
        if sender_text != "?" and sender_text != self.client.user_id:
            self._ensure_user(sender_text, sender_text)
        self.record_message(
            ("group", group_id),
            direction="outgoing" if sender_text == self.client.user_id else "incoming",
            sender="Tú" if sender_text == self.client.user_id else sender_text,
            text=str(event.get("text", "")),
        )

    def _ensure_group(self, group_id: str, label: str | None = None) -> None:
        group_id = group_id.strip()
        if not group_id:
            return
        if group_id not in self.groups:
            self.groups[group_id] = (label or group_id).strip()
        elif label and label.strip() and self.groups[group_id] == group_id:
            self.groups[group_id] = label.strip()

    def _ensure_user(self, user_id: str, label: str | None = None) -> None:
        user_id = user_id.strip()
        if not user_id or user_id == self.client.user_id:
            return
        self.users.setdefault(user_id, (label or user_id).strip())

    def record_message(
        self,
        conversation: Conversation,
        *,
        direction: str,
        sender: str,
        text: str,
        request_id: str | None = None,
    ) -> None:
        self.messages.setdefault(conversation, []).append(
            {
                "direction": direction,
                "sender": sender,
                "text": text,
                "requestId": request_id,
                "failed": False,
            }
        )
        if direction == "incoming" and conversation != self.selected:
            self.unread.add(conversation)

    def mark_message_failed(
        self,
        conversation: Conversation,
        request_id: Any,
    ) -> None:
        if not isinstance(request_id, str):
            return
        for message in self.messages.get(conversation, []):
            if message.get("requestId") == request_id:
                message["failed"] = True
                break

    def add_system_message(self, conversation: Conversation, text: str) -> None:
        self.messages.setdefault(conversation, []).append(
            {
                "direction": "system",
                "sender": "Sistema",
                "text": text,
            }
        )

    def add_system_to_current(self, text: str) -> None:
        if self.selected is not None:
            self.add_system_message(self.selected, str(text))

