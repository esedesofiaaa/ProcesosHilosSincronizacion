# Arquitectura final del chat

## 1. Decisiones principales

La solución utilizará:

- Un servidor central cliente-servidor.
- Sockets TCP no bloqueantes.
- Un solo `Selector` para administrar `OP_ACCEPT`, `OP_READ` y `OP_WRITE`.
- Una cola de clientes listos basada en `BlockingQueue`.
- Una cola personal de mensajes pendientes por cliente.
- Un pool fijo de workers.
- Una cola de salida por cliente.
- Una cola de órdenes pendientes para el Selector.
- Mensajes JSON convertidos a bytes.
- Comunicación privada y grupal.
- Una sola sesión activa por usuario.
- Sin persistencia de chats.

No se utilizarán dos Selectors, una cola de comandos ni una base de datos en esta primera versión.

## 2. Arquitectura general

```text
                         ENTRADA

Clientes TCP
     |
     v
SocketChannels
     |
     v
Selector
 OP_ACCEPT / OP_READ / OP_WRITE
     |
     | OP_READ
     v
Cola personal de cada cliente
     |
     v
BlockingQueue<ClientState>
 (clientes con trabajo pendiente)
     |
     v
Pool de workers
     |
     v
Procesamiento del mensaje
     |
     | crea respuestas
     v
Colas de salida por cliente
     |
     | pendingCommands + selector.wakeup()
     v
Selector OP_WRITE
     |
     v
SocketChannel del destinatario
     |
     v
Clientes TCP
```

## 3. Responsabilidad del Selector

El `Selector` es el único hilo que administra los sockets y las claves de selección.

Sus responsabilidades son:

- Aceptar nuevas conexiones con `OP_ACCEPT`.
- Asignar un `connectionId` a cada conexión aceptada.
- Leer bytes con `OP_READ`.
- Mantener el buffer de lectura de cada cliente.
- Reconstruir mensajes completos.
- Agregar cada mensaje a la cola personal del cliente.
- Publicar al cliente en la cola de clientes listos.
- Aplicar las órdenes pendientes que dejaron los workers.
- Activar `OP_WRITE` cuando un cliente tenga respuestas pendientes.
- Escribir respuestas en los sockets.
- Desactivar `OP_WRITE` cuando la cola de salida quede vacía.
- Cerrar conexiones.

Los workers no ejecutan directamente operaciones como `channel.write()`, `configureBlocking()`, `interestOps()` o `close()`.

Como el Selector es un solo hilo, todo el estado de sockets y claves está confinado a ese hilo y no necesita protección adicional.

## 4. Cola de clientes listos

La coordinación entre el `Selector` y el pool de workers no se hace con una cola de mensajes, sino con una cola de **clientes que tienen trabajo pendiente**.

```text
Selector          -> productor
BlockingQueue<ClientState> -> cola compartida
Workers           -> consumidores
```

Ejemplos de tareas:

- Procesar un mensaje privado.
- Procesar un mensaje grupal.
- Validar un mensaje.
- Registrar o retirar un usuario.
- Cerrar una sesión por una orden de aplicación.

Cada cliente tiene además su propia lista de mensajes pendientes, en orden de llegada.

```text
cola personal de Ana:    [m1] [m2] [m3]
cola personal de Bob:    [m1]
cola personal de Carlos: (vacía)

readyQueue: [ Ana ] [ Bob ]
```

### Estado de cada cliente

```java
class ClientState {
    final long connectionId;
    final Queue<Task> pending = new ConcurrentLinkedQueue<>();
    final AtomicBoolean scheduled = new AtomicBoolean(false);
    final Queue<ByteBuffer> outputQueue = new ConcurrentLinkedQueue<>();
    ByteBuffer currentWrite;      // respuesta parcial en curso
    ByteBuffer readBuffer;        // bytes de entrada incompletos
    String userId;                // null hasta el CONNECT
    Set<String> seenRequestIds;
}
```

### Cola compartida

```java
BlockingQueue<ClientState> readyQueue = new LinkedBlockingQueue<>();
```

La cola no tiene límite de capacidad. Esto es deliberado: si la cola fuera acotada, `put()` bloquearía al hilo del `Selector` cuando se llenara, y el servidor entero dejaría de aceptar, leer y escribir para todos los clientes. El Selector nunca debe bloquearse.

El costo de esta decisión es que la memoria crece si los workers no dan abasto. Queda declarado en la sección 13.

### El Selector produce

```java
state.pending.offer(task);
if (state.scheduled.compareAndSet(false, true)) {
    readyQueue.offer(state);
}
```

### Cada worker consume

```java
ClientState state = readyQueue.take();
```

