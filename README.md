# Clientes de chat distribuido

Implementaciones de cliente para el laboratorio de procesos, hilos y sincronización.

## Clientes

- `Cliente-Fedora-Python`: cliente de escritorio para Fedora/Linux con Tkinter.
- `Cliente-macOS-Swift`: cliente de escritorio para macOS con SwiftUI y `NWConnection`.

Ambos clientes usan TCP y mensajes JSON delimitados por salto de línea. El contrato común está en [`PROTOCOLO-CHAT.md`](PROTOCOLO-CHAT.md).
