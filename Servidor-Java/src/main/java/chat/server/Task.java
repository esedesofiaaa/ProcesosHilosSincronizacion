package chat.server;

/**
 * Un mensaje completo leído de un cliente, pendiente de procesar.
 *
 * <p>El Selector la crea al reconstruir un mensaje; un worker la consume. El texto
 * viaja sin interpretar: parsear el JSON es trabajo del worker, no del Selector.
 */
public record Task(ClientState origin, String payload) {
}
