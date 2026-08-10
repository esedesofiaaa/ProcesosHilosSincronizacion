package chat.server;

import java.nio.ByteBuffer;
import java.nio.channels.SelectionKey;
import java.nio.channels.SocketChannel;
import java.util.HashSet;
import java.util.Queue;
import java.util.Set;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Todo lo que el servidor sabe de una conexión.
 *
 * <p>Los campos tienen dueños distintos y conviene no mezclarlos:
 * <ul>
 *   <li>{@code readBuffer}, {@code currentWrite} y {@code key} pertenecen al hilo del
 *       Selector. Ningún worker los toca.</li>
 *   <li>{@code pending}, {@code scheduled} y {@code outputQueue} cruzan la frontera
 *       entre hilos, y por eso son concurrentes.</li>
 *   <li>{@code userId} y {@code seenRequestIds} solo los usa el worker que atiende a
 *       este cliente. Como un cliente lo atiende un worker a la vez, no necesitan
 *       protección: es confinamiento, no exclusión mutua.</li>
 * </ul>
 */
public final class ClientState {

    /** Número secuencial asignado por el Selector al aceptar la conexión. */
    public final long connectionId;

    public final SocketChannel channel;

    /** Mensajes de este cliente pendientes de procesar, en orden de llegada. */
    public final Queue<Task> pending = new ConcurrentLinkedQueue<>();

    /**
     * Verdadero cuando el cliente está en la cola de listos o un worker lo está
     * atendiendo. Es el cerrojo lógico que sostiene el invariante: un cliente en la
     * cola como máximo una vez, y un solo worker a la vez.
     */
    public final AtomicBoolean scheduled = new AtomicBoolean(false);

    /**
     * Respuestas listas para enviar. No es una BlockingQueue porque nadie espera
     * sobre ella: el worker agrega sin bloquearse y el Selector retira sin bloquearse.
     */
    public final Queue<ByteBuffer> outputQueue = new ConcurrentLinkedQueue<>();

    /** Bytes de entrada que todavía no forman un mensaje completo. */
    public StringBuilder readBuffer = new StringBuilder();

    /** Respuesta a medio escribir, cuando el socket se llenó antes de vaciarla. */
    public ByteBuffer currentWrite;

    public SelectionKey key;

    /** Nulo hasta que el CONNECT se acepta. */
    public volatile String userId;

    /** Identificadores ya usados en esta sesión; se descartan al cerrarla. */
    public final Set<String> seenRequestIds = new HashSet<>();

    /** Marca que la conexión debe cerrarse en cuanto se vacíe la cola de salida. */
    public volatile boolean closeAfterWrite;

    public ClientState(long connectionId, SocketChannel channel) {
        this.connectionId = connectionId;
        this.channel = channel;
    }

    /** Descripción corta para los registros de consola. */
    public String describe() {
        return userId != null ? userId : "conexión#" + connectionId;
    }
}
