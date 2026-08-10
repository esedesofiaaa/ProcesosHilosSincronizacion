package chat.app;

import chat.json.Json;
import chat.server.ClientState;
import chat.server.ServerContext;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Procesamiento de los mensajes del chat.
 *
 * <p>Se ejecuta siempre en un hilo worker, nunca en el del Selector, salvo
 * {@link #onDisconnect} que corre en el Selector al cerrar una conexión.
 *
 * <p>No toca sockets: cuando hay algo que enviar, lo deja en la cola de salida del
 * destinatario a través de {@link ServerContext}.
 */
public final class ChatLogic {

    private final ServerContext context;
    private final AtomicLong messageIds = new AtomicLong();

    public ChatLogic(ServerContext context) {
        this.context = context;
    }

    // ------------------------------------------------------------------ despacho

    /**
     * Procesa un mensaje completo recibido de un cliente.
     *
     * <p>Los campos de sesión de {@code client} ({@code userId}, {@code seenRequestIds})
     * se leen y escriben sin sincronización porque un cliente lo atiende un solo worker
     * a la vez. La visibilidad entre workers distintos la garantiza la cola de listos:
     * el {@code offer}/{@code take} de una {@code BlockingQueue} establece una relación
     * de precedencia, así que el worker que reciba al cliente después verá todo lo que
     * escribió el anterior.
     */
    public void process(ClientState client, String payload) {
        Map<String, Object> request;
        try {
            request = Json.readObject(payload);
        } catch (Json.ParseException e) {
            // Sin JSON válido no hay requestId que devolver: se pierde con la solicitud.
            sendError(client, null, "INVALID_JSON", "El mensaje no es un objeto JSON valido");
            return;
        }

        String type = Json.string(request, "type");
        String requestId = Json.string(request, "requestId");

        if (type == null) {
            sendError(client, requestId, "INVALID_MESSAGE", "Falta el campo type");
            return;
        }

        if (!"CONNECT".equals(type) && client.userId == null) {
            sendError(client, requestId, "NOT_CONNECTED", "Envia CONNECT antes de operar");
            return;
        }

        if (requestId != null && !client.seenRequestIds.add(requestId)) {
            sendError(client, requestId, "DUPLICATE_REQUEST", "El requestId ya fue utilizado");
            return;
        }

        switch (type) {
            case "CONNECT" -> handleConnect(client, request, requestId);
            case "PRIVATE_SEND" -> handlePrivateSend(client, request, requestId);
            case "GROUP_JOIN" -> handleGroupJoin(client, request, requestId);
            case "GROUP_LEAVE" -> handleGroupLeave(client, request, requestId);
            case "GROUP_SEND" -> handleGroupSend(client, request, requestId);
            default -> sendError(client, requestId, "INVALID_MESSAGE", "Tipo no reconocido: " + type);
        }
    }

    // ------------------------------------------------------------------- CONNECT

    private void handleConnect(ClientState client, Map<String, Object> request, String requestId) {
        String userId = Json.string(request, "userId");
        if (userId == null) {
            sendError(client, requestId, "INVALID_MESSAGE", "Falta el campo userId");
            return;
        }

        if (client.userId != null) {
            sendError(client, requestId, "INVALID_MESSAGE", "La sesion ya esta iniciada");
            return;
        }

        // Sesión única: si el usuario ya tiene sesión, la rechazada es la conexión
        // nueva. putIfAbsent resuelve la carrera de dos CONNECT simultáneos — un
        // "consultar y luego insertar" dejaría pasar a los dos.
        ClientState previous = context.connectedUsers.putIfAbsent(userId, client);
        if (previous != null) {
            sendError(client, requestId, "USER_ALREADY_CONNECTED", "El usuario ya tiene una sesion activa");
            context.requestClose(client);
            return;
        }

        client.userId = userId;
        context.knownUsers.add(userId);

        Map<String, Object> response = message("CONNECTED");
        response.put("requestId", requestId);
        response.put("userId", userId);
        context.send(client, response);

        // El cliente recién llegado necesita los directorios; los demás necesitan
        // enterarse de que llegó.
        broadcastUsersList();
        context.send(client, groupListMessage());

        System.out.println("[servidor] " + userId + " conectado (conexion#" + client.connectionId + ")");
    }

    // -------------------------------------------------------------- mensaje privado

    private void handlePrivateSend(ClientState client, Map<String, Object> request, String requestId) {
        String to = Json.string(request, "to");
        String text = Json.string(request, "text");

        if (to == null || text == null) {
            sendError(client, requestId, "INVALID_MESSAGE", "PRIVATE_SEND requiere to y text");
            return;
        }

        ClientState target = context.connectedUsers.get(to);
        if (target == null) {
            // El servidor no guarda usuarios: alguien existe mientras tenga sesión.
            // El conjunto de nombres vistos es lo que permite separar los dos casos.
            if (context.knownUsers.contains(to)) {
                sendError(client, requestId, "USER_DISCONNECTED", "El usuario " + to + " no esta conectado");
            } else {
                sendError(client, requestId, "USER_NOT_FOUND", "El usuario " + to + " no existe");
            }
            return;
        }

        // Un privado no entregable produce un solo ERROR y ningún ACK: enviar ambos
        // obligaría a los clientes a manejar un ACK que puede desmentirse después.
        sendAck(client, requestId, "PRIVATE_SEND");

        Map<String, Object> event = message("PRIVATE_MESSAGE");
        event.put("messageId", nextMessageId());
        event.put("from", client.userId);
        event.put("text", text);
        context.send(target, event);
    }

    // ---------------------------------------------------------------------- grupos

    private void handleGroupJoin(ClientState client, Map<String, Object> request, String requestId) {
        String groupId = Json.string(request, "groupId");
        if (groupId == null) {
            sendError(client, requestId, "INVALID_MESSAGE", "GROUP_JOIN requiere groupId");
            return;
        }

        Set<String> members = context.groups.computeIfAbsent(
                groupId, key -> ConcurrentHashMap.newKeySet());
        members.add(client.userId);

        sendAck(client, requestId, "GROUP_JOIN");
        broadcastGroupList();
        broadcastGroupMembers(groupId);
    }

    private void handleGroupLeave(ClientState client, Map<String, Object> request, String requestId) {
        String groupId = Json.string(request, "groupId");
        if (groupId == null) {
            sendError(client, requestId, "INVALID_MESSAGE", "GROUP_LEAVE requiere groupId");
            return;
        }

        Set<String> members = context.groups.get(groupId);
        if (members == null) {
            sendError(client, requestId, "GROUP_NOT_FOUND", "El grupo " + groupId + " no existe");
            return;
        }
        if (!members.remove(client.userId)) {
            sendError(client, requestId, "NOT_GROUP_MEMBER", "No perteneces al grupo " + groupId);
            return;
        }

        if (members.isEmpty()) {
            context.groups.remove(groupId, members);
        }

        sendAck(client, requestId, "GROUP_LEAVE");
        broadcastGroupList();
        broadcastGroupMembers(groupId);
    }

    private void handleGroupSend(ClientState client, Map<String, Object> request, String requestId) {
        String groupId = Json.string(request, "groupId");
        String text = Json.string(request, "text");

        if (groupId == null || text == null) {
            sendError(client, requestId, "INVALID_MESSAGE", "GROUP_SEND requiere groupId y text");
            return;
        }

        Set<String> members = context.groups.get(groupId);
        if (members == null) {
            sendError(client, requestId, "GROUP_NOT_FOUND", "El grupo " + groupId + " no existe");
            return;
        }
        if (!members.contains(client.userId)) {
            sendError(client, requestId, "NOT_GROUP_MEMBER", "No perteneces al grupo " + groupId);
            return;
        }

        sendAck(client, requestId, "GROUP_SEND");

        Map<String, Object> event = message("GROUP_MESSAGE");
        event.put("messageId", nextMessageId());
        event.put("groupId", groupId);
        event.put("from", client.userId);
        event.put("text", text);

        // El mensaje llega a todos los integrantes, incluido el remitente. Los que no
        // estén conectados simplemente no lo reciben y no se emite error por ellos.
        for (String member : members) {
            context.sendTo(member, event);
        }
    }

    // --------------------------------------------------------------- desconexión

    /**
     * Limpia el estado de una conexión que se cerró. Corre en el hilo del Selector.
     */
    public void onDisconnect(ClientState client) {
        String userId = client.userId;
        if (userId == null) {
            return;
        }

        // remove(clave, valor) evita borrar la sesión de un usuario que ya volvió a
        // entrar con otra conexión.
        context.connectedUsers.remove(userId, client);

        List<String> affectedGroups = new ArrayList<>();
        context.groups.forEach((groupId, members) -> {
            if (members.remove(userId)) {
                affectedGroups.add(groupId);
            }
        });
        context.groups.entrySet().removeIf(entry -> entry.getValue().isEmpty());

        System.out.println("[servidor] " + userId + " desconectado");

        broadcastUsersList();
        if (!affectedGroups.isEmpty()) {
            broadcastGroupList();
            affectedGroups.forEach(this::broadcastGroupMembers);
        }
    }

    // ---------------------------------------------------------------- directorios

    private void broadcastUsersList() {
        Map<String, Object> event = usersListMessage();
        context.connectedUsers.values().forEach(target -> context.send(target, event));
    }

    private void broadcastGroupList() {
        Map<String, Object> event = groupListMessage();
        context.connectedUsers.values().forEach(target -> context.send(target, event));
    }

    private void broadcastGroupMembers(String groupId) {
        Map<String, Object> event = message("GROUP_MEMBERS");
        event.put("groupId", groupId);

        Set<String> members = context.groups.get(groupId);
        List<Object> entries = new ArrayList<>();
        if (members != null) {
            for (String member : members) {
                Map<String, Object> entry = new LinkedHashMap<>();
                entry.put("userId", member);
                entries.add(entry);
            }
        }
        event.put("members", entries);

        if (members == null) {
            // El grupo desapareció: se avisa a todos para que lo retiren del panel.
            context.connectedUsers.values().forEach(target -> context.send(target, event));
            return;
        }
        for (String member : members) {
            context.sendTo(member, event);
        }
    }

    private Map<String, Object> usersListMessage() {
        List<Object> entries = new ArrayList<>();
        for (String userId : context.connectedUsers.keySet()) {
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("userId", userId);
            entries.add(entry);
        }
        Map<String, Object> event = message("USERS_LIST");
        event.put("users", entries);
        return event;
    }

    private Map<String, Object> groupListMessage() {
        List<Object> entries = new ArrayList<>();
        for (String groupId : context.groups.keySet()) {
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("groupId", groupId);
            entry.put("name", groupId);
            entries.add(entry);
        }
        Map<String, Object> event = message("GROUP_LIST");
        event.put("groups", entries);
        return event;
    }

    // --------------------------------------------------------------------- ayudas

    private void sendAck(ClientState client, String requestId, String operation) {
        Map<String, Object> ack = message("ACK");
        ack.put("requestId", requestId);
        ack.put("operation", operation);
        context.send(client, ack);
    }

    private void sendError(ClientState client, String requestId, String code, String detail) {
        Map<String, Object> error = message("ERROR");
        error.put("requestId", requestId);   // se omite del JSON si es null
        error.put("code", code);
        error.put("message", detail);
        context.send(client, error);
    }

    private Map<String, Object> message(String type) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("type", type);
        return result;
    }

    private String nextMessageId() {
        return "m" + messageIds.incrementAndGet();
    }
}
