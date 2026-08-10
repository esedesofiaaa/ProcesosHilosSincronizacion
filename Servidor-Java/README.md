# Servidor de chat — Java NIO

Servidor central del chat: un `Selector` no bloqueante, un pool fijo de workers y una
`BlockingQueue` que los coordina. Implementa [`PROTOCOLO-CHAT.md`](../PROTOCOLO-CHAT.md)
según el diseño de
[`arquitectura-final-chat-selector-blockingqueue.md`](../arquitectura-final-chat-selector-blockingqueue.md).

**Sin dependencias externas.** El JSON se lee y se escribe en `chat.json.Json`, así que
el proyecto compila con `javac` a secas.

## Ejecutar

```bash
cd Servidor-Java && javac -d target/classes $(find src/main/java -name "*.java") && java -cp target/classes chat.Main
```

Escucha en el puerto **1803** con **4 workers**. Ambos se pueden cambiar:

```bash
java -cp target/classes chat.Main 9000 8
```

También hay `pom.xml`, para quien prefiera `mvn compile`.

## Estructura

| Archivo | Responsabilidad |
|---|---|
| `chat/Main.java` | Arranque: levanta los workers y ejecuta el Selector |
| `chat/server/SelectorServer.java` | El único hilo que toca sockets y claves |
| `chat/server/Worker.java` | Consumidor de la cola de listos |
| `chat/server/ServerContext.java` | Estado compartido y las dos colas que cruzan hilos |
| `chat/server/ClientState.java` | Todo lo que se sabe de una conexión |
| `chat/server/Task.java` · `Command.java` | Mensaje por procesar · orden para el Selector |
| `chat/app/ChatLogic.java` | Lógica del chat: sesiones, privados, grupos, errores |
| `chat/json/Json.java` | Lector y escritor de JSON mínimo |

## Cómo se coordinan los hilos

```
Selector (1 hilo)                 Workers (4 hilos)
     │                                  │
     │ lee bytes, arma mensajes         │
     ├──> cola personal del cliente     │
     ├──> BlockingQueue<ClientState> ───┤ take()  ← aquí esperan
     │                                  │
     │                                  │ procesa UN mensaje
     │      cola de salida del destino <┤
     │      pendingCommands + wakeup() <┤
     │                                  │
     └──> OP_WRITE ──> socket           │
```

Cuatro mecanismos, cada uno con su función:

| Mecanismo | Dónde | Para qué |
|---|---|---|
| `BlockingQueue` | `readyQueue` | Productor-consumidor; los workers esperan sin consumir CPU |
| CAS (`AtomicBoolean`) | `ClientState.scheduled` | Que un cliente no se encole dos veces |
| Confinamiento a hilo | Selector | Sockets y claves sin locks, porque solo un hilo los toca |
| Exclusión mutua sin monitor | `scheduled` | Cerrojo lógico sobre el cliente, sin `synchronized` |

### El invariante que preserva el orden

> Un cliente está en `readyQueue` como máximo una vez, y solo un worker lo atiende a la vez.

Mientras un worker atiende a Ana, Ana no está en la cola, así que ningún otro worker
puede tomarla. Sus mensajes se procesan en orden **por construcción**: el desorden no se
corrige, se vuelve imposible.

El worker procesa **un solo** mensaje y devuelve al cliente al final de la cola. Vaciar
toda su cola personal de golpe dejaría ese worker secuestrado por quien más escribe.

## Pruebas

Con el servidor corriendo en el puerto 1803, en otra terminal:

```bash
python3 pruebas/prueba_protocolo.py
```

Cubre las 24 comprobaciones del contrato: CONNECT y directorios, privados, grupos,
sesión única, los ocho códigos de error, texto con saltos de línea y comillas, y
desconexión.

```bash
python3 pruebas/prueba_orden.py
```

Seis remitentes en paralelo, 60 mensajes cada uno, arrancando a la vez con una barrera.
Verifica que los 360 lleguen completos y que la secuencia de cada remitente se conserve.

### Sobre la validez de esa prueba

La primera versión pasaba incluso con el invariante deshabilitado: el procesamiento es
tan rápido que dos workers casi nunca se solapan. Para comprobar que la prueba realmente
discrimina se compararon dos variantes con 0,3 ms de latencia añadida por mensaje:

| Variante | Resultado |
|---|---|
| Con `compareAndSet` | 6 de 6 remitentes en orden |
| Sin `compareAndSet` | 2 de 6 desordenados — `[39, 40, 42, 41, 43]` |

El patrón invertido es la huella de dos workers procesando al mismo cliente a la vez.
Confirma que la garantía la da el CAS y no la casualidad.

## Decisiones que conviene conocer

- **La cola no tiene límite.** Una cola acotada bloquearía al Selector al llenarse y
  congelaría el servidor entero. El precio es que la memoria crece si los workers no dan
  abasto.
- **Las órdenes se aplican antes de `select()`.** Al revés habría una carrera: el
  `wakeup()` llegaría antes de registrar el interés y la respuesta dormiría hasta el
  siguiente evento.
- **Un privado no entregable produce un solo `ERROR`, sin `ACK`.** Enviar ambos obligaría
  a los clientes a manejar un `ACK` que puede desmentirse después.
- **Sesión única:** la conexión rechazada es la nueva; la activa sigue intacta.

Las limitaciones conocidas (caída sucia de un cliente, memoria de la cola, ausencia de
límite de tasa) están en la sección 13 del documento de arquitectura.
