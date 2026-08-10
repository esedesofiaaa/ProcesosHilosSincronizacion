package chat.server;

import chat.app.ChatLogic;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.Set;

/**
 * El único hilo que administra sockets y claves de selección.
 *
 * <p>Acepta conexiones, lee bytes, reconstruye mensajes, publica clientes en la cola de
 * listos y escribe las respuestas. Como es un solo hilo, todo ese estado está confinado
 * y no necesita protección: es confinamiento a hilo, no exclusión mutua.
 */
public final class SelectorServer implements Runnable {

    /** Tamaño del buffer de lectura reutilizado en cada {@code read()}. */
    private static final int READ_BUFFER_SIZE = 16 * 1024;

    /** Tope de bytes acumulados sin delimitador antes de cortar la conexión. */
    private static final int MAX_PENDING_BYTES = 1024 * 1024;

    private final int port;
    private final ServerContext context;
    private final ChatLogic logic;

    private final ByteBuffer readBuffer = ByteBuffer.allocate(READ_BUFFER_SIZE);
    private long nextConnectionId;

    public SelectorServer(int port, ServerContext context, ChatLogic logic) {
        this.port = port;
        this.context = context;
        this.logic = logic;
    }

    @Override
    public void run() {
        try (Selector selector = Selector.open();
             ServerSocketChannel serverChannel = ServerSocketChannel.open()) {

            serverChannel.bind(new InetSocketAddress(port));
            serverChannel.configureBlocking(false);
            serverChannel.register(selector, SelectionKey.OP_ACCEPT);
            context.bindSelector(selector);

            System.out.println("[servidor] escuchando en el puerto " + port);

            while (!Thread.currentThread().isInterrupted()) {
                // Las órdenes se aplican ANTES de select(). Al revés habría una carrera:
                // el wakeup() de un worker llegaría antes de registrar el interés y la
                // respuesta se quedaría dormida hasta el siguiente evento del socket.
                drainCommands();

                selector.select();

                Set<SelectionKey> selected = selector.selectedKeys();
                Iterator<SelectionKey> iterator = selected.iterator();
                while (iterator.hasNext()) {
                    SelectionKey key = iterator.next();
                    iterator.remove();

                    if (!key.isValid()) {
                        continue;
                    }
                    try {
                        if (key.isAcceptable()) {
                            accept(serverChannel, selector);
                        } else if (key.isReadable()) {
                            read(key);
                        } else if (key.isWritable()) {
                            write(key);
                        }
                    } catch (IOException e) {
                        close(key, "error de E/S: " + e.getMessage());
                    }
                }
            }
        } catch (IOException e) {
            System.err.println("[servidor] no se pudo iniciar: " + e.getMessage());
        }
    }

    // ---------------------------------------------------------------- órdenes

    private void drainCommands() {
        Command command;
        while ((command = context.pendingCommands.poll()) != null) {
            switch (command) {
                case Command.EnableWrite(ClientState client) -> {
                    SelectionKey key = client.key;
                    if (key != null && key.isValid()) {
                        key.interestOps(key.interestOps() | SelectionKey.OP_WRITE);
                    }
                }
                case Command.CloseConnection(ClientState client) -> {
                    SelectionKey key = client.key;
                    if (key != null && key.isValid()) {
                        // Se conserva OP_WRITE para que alcance a salir el ERROR que
                        // explica el cierre; el socket se cierra al vaciar la salida.
                        key.interestOps(key.interestOps() | SelectionKey.OP_WRITE);
                    }
                }
            }
        }
    }

    // ---------------------------------------------------------------- aceptar

    private void accept(ServerSocketChannel serverChannel, Selector selector) throws IOException {
        SocketChannel channel = serverChannel.accept();
        if (channel == null) {
            return;
        }
        channel.configureBlocking(false);

        ClientState client = new ClientState(nextConnectionId++, channel);
        client.key = channel.register(selector, SelectionKey.OP_READ, client);

        System.out.println("[servidor] conexion#" + client.connectionId + " aceptada desde "
                + channel.getRemoteAddress());
    }

    // ------------------------------------------------------------------- leer

    private void read(SelectionKey key) throws IOException {
        ClientState client = (ClientState) key.attachment();
        SocketChannel channel = client.channel;

        readBuffer.clear();
        int read = channel.read(readBuffer);

        if (read == -1) {
            close(key, "el cliente cerro la conexion");
            return;
        }
        if (read == 0) {
            return;
        }

        readBuffer.flip();
        client.readBuffer.append(StandardCharsets.UTF_8.decode(readBuffer));

        extractMessages(client);

        if (client.readBuffer.length() > MAX_PENDING_BYTES) {
            close(key, "se supero el limite de un mensaje sin delimitador");
        }
    }

    /**
     * Extrae del buffer todos los mensajes completos y los deja en la cola personal del
     * cliente, en orden.
     *
     * <p>Una sola lectura puede traer varios mensajes y el fragmento de otro. El resto
     * incompleto se conserva para la próxima lectura. Es el segundo eslabón de la
     * cadena que preserva el orden: como este hilo es único, m1 se encola antes que m2.
     */
    private void extractMessages(ClientState client) {
        int start = 0;
        int delimiter;
        boolean scheduled = false;

        while ((delimiter = client.readBuffer.indexOf(String.valueOf(ServerContext.DELIMITER), start)) >= 0) {
            String line = client.readBuffer.substring(start, delimiter).trim();
            start = delimiter + 1;

            if (line.isEmpty()) {
                continue;
            }
            client.pending.offer(new Task(client, line));
            scheduled = true;
        }

        if (start > 0) {
            client.readBuffer.delete(0, start);
        }
        if (scheduled) {
            // Aunque se hayan extraído varios mensajes, el cliente entra una sola vez:
            // el compareAndSet solo gana la primera. Los demás esperan su turno en la
            // cola personal.
            context.schedule(client);
        }
    }

    // ---------------------------------------------------------------- escribir

    private void write(SelectionKey key) throws IOException {
        ClientState client = (ClientState) key.attachment();
        SocketChannel channel = client.channel;

        while (true) {
            if (client.currentWrite == null) {
                client.currentWrite = client.outputQueue.poll();
                if (client.currentWrite == null) {
                    key.interestOps(key.interestOps() & ~SelectionKey.OP_WRITE);
                    if (client.closeAfterWrite) {
                        close(key, "cierre solicitado por la aplicacion");
                    }
                    return;
                }
            }

            channel.write(client.currentWrite);

            if (client.currentWrite.hasRemaining()) {
                return;   // el socket se llenó: conserva OP_WRITE y el buffer parcial
            }
            client.currentWrite = null;
        }
    }

    // ------------------------------------------------------------------ cerrar

    private void close(SelectionKey key, String reason) {
        ClientState client = (ClientState) key.attachment();
        key.cancel();

        try {
            client.channel.close();
        } catch (IOException e) {
            // Cerrar es lo último que se hace con el socket; un fallo aquí no cambia nada.
        }

        // Las respuestas que quedaran en la cola no se entregan.
        client.outputQueue.clear();
        client.currentWrite = null;

        System.out.println("[servidor] " + client.describe() + " cerrado: " + reason);
        logic.onDisconnect(client);
    }
}
