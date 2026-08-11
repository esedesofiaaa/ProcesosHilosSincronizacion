#!/usr/bin/env python3
"""Punto de entrada del cliente Fedora de chat.

La implementación está separada por responsabilidad:

* ``chat_protocol.py``: framing TCP y JSON.
* ``chat_client.py``: transporte TCP.
* ``chat_session.py``: estado y reglas de negocio.
* ``chat_app.py``: interfaz Tkinter.
"""

from __future__ import annotations

import argparse
import tkinter as tk
from typing import Iterable

from chat_app import ChatApp
from chat_client import ChatClient


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
        default=1803,
        help="Puerto del servidor TCP (por defecto: 1803)",
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
