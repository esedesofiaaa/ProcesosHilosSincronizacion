import Combine
import Foundation
import Network

/// Adaptador de Network.framework para el contrato JSON delimitado por newline.
/// Todo el estado observable se actualiza en el MainActor; NWConnection trabaja
/// en una cola serial independiente para no bloquear la interfaz.
@MainActor
final class ChatConnection: ObservableObject {
    @Published private(set) var statusText = "Desconectado"
    @Published private(set) var isTransportReady = false
    @Published private(set) var isProtocolConnected = false
    @Published private(set) var events: [ChatEvent] = []
    @Published private(set) var conversationMessages: [ConversationMessage] = []
    @Published private(set) var groups: [ChatGroup] = []
    @Published private(set) var users: [ChatUser] = []
    @Published private(set) var groupMembers: [String: [ChatUser]] = [:]

    var hasActiveTransport: Bool {
        connection != nil
    }

    private let networkQueue = DispatchQueue(label: "edu.chat.swift.tcp")
    private let maximumReceiveLength = 64 * 1024
    private let maximumBufferedBytes = 1_048_576
    private let maximumEvents = 500
    private let maximumConversationMessages = 1_000

    private var connection: NWConnection?
    private var receiveBuffer = Data()
    private var configuredUserID = ""
    private var connectWasSent = false
    private var receiveLoopStarted = false
    /// requestId → destinatario, para poder retirar el mensaje privado que se
    /// pintó de forma optimista si el servidor responde ERROR.
    private var pendingPrivateSends: [String: String] = [:]

    func connect(host: String, portText: String, userID: String) {
        let normalizedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalizedPort = portText.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalizedUserID = userID.trimmingCharacters(in: .whitespacesAndNewlines)

        guard !normalizedHost.isEmpty else {
            reportLocalError("Indica un host antes de conectar.")
            return
        }

        guard let portValue = UInt16(normalizedPort), portValue > 0,
              let endpointPort = NWEndpoint.Port(rawValue: portValue) else {
            reportLocalError("El puerto debe ser un número entre 1 y 65535.")
            return
        }

        guard !normalizedUserID.isEmpty else {
            reportLocalError("Indica un identificador de usuario antes de conectar.")
            return
        }

        stopCurrentConnection(recordClosure: false)
        // Una sesión nueva puede usar otro usuario: el historial anterior ya no
        // le corresponde.
        conversationMessages.removeAll()

        let newConnection = NWConnection(
            host: NWEndpoint.Host(normalizedHost),
            port: endpointPort,
            using: .tcp
        )

        connection = newConnection
        configuredUserID = normalizedUserID
        receiveBuffer.removeAll(keepingCapacity: true)
        connectWasSent = false
        receiveLoopStarted = false
        isTransportReady = false
        isProtocolConnected = false
        statusText = "Conectando a \(normalizedHost):\(portValue)…"
        appendEvent(.info, title: "Conexión TCP", detail: statusText)

        newConnection.stateUpdateHandler = { [weak self, weak newConnection] state in
            guard let self, let newConnection else {
                return
            }

            Task { @MainActor in
                self.handleConnectionState(state, from: newConnection)
            }
        }

        newConnection.start(queue: networkQueue)
    }

    func disconnect() {
        guard connection != nil else {
            return
        }

        stopCurrentConnection(
            status: "Desconectado por el usuario.",
            kind: .info,
            recordClosure: true
        )
    }

    func clearEvents() {
        events.removeAll()
    }

    @discardableResult
    func sendPrivate(to recipient: String, text: String) -> Bool {
        guard requireProtocolConnection() else {
            return false
        }

        let normalizedRecipient = recipient.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedRecipient.isEmpty else {
            reportLocalError("Indica el destinatario del mensaje privado.")
            return false
        }

        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            reportLocalError("El mensaje privado no puede estar vacío.")
            return false
        }

        let requestID = nextRequestID()
        let queued = send(
            OutgoingMessage(
                type: "PRIVATE_SEND",
                requestId: requestID,
                to: normalizedRecipient,
                text: text
            )
        )

        if queued {
            pendingPrivateSends[requestID] = normalizedRecipient
            appendConversationMessage(
                ConversationMessage(
                    id: "local-\(requestID)",
                    conversationID: normalizedRecipient,
                    kind: .privateChat,
                    sender: configuredUserID,
                    text: text,
                    isOutgoing: true,
                    timestamp: Date()
                )
            )
        }