`BlockingQueue` gestiona internamente la espera y la coordinación entre productores y consumidores. No se agregará un `ReentrantLock` externo para proteger la misma cola.

### El invariante

> Un cliente está en `readyQueue` como máximo una vez, y solo un worker puede tenerlo a la vez.

Mientras un worker atiende a Ana, Ana no está en la cola compartida, así que ningún otro worker puede tomarla. Es imposible que dos workers procesen mensajes de Ana al mismo tiempo, y por lo tanto sus mensajes se procesan en el orden en que llegaron.

El invariante lo sostiene `compareAndSet(false, true)`: si el valor es `false` lo cambia a `true` y devuelve `true`; si ya era `true` no toca nada y devuelve `false`. La operación es indivisible, de modo que si el Selector y un worker la intentan a la vez, exactamente uno gana. Eso evita que un cliente quede encolado dos veces.

## 5. Procesamiento del worker

Un worker realiza estas acciones:

1. Toma un cliente de la cola de clientes listos con `take()`.
2. Saca **un solo** mensaje de la cola personal de ese cliente.
3. Decodifica los bytes como texto.
4. Reconstruye el JSON.
5. Valida el tipo de mensaje.
6. Identifica al remitente.
7. Determina los destinatarios.
8. Crea una respuesta para cada destinatario.
9. Convierte cada respuesta a bytes.
10. Agrega cada respuesta a la cola de salida correspondiente.
11. Deja una orden en `pendingCommands` y ejecuta `selector.wakeup()`.
12. Libera al cliente y lo devuelve al final de la cola si le quedó trabajo.

```java
while (true) {
    ClientState state = readyQueue.take();

    Task task = state.pending.poll();
    if (task != null) {
        procesar(task);
    }

    state.scheduled.set(false);

    if (!state.pending.isEmpty()
            && state.scheduled.compareAndSet(false, true)) {
        readyQueue.offer(state);
    }
}
```

El worker procesa **un** mensaje por turno y no vacía toda la cola personal del cliente. Si lo hiciera, un cliente muy activo secuestraría a ese worker mientras los demás esperan. Al devolver al cliente al final de la cola, el reparto de trabajo queda equilibrado por turnos.

El worker procesa la lógica, pero no escribe directamente en los sockets.

## 6. Colas de salida

La cola de clientes listos y las colas de salida son diferentes.

```text
Selector -> BlockingQueue<ClientState> -> Workers
Workers  -> OutputQueue del cliente    -> Selector
```

Cada cliente tiene una cola de salida:

```text
Cliente A -> OutputQueue A
Cliente B -> OutputQueue B
Cliente C -> OutputQueue C
```

Las colas de salida son `ConcurrentLinkedQueue` y no `BlockingQueue`, porque nadie se bloquea sobre ellas: el worker agrega sin esperar y el Selector retira sin esperar.

### No hay un mapa de colas de salida

Una primera versión de este diseño tenía una estructura aparte para las colas:

```java
Map<ClientId, BlockingQueue<ByteBuffer>> outputQueues;   // descartado
```

Eso obliga a mantener dos mapas en paralelo, uno con los clientes y otro con sus colas, y a borrarlos siempre juntos. Olvidar uno de los dos deja una cola huérfana que nadie vaciará nunca.

En su lugar, la cola de salida es un campo del propio cliente, igual que su cola personal de entrada:

```java
class ClientState {
    Queue<Task> pending;              // entrada: mensajes por procesar
    Queue<ByteBuffer> outputQueue;    // salida: respuestas por enviar
    // ...
}
```

Así una sola búsqueda devuelve todo lo del cliente, y al cerrar la conexión basta con soltar el `ClientState`: se lleva sus dos colas con él.

El único registro que hace falta es el de sesiones activas, y es concurrente porque lo leen todos los workers:

```java
Map<String, ClientState> connectedUsers = new ConcurrentHashMap<>();
```

El worker agrega una respuesta:

```java
ClientState destino = connectedUsers.get(destinationId);
if (destino == null) {
    // Privado: se responde ERROR al remitente.
    // Grupal: no se entrega y no se emite error.
    return;
}
destino.outputQueue.offer(responseBuffer);
pendingCommands.offer(new EnableWrite(destino));
selector.wakeup();
```

La comprobación de `null` es necesaria: entre el momento en que el worker resuelve al destinatario y el momento en que encola la respuesta, ese cliente pudo desconectarse.

El destinatario ausente se trata distinto según el tipo de mensaje. En un mensaje privado el remitente recibe `USER_NOT_FOUND` o `USER_DISCONNECTED`, según el nombre se haya visto antes o no. En un mensaje grupal el integrante ausente simplemente no recibe nada y el remitente obtiene su `ACK` normal.

