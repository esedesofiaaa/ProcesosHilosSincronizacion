import Foundation

/// Representa cualquier solicitud que este cliente envía al servidor.
/// Las propiedades opcionales se omiten del JSON cuando no aplican a la operación.
struct OutgoingMessage: Encodable {
    let type: String
    let requestId: String
    var userId: String? = nil
    var to: String? = nil
    var groupId: String? = nil
    var text: String? = nil
}

enum ChatEventKind: String {
    case info
    case acknowledgement
    case message
    case error

    var label: String {
        switch self {
        case .info:
            return "INFO"
        case .acknowledgement:
            return "ACK"
        case .message:
            return "MENSAJE"
        case .error:
            return "ERROR"
        }
    }
}

struct ChatEvent: Identifiable {
    let id = UUID()
    let timestamp = Date()
    let kind: ChatEventKind
    let title: String
    let detail: String
}

enum ConversationKind: Hashable {
    case privateChat
    case groupChat
}

struct ConversationMessage: Identifiable {
    let id: String
    let conversationID: String
    let kind: ConversationKind
    let sender: String
    let text: String
    let isOutgoing: Bool
    let timestamp: Date
}

struct ChatUser: Identifiable, Hashable {
    let id: String

    var displayName: String {
        id
    }
}

struct ChatGroup: Identifiable, Hashable {
    let id: String
    let name: String
}