        return queued
    }

    @discardableResult
    func joinGroup(_ groupID: String) -> Bool {
        sendGroupMembership(type: "GROUP_JOIN", groupID: groupID)
    }

    @discardableResult
    func leaveGroup(_ groupID: String) -> Bool {
        sendGroupMembership(type: "GROUP_LEAVE", groupID: groupID)
    }

    @discardableResult
    func sendGroup(groupID: String, text: String) -> Bool {
        guard requireProtocolConnection() else {
            return false
        }

        let normalizedGroupID = groupID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedGroupID.isEmpty else {
            reportLocalError("Indica el grupo de destino.")
            return false
        }

        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            reportLocalError("El mensaje grupal no puede estar vacío.")
            return false
        }

        return send(
            OutgoingMessage(
                type: "GROUP_SEND",
                requestId: nextRequestID(),
                groupId: normalizedGroupID,
                text: text
            )
        )
    }

    private func sendGroupMembership(type: String, groupID: String) -> Bool {
        guard requireProtocolConnection() else {
            return false
        }

        let normalizedGroupID = groupID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedGroupID.isEmpty else {
            reportLocalError("Indica el identificador del grupo.")
            return false
        }

        return send(
            OutgoingMessage(
                type: type,
                requestId: nextRequestID(),
                groupId: normalizedGroupID
            )
        )
    }

    private func handleConnectionState(_ state: NWConnection.State, from current: NWConnection) {
        guard connection === current else {
            return
        }

        switch state {
        case .setup:
            break
        case .preparing:
            statusText = "Preparando la conexión TCP…"
        case .waiting(let error):
            statusText = "Esperando red: \(error.localizedDescription)"
            appendEvent(.info, title: "Conexión TCP", detail: statusText)
        case .ready:
            isTransportReady = true
            statusText = "TCP conectado; enviando CONNECT…"
            appendEvent(.info, title: "Conexión TCP", detail: statusText)
            beginReceiveLoop(on: current)
            sendConnect()
        case .failed(let error):
            stopCurrentConnection(
                status: "Error TCP: \(error.localizedDescription)",
                kind: .error,
                recordClosure: true
            )
        case .cancelled:
            stopCurrentConnection(
                status: "La conexión TCP fue cancelada.",
                kind: .info,
                recordClosure: true
            )
        @unknown default:
            stopCurrentConnection(
                status: "La conexión TCP entró en un estado no reconocido.",
                kind: .error,
                recordClosure: true
            )
        }
    }

    private func sendConnect() {
        guard !connectWasSent else {
            return
        }

        connectWasSent = true
        _ = send(
            OutgoingMessage(
                type: "CONNECT",
                requestId: nextRequestID(),
                userId: configuredUserID
            )
        )
    }

    private func beginReceiveLoop(on current: NWConnection) {
        guard !receiveLoopStarted else {
            return
        }

        receiveLoopStarted = true
        receiveNext(on: current)
    }

    /// NWConnection puede entregar un JSON dividido o varios JSON juntos. Por ello
    /// el acumulador busca delimitadores en bytes, no en cada callback de receive.
    private func receiveNext(on current: NWConnection) {
        current.receive(
            minimumIncompleteLength: 1,
            maximumLength: maximumReceiveLength
        ) { [weak self, weak current] content, _, isComplete, error in
            guard let self, let current else {
                return
            }

            Task { @MainActor in
                guard self.connection === current else {
                    return
                }

                if let content, !content.isEmpty {
                    self.consumeReceivedBytes(content)
                }

                if let error {
                    self.stopCurrentConnection(
                        status: "Error al recibir datos: \(error.localizedDescription)",
                        kind: .error,
                        recordClosure: true
                    )
                    return
                }

                if isComplete {
                    self.stopCurrentConnection(
                        status: "El servidor cerró la conexión.",
                        kind: .info,
                        recordClosure: true
                    )
                    return
                }

                self.receiveNext(on: current)
            }
        }
    }

    private func consumeReceivedBytes(_ bytes: Data) {
        receiveBuffer.append(bytes)

        while let newlineIndex = receiveBuffer.firstIndex(of: 0x0A) {
            var line = Data(receiveBuffer.prefix(upTo: newlineIndex))
            receiveBuffer.removeSubrange(...newlineIndex)

            // Es tolerante con un servidor que emita CRLF, aunque el contrato usa LF.
            if line.last == 0x0D {
                line.removeLast()
            }

            guard !line.isEmpty else {
                continue
            }

            handleIncomingLine(line)
        }

        if receiveBuffer.count > maximumBufferedBytes {
            stopCurrentConnection(
                status: "Se superó el límite de un mensaje TCP sin delimitador.",
                kind: .error,
                recordClosure: true
            )
        }
    }

    private func handleIncomingLine(_ line: Data) {
        guard let jsonObject = try? JSONSerialization.jsonObject(with: line),
              let message = jsonObject as? [String: Any],
              let type = stringValue("type", in: message) else {
            appendEvent(
                .error,
                title: "Respuesta inválida",
                detail: "Se recibió una línea que no es un objeto JSON UTF-8 válido."
            )
            return
        }

        switch type {
        case "CONNECTED":
            let connectedUser = stringValue("userId", in: message) ?? configuredUserID
            let requestID = stringValue("requestId", in: message) ?? "sin requestId"
            isProtocolConnected = true
            statusText = "Conectado como \(connectedUser)."
            appendEvent(
                .info,
                title: "CONNECTED",
                detail: "Usuario: \(connectedUser) · requestId: \(requestID)"
            )
        case "ACK":
            let operation = stringValue("operation", in: message) ?? "operación"
            let requestID = stringValue("requestId", in: message) ?? "sin requestId"
            pendingPrivateSends.removeValue(forKey: requestID)
            appendEvent(
                .acknowledgement,
                title: "ACK · \(operation)",
                detail: "requestId: \(requestID)"
            )
        case "PRIVATE_MESSAGE":
            let sender = stringValue("from", in: message) ?? "desconocido"
            let text = stringValue("text", in: message) ?? ""
            let messageID = stringValue("messageId", in: message) ?? nextRequestID()
            appendConversationMessage(
                ConversationMessage(
                    id: messageID,
                    conversationID: sender,
                    kind: .privateChat,
                    sender: sender,
                    text: text,
                    isOutgoing: false,
                    timestamp: Date()
                )
            )
            appendEvent(
                .message,
                title: "Privado de \(sender)",
                detail: "\(text)\nmessageId: \(messageID)"
            )
        case "GROUP_MESSAGE":
            let groupID = stringValue("groupId", in: message) ?? "sin grupo"
            let sender = stringValue("from", in: message) ?? "desconocido"
            let text = stringValue("text", in: message) ?? ""
            let messageID = stringValue("messageId", in: message) ?? nextRequestID()
            appendConversationMessage(
                ConversationMessage(
                    id: messageID,
                    conversationID: groupID,
                    kind: .groupChat,
                    sender: sender,
                    text: text,
                    isOutgoing: sender == configuredUserID,
                    timestamp: Date()
                )
            )
            appendEvent(
                .message,
                title: "Grupo \(groupID) · \(sender)",
                detail: "\(text)\nmessageId: \(messageID)"
            )
        case "GROUP_LIST":
            handleGroupList(message)
        case "GROUP_MEMBERS":
            handleGroupMembers(message)
        case "USERS_LIST":
            handleUsersList(message)
        case "ERROR":
            let code = stringValue("code", in: message) ?? "UNKNOWN_ERROR"
            let serverMessage = stringValue("message", in: message) ?? "Sin detalle del servidor."
            let requestID = stringValue("requestId", in: message) ?? "sin requestId"
            var detail = "\(serverMessage)\nrequestId: \(requestID)"

            // Si el rechazo corresponde a un privado ya pintado, se retira la
            // burbuja optimista para no dar por entregado lo que no lo fue.
            if let recipient = pendingPrivateSends.removeValue(forKey: requestID) {
                conversationMessages.removeAll { $0.id == "local-\(requestID)" }
                detail += "\nEl mensaje para \(recipient) no se entregó."
            }

            appendEvent(
                .error,
                title: "ERROR · \(code)",
                detail: detail
            )
        default:
            appendEvent(
                .info,
                title: "Evento \(type)",
                detail: "Se recibió un tipo no reconocido por la interfaz."
            )
        }
    }

    private func handleGroupList(_ message: [String: Any]) {
        let entries = listEntries(
            "groups",
            in: message,
            identifierKeys: ["groupId"],
            labelKeys: ["name", "groupName", "groupId"]
        )

        groups = entries.map { ChatGroup(id: $0.id, name: $0.label) }
        appendEvent(
            .info,
            title: "GROUP_LIST",
            detail: "El servidor publicó \(entries.count) grupo(s)."
        )
    }

    private func handleGroupMembers(_ message: [String: Any]) {
        guard let groupID = stringValue("groupId", in: message), !groupID.isEmpty else {
            appendEvent(
                .error,
                title: "GROUP_MEMBERS inválido",
                detail: "El evento opcional requiere groupId."
            )
            return
        }

        let entries = listEntries(
            "members",
            in: message,
            identifierKeys: ["userId"],
            labelKeys: ["displayName", "name", "userId"]
        )

        groupMembers[groupID] = entries.map { ChatUser(id: $0.id) }
        appendEvent(
            .info,
            title: "GROUP_MEMBERS",
            detail: "El servidor publicó \(entries.count) miembro(s) para \(groupID)."
        )
    }

    private func handleUsersList(_ message: [String: Any]) {
        let entries = listEntries(
            "users",
            in: message,
            identifierKeys: ["userId"],
            labelKeys: ["displayName", "name", "userId"]
        )

        users = entries.map { ChatUser(id: $0.id) }
        appendEvent(
            .info,
            title: "USERS_LIST",
            detail: "El servidor publicó \(entries.count) persona(s)."
        )
    }

    private func listEntries(
        _ key: String,
        in message: [String: Any],
        identifierKeys: [String],
        labelKeys: [String]
    ) -> [(id: String, label: String)] {
        guard let rawEntries = message[key] as? [Any] else {
            return []
        }

        return rawEntries.compactMap { rawEntry in
            if let value = rawEntry as? String {
                let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
                return normalized.isEmpty ? nil : (id: normalized, label: normalized)
            }

            guard let object = rawEntry as? [String: Any] else {
                return nil
            }

            let identifier = identifierKeys
                .compactMap { stringValue($0, in: object)?.trimmingCharacters(in: .whitespacesAndNewlines) }
                .first { !$0.isEmpty }
            guard let identifier else {
                return nil
            }

            let label = labelKeys
                .compactMap { stringValue($0, in: object)?.trimmingCharacters(in: .whitespacesAndNewlines) }
                .first { !$0.isEmpty }
                ?? identifier
            return (id: identifier, label: label)
        }
    }

    private func appendConversationMessage(_ message: ConversationMessage) {
        conversationMessages.append(message)

        if conversationMessages.count > maximumConversationMessages {
            conversationMessages.removeFirst(conversationMessages.count - maximumConversationMessages)
        }
    }

    @discardableResult
    private func send(_ message: OutgoingMessage) -> Bool {
        guard let current = connection, isTransportReady else {
            reportLocalError("No hay una conexión TCP lista para enviar la solicitud.")
            return false
        }

        do {
            var wireData = try JSONEncoder().encode(message)
            wireData.append(0x0A)

            current.send(content: wireData, completion: .contentProcessed { [weak self, weak current] error in
                guard let self, let current else {
                    return
                }

                Task { @MainActor in
                    guard self.connection === current else {
                        return
                    }

                    if let error {
                        self.stopCurrentConnection(
                            status: "Error al enviar datos: \(error.localizedDescription)",
                            kind: .error,
                            recordClosure: true
                        )
                    }
                }
            })

            appendEvent(
                .info,
                title: "Solicitud enviada · \(message.type)",
                detail: "requestId: \(message.requestId)"
            )
            return true
        } catch {
            reportLocalError("No se pudo codificar el JSON: \(error.localizedDescription)")
            return false
        }
    }

    private func stopCurrentConnection(
        status: String = "Desconectado",
        kind: ChatEventKind = .info,
        recordClosure: Bool
    ) {
        let current = connection
        connection = nil
        current?.stateUpdateHandler = nil
        current?.cancel()

        receiveBuffer.removeAll(keepingCapacity: false)
        configuredUserID = ""
        connectWasSent = false
        receiveLoopStarted = false
        isTransportReady = false
        isProtocolConnected = false
        pendingPrivateSends.removeAll()
        // Los directorios son estado del servidor y dejan de ser válidos, pero
        // el historial recibido se conserva: al caerse la conexión es justo
        // cuando el usuario quiere releer lo que pasó.
        groups.removeAll()
        users.removeAll()
        groupMembers.removeAll()
        statusText = status

        if recordClosure {
            appendEvent(kind, title: "Conexión TCP", detail: status)
        }
    }

    private func requireProtocolConnection() -> Bool {
        guard connection != nil, isTransportReady, isProtocolConnected else {
            reportLocalError("Espera la respuesta CONNECTED antes de enviar operaciones del chat.")
            return false
        }

        return true
    }

    private func reportLocalError(_ detail: String) {
        appendEvent(.error, title: "Cliente", detail: detail)
    }

    private func appendEvent(_ kind: ChatEventKind, title: String, detail: String) {
        events.append(ChatEvent(kind: kind, title: title, detail: detail))

        if events.count > maximumEvents {
            events.removeFirst(events.count - maximumEvents)
        }
    }

    private func nextRequestID() -> String {
        "swift-\(UUID().uuidString.lowercased())"
    }

}

/// El contrato define estos campos como texto, pero un `messageId` numérico no
/// debe perderse en silencio: se normaliza a String.
private func stringValue(_ key: String, in message: [String: Any]) -> String? {
    guard let rawValue = message[key] else {
        return nil
    }

    if let text = rawValue as? String {
        return text
    }

    if let number = rawValue as? NSNumber {
        return number.stringValue
    }

    return nil
}