El Selector no utiliza `take()` para la salida, porque no debe bloquearse. Cuando recibe `OP_WRITE`, utiliza `poll()` y escribe los datos disponibles.

Cuando el Selector cierra una conexión, descarta la cola de salida de ese cliente. Los buffers que quedaran allí no se entregan.

## 6.1 Cola de órdenes pendientes

`selector.wakeup()` despierta al Selector pero no transporta información: es una señal sin contenido. El Selector despierta, consulta el conjunto de claves listas para E/S, y la clave del destinatario no está allí porque nadie registró todavía su interés en escritura.

Los workers tampoco pueden registrarlo por su cuenta: `interestOps()` es una operación sobre claves de selección, reservada al hilo del Selector, y en varias implementaciones se bloquea mientras el Selector está dentro de `select()`.

La solución es una cola de órdenes que los workers dejan para el Selector:

```java
Queue<Command> pendingCommands = new ConcurrentLinkedQueue<>();
```

El worker deja la orden y después despierta:

```java
pendingCommands.offer(new EnableWrite(destinationId));
selector.wakeup();
```

El Selector la drena **antes** de `select()`:

```java
Command cmd;
while ((cmd = pendingCommands.poll()) != null) {
    aplicar(cmd);
}
int n = selector.select();
```

El orden importa. Si el Selector drenara la cola después de `select()`, existiría una carrera: el `wakeup()` llegaría antes de que la orden se registre y el mensaje se quedaría dormido hasta que otro evento cualquiera activara esa clave.

Esta misma cola sirve para todas las operaciones sobre sockets que un worker necesita pero no puede ejecutar: activar `OP_WRITE`, cerrar una conexión por una orden de aplicación y, en versiones futuras, reactivar `OP_READ`.

Las órdenes duplicadas son inofensivas. Si tres respuestas van al mismo destinatario, la cola tendrá tres órdenes idénticas y activar `OP_WRITE` tres veces produce el mismo resultado que activarlo una.

## 7. Flujo de salida

```text
1. El worker termina de procesar el mensaje.
2. Identifica el destinatario.
3. Agrega la respuesta a la OutputQueue del destinatario.
4. Deja una orden EnableWrite en pendingCommands.
5. Ejecuta selector.wakeup().
6. El Selector despierta y drena pendingCommands.
7. El Selector activa OP_WRITE para ese cliente.
8. El socket está listo para escribir.
9. El Selector escribe todo lo que pueda.
10. Si el socket se llena, conserva OP_WRITE y el buffer parcial.
11. Si la cola queda vacía, desactiva OP_WRITE.
```

La escritura debe conservar el buffer parcial. `poll()` retira el `ByteBuffer` de la cola, así que si `write()` no lo vacía por completo, ese buffer ya no está en ninguna parte y hay que guardarlo en el estado del cliente:

```java
while (true) {
    if (state.currentWrite == null) {
        state.currentWrite = state.outputQueue.poll();
        if (state.currentWrite == null) {
            key.interestOps(key.interestOps() & ~SelectionKey.OP_WRITE);
            break;                              // no queda nada por enviar
        }
    }
    channel.write(state.currentWrite);
    if (state.currentWrite.hasRemaining()) {
        break;                                  // socket lleno: conserva OP_WRITE
    }
    state.currentWrite = null;                  // completo: sigue con el próximo
}
```

TCP permite leer y escribir por la misma conexión. Sin embargo, la respuesta no siempre vuelve al mismo cliente:

- Una confirmación vuelve al socket del remitente.
- Un mensaje privado sale por el socket del destinatario.
- Un mensaje grupal se agrega a la cola de salida de cada integrante.

## 8. Sincronización seleccionada

La técnica seleccionada para la coordinación principal es:

> Patrón productor-consumidor mediante `BlockingQueue`.

La cola protege el acceso concurrente y permite que los workers esperen cuando no hay clientes con trabajo pendiente. El `take()` de cada worker es el punto donde el hilo se bloquea y cede el procesador.

No se agregará un mutex manual alrededor de una `BlockingQueue`.

La solución combina cuatro mecanismos:

| Mecanismo | Dónde | Para qué |
|---|---|---|
| `BlockingQueue` | `readyQueue` | Productor-consumidor y espera bloqueante de los workers |
| Operación atómica (CAS) | `AtomicBoolean scheduled` | Garantizar que un cliente no se encole dos veces |
| Confinamiento a hilo | Selector | Sockets y claves sin protección, porque solo un hilo los toca |
| Exclusión mutua sin monitor | `scheduled` | Actúa como cerrojo lógico sobre el cliente, sin `synchronized` |

