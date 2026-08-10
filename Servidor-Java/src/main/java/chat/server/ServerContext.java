package chat.server;

import chat.json.Json;

import java.nio.ByteBuffer;
import java.nio.channels.Selector;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Queue;
import java.util.Set;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Estado compartido del servidor y los dos canales que conectan al Selector con los
 * workers.
 *
 * <p>Aquí vive el mecanismo de sincronización principal: {@link #readyQueue}, una
 * {@code BlockingQueue} de clientes con trabajo pendiente. El Selector produce, los
 * workers consumen, y la cola coordina la espera.
 */
public final class ServerContext {

    /** Delimitador de mensajes acordado en el protocolo. */
    public static final char DELIMITER = '\n';

    /**
     * Clientes con trabajo pendiente que ningún worker está atendiendo.
     *
     * <p>Sin límite de capacidad, y es deliberado: una cola acotada bloquearía al hilo
     * del Selector cuando se llenara, y el servidor entero dejaría de aceptar, leer y
     * escribir para todos. El precio es que la memoria crece si los workers no dan
     * abasto.
     */
    public final BlockingQueue<ClientState> readyQueue = new LinkedBlockingQueue<>();

    /** Órdenes que los workers dejan para que el Selector las aplique al despertar. */
    public final Queue<Command> pendingCommands = new ConcurrentLinkedQueue<>();

    /** Sesiones activas, por identificador de usuario. Una sola por usuario. */
    public final Map<String, ClientState> connectedUsers = new ConcurrentHashMap<>();

    /** Integrantes de cada grupo. Los conjuntos son concurrentes: se iteran mientras cambian. */
    public final Map<String, Set<String>> groups = new ConcurrentHashMap<>();

    /**
     * Usuarios que se han conectado desde que arrancó el servidor.
     *
     * <p>Permite distinguir {@code USER_NOT_FOUND} (nombre nunca visto) de
     * {@code USER_DISCONNECTED} (nombre conocido, sin sesión activa). Se pierde al
     * reiniciar, igual que el resto del estado.
     */
    public final Set<String> knownUsers = ConcurrentHashMap.newKeySet();

    private volatile Selector selector;

    public void bindSelector(Selector selector) {
        this.selector = selector;
    }

    // ------------------------------------------------------- cola de clientes listos

    /**
     * Publica al cliente en la cola de listos, si no estaba ya publicado.
     *
     * <p>El {@code compareAndSet} es lo que sostiene el invariante: si el Selector y un
     * worker lo intentan a la vez, exactamente uno gana y el cliente queda encolado una
     * sola vez.
     */
    public void schedule(ClientState client) {
        if (client.scheduled.compareAndSet(false, true)) {
            readyQueue.offer(client);
        }
    }

    /**
     * Libera al cliente después de procesar un mensaje y lo devuelve al final de la
     * cola si le quedó trabajo.
     */
    public void release(ClientState client) {
        client.scheduled.set(false);
        if (!client.pending.isEmpty()) {
            schedule(client);
        }
    }

    // ------------------------------------------------------------------- respuestas

    /** Encola una respuesta para un cliente y avisa al Selector. */
    public void send(ClientState target, Map<String, Object> message) {
        String line = Json.write(message) + DELIMITER;
        target.outputQueue.offer(ByteBuffer.wrap(line.getBytes(StandardCharsets.UTF_8)));
        pendingCommands.offer(new Command.EnableWrite(target));
        wakeup();
    }

    /** Encola una respuesta para el usuario indicado, si tiene sesión activa. */
    public void sendTo(String userId, Map<String, Object> message) {
        ClientState target = connectedUsers.get(userId);
        if (target == null) {
            return;   // se desconectó entre que se resolvió el destinatario y ahora
        }
        send(target, message);
    }

    /** Pide al Selector que cierre la conexión cuando termine de vaciar su salida. */
    public void requestClose(ClientState client) {
        client.closeAfterWrite = true;
        pendingCommands.offer(new Command.CloseConnection(client));
        wakeup();
    }

    private void wakeup() {
        Selector current = selector;
        if (current != null) {
            current.wakeup();
        }
    }
}
