package chat.server;

import chat.app.ChatLogic;

/**
 * Consumidor de la cola de clientes listos.
 *
 * <p>Toma un cliente, procesa <b>un solo</b> mensaje suyo y lo devuelve al final de la
 * cola si le quedó trabajo. Vaciar de una vez toda la cola personal de un cliente
 * dejaría a este worker secuestrado por quien más escribe; procesar de a uno reparte el
 * trabajo por turnos.
 *
 * <p>Nunca toca sockets: ni {@code write()}, ni {@code interestOps()}, ni {@code close()}.
 * Lo que necesita del Selector lo pide por la cola de órdenes.
 */
public final class Worker implements Runnable {

    private final ServerContext context;
    private final ChatLogic logic;

    public Worker(ServerContext context, ChatLogic logic) {
        this.context = context;
        this.logic = logic;
    }

    @Override
    public void run() {
        while (!Thread.currentThread().isInterrupted()) {
            ClientState client;
            try {
                client = context.readyQueue.take();   // aquí el hilo se bloquea y cede
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }

            try {
                Task task = client.pending.poll();
                if (task != null) {
                    logic.process(client, task.payload());
                }
            } catch (RuntimeException e) {
                // Un fallo procesando un mensaje no puede matar al worker: se quedaría
                // un hilo menos en el pool y nadie se enteraría.
                System.err.println("[worker] error procesando un mensaje de "
                        + client.describe() + ": " + e);
            } finally {
                context.release(client);
            }
        }
    }
}