El `Selector` no necesita un mutex interno porque solo ese hilo administra los sockets y las claves de selección.

### Cadena de custodia del orden

El orden de los mensajes de un mismo cliente se conserva porque cada eslabón de la cadena lo conserva:

| Eslabón | Qué lo garantiza |
|---|---|
| Cliente → red | TCP entrega los bytes de una conexión en el orden en que se enviaron |
| Selector lee y encola | Hilo único; `offer()` agrega al final de la cola personal |
| Worker procesa | Un cliente lo atiende un solo worker a la vez; `poll()` saca del frente |
| Worker → cola de salida | Las respuestas se generan en orden y se encolan al final |
| Selector escribe | Hilo único; `poll()` desde el frente; `currentWrite` termina un buffer antes de sacar el siguiente |

Si cualquiera de los cinco se rompe, se pierde la garantía.

No se define orden entre clientes distintos. Si Ana y Bob escriben al mismo tiempo, `select()` devuelve las dos claves sin indicar cuál llegó antes. No existe un reloj común entre máquinas que permita decidirlo, y ningún usuario percibe ese orden.

## 9. Mensajes TCP

TCP es un flujo de bytes y no conserva los límites de los mensajes JSON.

La primera versión debe usar uno de estos mecanismos:

- JSON terminado por un delimitador, por ejemplo `\\n`.
- Prefijo con el tamaño del mensaje.

El buffer de lectura de cada cliente debe conservar bytes parciales hasta completar un mensaje.

Una sola lectura puede traer varios mensajes completos y un fragmento del siguiente. El bucle de extracción debe encolar los mensajes completos en el orden en que aparecen y guardar el fragmento para la próxima lectura.

## 10. Chat privado y grupal

### Mensaje privado

```text
Cliente A
   |
   v
Selector
   |
   v
Cola personal de A
   |
   v
Cola de clientes listos
   |
   v
Worker
   |
   v
OutputQueue B
   |
   v
Selector OP_WRITE
   |
   v
Cliente B
```

### Mensaje grupal

```text
Worker
  |
  +--> OutputQueue A
  +--> OutputQueue B
  +--> OutputQueue C
          |
          v
       Selector
          |
          v
    Socket de cada cliente
```

En un mensaje grupal, los integrantes que no estén conectados simplemente no reciben nada. No se genera un error por cada ausente.

## 11. Persistencia

No se guardará el historial de chats.

Los mensajes existirán temporalmente durante este flujo:

```text
Cliente -> Cola personal -> Cola de clientes listos -> Worker -> Cola de salida -> Cliente
```

Después de la entrega, el servidor no conservará el contenido. Si el servidor se reinicia, se perderán los mensajes pendientes.

## 12. Resumen

```text
Un Selector administra todos los sockets.
Cada cliente tiene una cola personal de mensajes pendientes.
La BlockingQueue de clientes listos coordina al Selector y los workers.
Un cliente lo atiende un solo worker a la vez, lo que preserva el orden.
Los workers procesan un mensaje por turno y devuelven el cliente a la cola.
Las OutputQueue almacenan respuestas por cliente.
La cola de órdenes le indica al Selector qué debe hacer al despertar.
El Selector envía las respuestas con OP_WRITE.
Los workers nunca escriben directamente en los sockets.
```

## 13. Limitaciones conocidas

Son deliberadas, para mantener esta primera versión enfocada.

- **Caída sucia de un cliente.** Solo se admite una sesión activa por usuario y la conexión nueva es la que se rechaza. Si un cliente se cae sin cerrar el socket (corte de red, cierre del equipo), TCP no avisa y el socket queda abierto del lado del servidor durante minutos. Durante ese tiempo el usuario aparece conectado y no puede volver a entrar. La solución sería un latido periódico o un tiempo de inactividad, y no se implementa aquí.
- **Memoria de la cola.** `readyQueue` no tiene límite. Si los workers no dan abasto, la cola crece hasta agotar la memoria disponible. Es el precio de que el Selector nunca se bloquee.
- **Registro de `requestId`.** El conjunto de identificadores vistos crece mientras la sesión esté activa. Se libera al desconectar.
- **Sin límite de tasa.** Un cliente muy activo alarga la cola. La rotación por turnos reparte el trabajo con justicia entre clientes, pero nada impide que uno solo genere la mayor parte de la carga. La solución sería desactivar `OP_READ` para ese cliente cuando su cola personal supere un umbral y reactivarlo cuando baje de otro más bajo, usando la cola de órdenes de la sección 6.1. No se implementa en esta versión.
