package chat;

import chat.app.ChatLogic;
import chat.server.SelectorServer;
import chat.server.ServerContext;
import chat.server.Worker;

/**
 * Arranque del servidor de chat.
 *
 * <p>Levanta el pool fijo de workers y deja el hilo principal ejecutando el Selector.
 *
 * <pre>
 *   java -cp target/classes chat.Main [puerto] [workers]
 * </pre>
 */
public final class Main {

    private static final int DEFAULT_PORT = 1803;
    private static final int DEFAULT_WORKERS = 4;

    public static void main(String[] args) {
        int port = readArgument(args, 0, DEFAULT_PORT, "puerto");
        int workerCount = readArgument(args, 1, DEFAULT_WORKERS, "numero de workers");

        ServerContext context = new ServerContext();
        ChatLogic logic = new ChatLogic(context);

        for (int i = 0; i < workerCount; i++) {
            Thread worker = new Thread(new Worker(context, logic), "worker-" + i);
            worker.setDaemon(true);
            worker.start();
        }
        System.out.println("[servidor] " + workerCount + " workers iniciados");

        new SelectorServer(port, context, logic).run();
    }

    private static int readArgument(String[] args, int index, int fallback, String name) {
        if (args.length <= index) {
            return fallback;
        }
        try {
            int value = Integer.parseInt(args[index]);
            if (value <= 0) {
                throw new NumberFormatException();
            }
            return value;
        } catch (NumberFormatException e) {
            System.err.println("[servidor] " + name + " invalido: '" + args[index]
                    + "'. Se usa " + fallback + ".");
            return fallback;
        }
    }
}
