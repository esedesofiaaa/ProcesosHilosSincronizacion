# Cliente macOS de chat (SwiftUI)

Cliente de escritorio macOS para el contrato TCP del chat. Usa `Network.framework` (`NWConnection`), JSON UTF-8 y un salto de línea como delimitador. No usa WebSocket ni incluye servidor.

## Compilación en macOS

Requiere macOS 13 o posterior y Xcode Command Line Tools con Swift 5.9 o posterior.

```bash
cd Cliente-macOS-Swift
swift run
```

En la interfaz, indica el host, el puerto real del servidor y un usuario. El cliente abre TCP y envía `CONNECT` automáticamente; las acciones privadas y grupales se habilitan después de recibir `CONNECTED`.

El lector de `NWConnection` acumula bytes y separa por `\n`, por lo que maneja tanto JSON fragmentado entre lecturas como varias líneas recibidas juntas.
