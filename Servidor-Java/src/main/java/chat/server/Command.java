package chat.server;

/**
 * Orden que un worker deja para que el Selector la ejecute al despertar.
 *
 * <p>Existe porque {@code selector.wakeup()} es una señal sin contenido: despierta al
 * Selector pero no le dice qué cambió. Y porque las operaciones sobre claves de
 * selección están reservadas al hilo del Selector — un worker que llamara a
 * {@code interestOps()} podría bloquearse mientras el Selector está dentro de
 * {@code select()}.
 */
public sealed interface Command {

    /** El cliente tiene respuestas pendientes: hay que activarle {@code OP_WRITE}. */
    record EnableWrite(ClientState client) implements Command {
    }

    /** Cerrar la conexión por una decisión de aplicación, no por un evento de socket. */
    record CloseConnection(ClientState client) implements Command {
    }
}
