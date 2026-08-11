import json
import queue
import threading
import time
import unittest
from unittest import mock

from chat_client import ChatClient
from chat_protocol import (
    JsonLineDecoder,
    ProtocolDecodeError,
    encode_message,
    parse_group_list,
    parse_group_members,
    parse_users_list,
)
from chat_session import ChatSession


class FakeSocket:
    """Socket mínimo para probar el cliente sin levantar un servidor."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._incoming: queue.Queue[bytes] = queue.Queue()
        self._lock = threading.Lock()
        self.closed = False

    def settimeout(self, _timeout: float | None) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        with self._lock:
            if self.closed:
                raise OSError("socket cerrado")
            self.sent.append(data)

    def recv(self, _size: int) -> bytes:
        return self._incoming.get()

    def feed(self, data: bytes) -> None:
        self._incoming.put(data)

    def shutdown(self, _how: int) -> None:
        self._incoming.put(b"")

    def close(self) -> None:
        with self._lock:
            self.closed = True


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class JsonProtocolTests(unittest.TestCase):
    def test_encode_message_escapes_inner_newline_and_preserves_utf8(self) -> None:
        payload = {
            "type": "PRIVATE_SEND",
            "requestId": "r1",
            "text": "línea 1\nlínea 2",
        }

        wire = encode_message(payload)

        self.assertEqual(wire.count(b"\n"), 1)
        self.assertIn(b"\\n", wire)
        self.assertIn("línea".encode("utf-8"), wire)
        self.assertEqual(json.loads(wire.decode("utf-8")), payload)

    def test_decoder_handles_fragmented_and_coalesced_messages(self) -> None:
        decoder = JsonLineDecoder()

        first = decoder.feed(b'{"type":"CONNECTED","requestId":"r1"')
        second = decoder.feed(
            b'}\n{"type":"ACK","requestId":"r2","operation":"GROUP_JOIN"}\n'
        )

        self.assertEqual(first, [])
        self.assertEqual(
            second,
            [
                {"type": "CONNECTED", "requestId": "r1"},
                {
                    "type": "ACK",
                    "requestId": "r2",
                    "operation": "GROUP_JOIN",
                },
            ],
        )
        self.assertFalse(decoder.has_pending_bytes)

    def test_decoder_reports_one_bad_line_and_continues(self) -> None:
        results = JsonLineDecoder().feed(
            b"not-json\n{\"type\":\"ACK\",\"requestId\":\"r2\"}\n"
        )

        self.assertIsInstance(results[0], ProtocolDecodeError)
        self.assertEqual(results[1], {"type": "ACK", "requestId": "r2"})


class OptionalDirectoryUiTests(unittest.TestCase):
    def test_optional_lists_use_only_server_payload(self) -> None:
        self.assertEqual(
            parse_group_list(
                {
                    "type": "GROUP_LIST",
                    "groups": [
                        {"groupId": "distribuidos", "name": "Distribuidos"},
                        "python",
                    ],
                }
            ),
            [("distribuidos", "Distribuidos"), ("python", "python")],
        )
        self.assertEqual(
            parse_users_list(
                {
                    "type": "USERS_LIST",
                    "users": [
                        {"userId": "bob", "displayName": "Bob"},
                        "carla",
                    ],
                }
            ),
            [("bob", "Bob"), ("carla", "carla")],
        )
        self.assertEqual(
            parse_group_members(
                {
                    "type": "GROUP_MEMBERS",
                    "groupId": "distribuidos",
                    "members": [{"userId": "bob", "displayName": "Bob"}],
                }
            ),
            [("bob", "Bob")],
        )

    def test_missing_or_empty_optional_lists_do_not_create_data(self) -> None:
        self.assertEqual(parse_group_list({"type": "GROUP_LIST"}), [])
        self.assertEqual(parse_users_list({"type": "USERS_LIST", "users": []}), [])
        self.assertEqual(
            parse_group_members(
                {"type": "GROUP_MEMBERS", "groupId": "g", "members": []}
            ),
            [],
        )


class ChatClientTests(unittest.TestCase):
    def test_connect_is_first_and_operations_use_unique_request_ids(self) -> None:
        fake_socket = FakeSocket()
        with mock.patch("chat_client.socket.create_connection", return_value=fake_socket):
            client = ChatClient("fedora-host", 7341, "ana")
            try:
                client.connect()
                self.assertTrue(wait_until(lambda: len(fake_socket.sent) >= 1))

                connect_payload = json.loads(fake_socket.sent[0].decode("utf-8"))
                self.assertEqual(connect_payload["type"], "CONNECT")
                self.assertEqual(connect_payload["userId"], "ana")
                self.assertIn("requestId", connect_payload)

                fake_socket.feed(encode_message({
                    "type": "CONNECTED",
                    "requestId": connect_payload["requestId"],
                    "userId": "ana",
                }))
                self.assertTrue(
                    wait_until(
                        lambda: not client.events.empty()
                    )
                )
                self.assertEqual(client.events.get_nowait()["type"], "CONNECTED")

                request_ids = {
                    client.send_private("bob", "Hola"),
                    client.join_group("distribuidos"),
                    client.leave_group("distribuidos"),
                    client.send_group("distribuidos", "Hola grupo"),
                }
                self.assertEqual(len(request_ids), 4)

                payloads = [
                    json.loads(wire.decode("utf-8")) for wire in fake_socket.sent
                ]
                self.assertEqual(
                    [payload["type"] for payload in payloads],
                    ["CONNECT", "PRIVATE_SEND", "GROUP_JOIN", "GROUP_LEAVE", "GROUP_SEND"],
                )
                self.assertTrue(all("requestId" in payload for payload in payloads))
            finally:
                client.close()

    def test_private_send_rejects_the_current_user(self) -> None:
        client = ChatClient("127.0.0.1", 5000, "ana")

        with self.assertRaises(ValueError):
            client.send_private(" ana ", "Hola")

        self.assertFalse(client.is_open)


    def test_reader_publishes_ack_message_and_error_from_one_tcp_chunk(self) -> None:
        fake_socket = FakeSocket()
        with mock.patch("chat_client.socket.create_connection", return_value=fake_socket):
            client = ChatClient("127.0.0.1", 5000, "ana")
            try:
                client.connect()
                self.assertTrue(wait_until(lambda: len(fake_socket.sent) >= 1))
                fake_socket.feed(
                    encode_message({
                        "type": "ACK",
                        "requestId": "r2",
                        "operation": "PRIVATE_SEND",
                    })
                    + encode_message({
                        "type": "PRIVATE_MESSAGE",
                        "messageId": "m1",
                        "from": "bob",
                        "text": "Hola ana",
                    })
                    + encode_message({
                        "type": "ERROR",
                        "requestId": "r3",
                        "code": "GROUP_NOT_FOUND",
                        "message": "El grupo no existe",
                    })
                )

                received: list[dict] = []
                self.assertTrue(
                    wait_until(
                        lambda: self._collect_three(client, received), timeout=1.5
                    )
                )
                self.assertEqual(
                    [event["type"] for event in received],
                    ["ACK", "PRIVATE_MESSAGE", "ERROR"],
                )
                self.assertEqual(received[1]["text"], "Hola ana")
                self.assertEqual(received[2]["code"], "GROUP_NOT_FOUND")
            finally:
                client.close()

    @staticmethod
    def _collect_three(client: ChatClient, received: list[dict]) -> bool:
        while len(received) < 3:
            try:
                received.append(client.events.get_nowait())
            except queue.Empty:
                break
        return len(received) == 3


class ChatSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = ChatSession(ChatClient("127.0.0.1", 5000, "ana"))

    def test_unknown_private_sender_remains_navigable_after_users_list(self) -> None:
        self.session.handle_event(
            {
                "type": "PRIVATE_MESSAGE",
                "from": "carol",
                "text": "Hola ana",
            }
        )

        self.assertEqual(self.session.users["carol"], "carol")
        self.assertIn(("private", "carol"), self.session.messages)

        self.session.handle_event(
            {"type": "USERS_LIST", "users": [{"userId": "bob"}]}
        )
        self.assertIn("carol", self.session.users)

    def test_unknown_group_message_remains_navigable_after_group_list(self) -> None:
        self.session.handle_event(
            {
                "type": "GROUP_MESSAGE",
                "groupId": "distribuidos",
                "from": "bob",
                "text": "Hola grupo",
            }
        )

        self.assertIn("distribuidos", self.session.groups)
        self.assertIn(("group", "distribuidos"), self.session.messages)

        self.session.handle_event({"type": "GROUP_LIST", "groups": []})
        self.assertIn("distribuidos", self.session.groups)

    def test_session_does_not_allow_selecting_current_user(self) -> None:
        self.assertFalse(self.session.select_private("ana"))
        self.assertIsNone(self.session.selected)


if __name__ == "__main__":
    unittest.main()
