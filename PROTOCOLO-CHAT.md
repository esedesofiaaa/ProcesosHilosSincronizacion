# Acuerdo de protocolo del chat

Este documento es el contrato entre el servidor y los clientes.

Todos los clientes deben respetarlo sin importar el lenguaje utilizado.

## 1. Transporte

- Transporte: TCP.
- Codificación: UTF-8.
- Formato: un objeto JSON por mensaje.
- Delimitador: salto de línea `\\n`.
- Cada mensaje debe enviarse en una sola línea lógica.
- Los saltos de línea escritos dentro del texto deben viajar escapados como `\\n` dentro del JSON.
- El servidor debe soportar mensajes divididos en varias lecturas TCP.
- El servidor también debe soportar varios mensajes recibidos en una sola lectura TCP.

Ejemplo de bytes conceptuales:

```text
{"type":"CONNECT","requestId":"r1","userId":"ana"}\n
```

## 2. Estructura común

Todos los mensajes del cliente incluyen:

```json
{
  "type": "TIPO_DE_OPERACION",
  "requestId": "id-generado-por-el-cliente"
}
```

`requestId` permite relacionar una respuesta con la solicitud original.

## 3. Conexión del cliente

El primer mensaje debe ser `CONNECT`.

```json
{
  "type": "CONNECT",
  "requestId": "r1",
  "userId": "ana"
}
```

Respuesta correcta:

```json
{
  "type": "CONNECTED",
  "requestId": "r1",
  "userId": "ana"
}
```

El servidor rechaza el usuario si ya está conectado.

### Sesión única

Solo se admite una sesión activa por usuario. Si llega un `CONNECT` con un `userId` que ya tiene sesión, **la conexión rechazada es la nueva**: la sesión que ya estaba activa se conserva intacta y no se entera del intento.

```json
{
  "type": "ERROR",
  "requestId": "r1",
  "code": "USER_ALREADY_CONNECTED",
  "message": "El usuario ya tiene una sesion activa"
}
```

Después de responder, el servidor cierra la conexión nueva.

Los `requestId` registrados pertenecen a la sesión: al cerrarse una conexión se descartan, y una sesión posterior del mismo usuario puede reutilizarlos.

## 4. Mensaje privado

Solicitud:

```json
{
  "type": "PRIVATE_SEND",
  "requestId": "r2",
  "to": "bob",
  "text": "Hola Bob"
}
```

Confirmación para el remitente:

```json
{
  "type": "ACK",
  "requestId": "r2",
  "operation": "PRIVATE_SEND"
}
```

Evento enviado al destinatario:

```json
{
  "type": "PRIVATE_MESSAGE",
  "messageId": "m1",
  "from": "ana",
  "text": "Hola Bob"
}
```

## 5. Grupos

### Unirse a un grupo

```json
{
  "type": "GROUP_JOIN",
  "requestId": "r3",
  "groupId": "distribuidos"
}
```

### Salir de un grupo

```json
{
  "type": "GROUP_LEAVE",
  "requestId": "r4",
  "groupId": "distribuidos"
}
```

### Enviar al grupo

```json
{
  "type": "GROUP_SEND",
  "requestId": "r5",
  "groupId": "distribuidos",
  "text": "Hola grupo"
}
```

El servidor envía el mensaje a todos los integrantes del grupo, incluido el remitente.

```json
{
  "type": "GROUP_MESSAGE",
  "messageId": "m2",
  "groupId": "distribuidos",
  "from": "ana",
  "text": "Hola grupo"
}
```

Para las operaciones `GROUP_JOIN`, `GROUP_LEAVE` y `GROUP_SEND`, el servidor responde con un `ACK` usando el mismo `requestId`.

## 6. Errores

Todos los errores tienen esta forma:

```json
{
  "type": "ERROR",
  "requestId": "r5",
  "code": "USER_NOT_FOUND",
  "message": "El usuario no existe"
}
```

Códigos iniciales:

