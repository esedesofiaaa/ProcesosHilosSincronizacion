# Cliente Fedora — Python

Cliente de escritorio con Tkinter. Incluye navegación por grupos, miembros del grupo, conversaciones privadas y chat grupal.

## Ejecutar

```bash
python3 cliente_chat.py --host 127.0.0.1 --port 1803 --user userx
```

## Estructura

- `chat_protocol.py`: framing TCP y utilidades JSON.
- `chat_client.py`: transporte TCP y lector de eventos.
- `chat_session.py`: estado de conversaciones y reglas de negocio.
- `chat_app.py`: interfaz Tkinter.
- `cliente_chat.py`: punto de entrada.

## Probar

```bash
python3 -m unittest -v test_cliente_chat.py
```