- `INVALID_JSON`: JSON inválido.
- `INVALID_MESSAGE`: faltan campos o el tipo no es válido.
- `NOT_CONNECTED`: el cliente aún no se ha identificado.
- `USER_ALREADY_CONNECTED`: el usuario ya está conectado.
- `USER_NOT_FOUND`: el destinatario no existe.
- `USER_DISCONNECTED`: el destinatario existe pero no está conectado.
- `GROUP_NOT_FOUND`: el grupo no existe.
- `NOT_GROUP_MEMBER`: el usuario no pertenece al grupo.
- `DUPLICATE_REQUEST`: el `requestId` ya fue utilizado.

### Destinatario no disponible

El servidor no tiene registro permanente de usuarios: un usuario existe mientras tiene una sesión activa. Para poder distinguir los dos casos, el servidor conserva en memoria el conjunto de `userId` que se han conectado desde que arrancó.

- El nombre nunca se ha visto: `USER_NOT_FOUND`.
- El nombre se ha visto pero no tiene sesión activa: `USER_DISCONNECTED`.

Ese conjunto se pierde al reiniciar el servidor, igual que el resto del estado.

En un `PRIVATE_SEND` que no se puede entregar, el remitente recibe **un solo `ERROR`** y ningún `ACK`. El servidor recibió el mensaje, pero desde el punto de vista del remitente la operación falló, y enviar las dos respuestas obligaría a cada cliente a manejar un `ACK` que puede desmentirse después.

En un `GROUP_SEND` el comportamiento es distinto: **no se emite ningún error** por los integrantes que no estén conectados. El mensaje llega a los presentes y el remitente recibe su `ACK` normal.

## 7. Orden y entrega

- Los mensajes de una misma conexión se procesan y se entregan en el mismo orden en que fueron enviados. La garantía es de extremo a extremo: la sostienen TCP en el transporte y el servidor en el procesamiento.
- **No se define orden entre remitentes distintos.** Si dos clientes escriben al mismo tiempo, no hay un reloj común que permita decidir cuál fue primero, y el servidor no impone ninguno.
- El servidor asigna `messageId` a los mensajes aceptados.
- La entrega es en memoria y no persistente.
- Si un cliente se desconecta, sus mensajes pendientes se pierden en esta primera versión.
- No se garantiza entrega después de una desconexión.

## 8. Responsabilidades del servidor

El servidor debe:

1. Leer el flujo TCP sin bloquear el Selector.
2. Reconstruir mensajes separados por `\\n`.
3. Validar el JSON.
4. Validar los campos obligatorios.
5. Procesar la operación.
6. Enviar `ACK`, eventos o `ERROR`.
7. Mantener usuarios, conexiones y grupos en memoria.

## 9. Responsabilidades de cada cliente

Cada cliente debe:

- Conectarse al host y puerto configurados.
- Enviar `CONNECT` primero.
- Generar un `requestId` único por solicitud.
- Enviar JSON UTF-8 terminado en `\\n`.
- Mantener un lector de respuestas independiente de la interfaz.
- Mostrar mensajes privados y grupales.
- Mostrar los errores recibidos.
- No asumir que un `read` TCP contiene exactamente un mensaje.

## 10. Prueba independiente

Antes de conectar al servidor real, cada cliente puede probarse contra un mock server que:

- Acepte una conexión.
- Lea mensajes JSON terminados en `\\n`.
- Responda `CONNECTED` después de `CONNECT`.
- Responda `ACK` a las operaciones válidas.
- Envíe mensajes privados y grupales simulados.
- Envíe errores de prueba.

## 11. Decisiones fuera del alcance

- No se usará navegador ni WebSocket en esta primera versión.
- No se almacenará historial en una base de datos.
- No se implementará autenticación avanzada.
- No se implementará recuperación de mensajes después de una desconexión.
- No se implementará detección de caída sucia. Si un cliente pierde la conexión sin cerrarla, TCP no avisa y el servidor lo seguirá considerando conectado durante varios minutos. En ese lapso el usuario no puede volver a entrar, porque su nueva conexión recibirá `USER_ALREADY_CONNECTED`. Un latido periódico o un tiempo de inactividad lo resolverían.
- No se implementará límite de tasa por cliente.
