import SwiftUI

struct ContentView: View {
    @ObservedObject var client: ChatConnection

    @State private var selectedConversation: ConversationSelection?
    @State private var draft = ""
    @State private var host = "127.0.0.1"
    @State private var port = ""
    @State private var userID = ""
    @State private var manualGroupID = ""
    @State private var showConnectionPopover = false
    @State private var showActivityPopover = false
    @State private var showGroupPopover = false

    var body: some View {
        HStack(spacing: 0) {
            WorkspaceSidebar(
                currentUserID: userID,
                isConnected: client.isProtocolConnected,
                statusText: client.statusText,
                groups: client.groups,
                selectedConversation: selectedConversation,
                onSelect: selectConversation,
                onSettings: { showConnectionPopover = true },
                onActivity: { showActivityPopover = true },
                onAddGroup: { showGroupPopover = true }
            )
            .frame(width: 270)
            .popover(isPresented: $showConnectionPopover, arrowEdge: .top) {
                ConnectionPopover(
                    host: $host,
                    port: $port,
                    userID: $userID,
                    isConnected: client.hasActiveTransport,
                    statusText: client.statusText,
                    onConnect: {
                        client.connect(host: host, portText: port, userID: userID)
                    },
                    onDisconnect: {
                        client.disconnect()
                    }
                )
            }
            .popover(isPresented: $showActivityPopover, arrowEdge: .bottom) {
                ActivityPopover(events: client.events) {
                    client.clearEvents()
                }
            }
            .popover(isPresented: $showGroupPopover, arrowEdge: .top) {
                JoinGroupPopover(groupID: $manualGroupID) { groupID in
                    if client.joinGroup(groupID) {
                        selectedConversation = .group(groupID)
                        showGroupPopover = false
                    }
                }
            }

            Divider()

            ConversationPane(
                client: client,
                selectedConversation: selectedConversation,
                draft: $draft,
                onJoinGroup: joinGroup,
                onLeaveGroup: leaveGroup,
                onSend: sendDraft
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Divider()

            PeopleSidebar(
                currentUserID: userID,
                selectedConversation: selectedConversation,
                users: client.users,
                groupMembers: client.groupMembers,
                onSelect: selectConversation
            )
            .frame(width: 230)
        }
        .background(ChatTheme.canvas)
        .frame(minWidth: 1_180, minHeight: 760)
        .onChange(of: client.isProtocolConnected) { isConnected in
            if !isConnected {
                // Se descarta el borrador porque ya no se puede enviar, pero la
                // conversación sigue abierta para poder releer el historial.
                draft = ""
            }
        }
    }

    private func selectConversation(_ conversation: ConversationSelection) {
        selectedConversation = conversation
        draft = ""
    }

    private func joinGroup(_ groupID: String) {
        _ = client.joinGroup(groupID)
    }

    private func leaveGroup(_ groupID: String) {
        _ = client.leaveGroup(groupID)
    }

    private func sendDraft() {
        guard let selectedConversation else {
            return
        }

        let message = draft
        guard !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return
        }

        let sent: Bool
        switch selectedConversation {
        case .group(let groupID):
            sent = client.sendGroup(groupID: groupID, text: message)
        case .person(let userID):
            sent = client.sendPrivate(to: userID, text: message)
        }

        if sent {
            draft = ""
        }
    }
}

private enum ConversationSelection: Hashable {
    case group(String)
    case person(String)

    var id: String {
        switch self {
        case .group(let groupID), .person(let groupID):
            return groupID
        }
    }

    var isGroup: Bool {
        if case .group = self {
            return true
        }
        return false
    }
}

private enum ChatTheme {
    static let canvas = Color(red: 0.93, green: 0.95, blue: 0.97)
    static let sidebar = Color(red: 0.12, green: 0.16, blue: 0.22)
    static let sidebarSelected = Color(red: 0.20, green: 0.38, blue: 0.52)
    static let conversation = Color(red: 0.98, green: 0.985, blue: 0.99)
    static let composer = Color(red: 0.94, green: 0.96, blue: 0.98)
    static let outgoingBubble = Color(red: 0.78, green: 0.90, blue: 0.98)
    static let incomingBubble = Color.white
}

private struct WorkspaceSidebar: View {
    let currentUserID: String
    let isConnected: Bool
    let statusText: String
    let groups: [ChatGroup]
    let selectedConversation: ConversationSelection?
    let onSelect: (ConversationSelection) -> Void
    let onSettings: () -> Void
    let onActivity: () -> Void
    let onAddGroup: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                AvatarView(title: currentUserID.isEmpty ? "?" : currentUserID, color: .cyan)
                    .frame(width: 38, height: 38)

                VStack(alignment: .leading, spacing: 2) {
                    Text(currentUserID.isEmpty ? "Cliente de chat" : currentUserID)
                        .font(.headline)
                        .lineLimit(1)
                    Text(isConnected ? "Conectado" : "Sin conexión")
                        .font(.caption)
                        .foregroundStyle(isConnected ? .green : .white.opacity(0.55))
                }

                Spacer(minLength: 4)

                Button(action: onSettings) {
                    Image(systemName: "slider.horizontal.3")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.white.opacity(0.8))
                .help("Configurar conexión")
            }
            .padding(18)

            Divider()
                .overlay(.white.opacity(0.12))

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack {
                        SidebarSectionTitle(title: "GRUPOS")
                        Spacer()
                        Button(action: onAddGroup) {
                            Image(systemName: "plus")
                                .font(.caption.weight(.bold))
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.white.opacity(0.75))
                        .help("Unirse a un grupo")
                    }

                    if groups.isEmpty {
                        SidebarPlaceholder(
                            icon: "rectangle.3.group",
                            text: "Esperando GROUP_LIST del servidor"
                        )
                    } else {
                        VStack(spacing: 4) {
                            ForEach(groups) { group in
                                SidebarConversationRow(
                                    icon: "person.3.fill",
                                    title: group.name,
                                    selected: selectedConversation.map {
                                        $0.isGroup && $0.id == group.id
                                    } ?? false,
                                    action: { onSelect(.group(group.id)) }
                                )
                            }
                        }
                    }

                    Text("Los grupos mostrados provienen del servidor; no se generan datos de ejemplo.")
                        .font(.caption2)
                        .foregroundStyle(.white.opacity(0.45))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(14)
            }

            Divider()
                .overlay(.white.opacity(0.12))

            HStack(spacing: 8) {
                Circle()
                    .fill(isConnected ? .green : .gray)
                    .frame(width: 8, height: 8)
                Text(statusText)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.65))
                    .lineLimit(2)
                Spacer(minLength: 4)
                Button(action: onActivity) {
                    Image(systemName: "bell.badge")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.white.opacity(0.8))
                .help("Actividad, ACK y errores")
            }
            .padding(14)
        }
        .foregroundStyle(.white)
        .background(ChatTheme.sidebar)
    }
}

private struct PeopleSidebar: View {
    let currentUserID: String
    let selectedConversation: ConversationSelection?
    let users: [ChatUser]
    let groupMembers: [String: [ChatUser]]
    let onSelect: (ConversationSelection) -> Void

    private var isGroupSelection: Bool {
        selectedConversation?.isGroup == true
    }

    private var displayedPeople: [ChatUser] {
        let people: [ChatUser]
        if let selectedConversation, case .group(let groupID) = selectedConversation {
            people = groupMembers[groupID] ?? []
        } else {
            people = users
        }

        // El usuario actual puede aparecer en USERS_LIST o GROUP_MEMBERS,
        // pero no es un destinatario privado válido para esta interfaz.
        let normalizedCurrentUserID = currentUserID.trimmingCharacters(in: .whitespacesAndNewlines)
        return people.filter { $0.id != normalizedCurrentUserID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(isGroupSelection ? "MIEMBROS" : "PERSONAS")
                    .font(.caption.weight(.bold))
                    .tracking(1.1)
                    .foregroundStyle(.secondary)
                Spacer()
                Image(systemName: isGroupSelection ? "person.2.fill" : "person.3.fill")
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 18)
            .padding(.top, 20)
            .padding(.bottom, 12)

            if displayedPeople.isEmpty {
                Spacer()
                SidebarPlaceholder(
                    icon: isGroupSelection ? "person.2.slash" : "person.crop.circle.badge.questionmark",
                    text: emptyMessage
                )
                .foregroundStyle(.secondary)
                .padding(18)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 4) {
                        ForEach(displayedPeople) { person in
                            PersonRow(
                                person: person,
                                isCurrentUser: person.id == currentUserID,
                                selected: selectedConversation.map {
                                    !$0.isGroup && $0.id == person.id
                                } ?? false,
                                action: { onSelect(.person(person.id)) }
                            )
                        }
                    }
                    .padding(.horizontal, 10)
                }
            }

            Divider()
                .padding(.top, 10)
            Text(footerMessage)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(16)
        }
        .background(ChatTheme.canvas)
    }

    private var emptyMessage: String {
        if isGroupSelection {
            return "El servidor aún no envió GROUP_MEMBERS para este grupo."
        }
        return "El servidor aún no envió USERS_LIST."
    }

    private var footerMessage: String {
        if isGroupSelection {
            return "Haz clic en un miembro para abrir una conversación privada."
        }
        return "Las personas disponibles dependen de USERS_LIST."
    }
}

private struct ConversationPane: View {
    @ObservedObject var client: ChatConnection
    let selectedConversation: ConversationSelection?
    @Binding var draft: String
    let onJoinGroup: (String) -> Void
    let onLeaveGroup: (String) -> Void
    let onSend: () -> Void

    private var messages: [ConversationMessage] {
        guard let selectedConversation else {
            return []
        }

        return client.conversationMessages.filter { message in
            message.conversationID == selectedConversation.id
                && ((message.kind == .groupChat) == selectedConversation.isGroup)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            if let selectedConversation {
                conversationHeader(for: selectedConversation)
                Divider()
                messageList
                composer
            } else {
                emptyConversation
            }
        }
        .background(ChatTheme.conversation)
    }

    private func conversationHeader(for selection: ConversationSelection) -> some View {
        HStack(spacing: 12) {
            AvatarView(
                title: conversationTitle(for: selection),
                color: selection.isGroup ? .blue : .purple
            )
            .frame(width: 42, height: 42)

            VStack(alignment: .leading, spacing: 3) {
                Text(conversationTitle(for: selection))
                    .font(.title3.weight(.semibold))
                    .lineLimit(1)
                Text(selection.isGroup ? "Conversación grupal" : "Conversación privada")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if case .group(let groupID) = selection {
                Button("Unirse") {
                    onJoinGroup(groupID)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                Button("Salir") {
                    onLeaveGroup(groupID)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 16)
    }

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if messages.isEmpty {
                        Text("Aún no hay mensajes en esta conversación.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, minHeight: 180, alignment: .center)
                    } else {
                        ForEach(messages) { message in
                            MessageBubble(message: message)
                                .id(message.id)
                        }
                    }
                }
                .padding(24)
            }
            .onChange(of: messages.count) { _ in
                if let lastMessage = messages.last {
                    proxy.scrollTo(lastMessage.id, anchor: .bottom)
                }
            }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Escribe un mensaje…", text: $draft, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(1...5)
                .onSubmit(onSend)

            Button(action: onSend) {
                Image(systemName: "paperplane.fill")
                    .font(.headline)
                    .frame(width: 34, height: 34)
            }
            .buttonStyle(.borderedProminent)
            // buttonBorderShape(.circle) y (.capsule) exigen macOS 14, y el
            // paquete apunta a macOS 13; recortar la forma no tiene esa
            // restricción y con un marco cuadrado da el mismo círculo.
            .clipShape(Circle())
            .disabled(!canSend)
            .help("Enviar mensaje")
        }
        .padding(12)
        .background(ChatTheme.composer)
        .overlay(alignment: .top) {
            Divider()
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    private var canSend: Bool {
        client.isProtocolConnected
            && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var emptyConversation: some View {
        VStack(spacing: 14) {
            Image(systemName: "bubble.left.and.bubble.right.fill")
                .font(.system(size: 42))
                .foregroundStyle(.blue.opacity(0.65))
            Text("Selecciona una conversación")
                .font(.title2.weight(.semibold))
            Text("Elige un grupo o haz clic en una persona para comenzar.")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func conversationTitle(for selection: ConversationSelection) -> String {
        switch selection {
        case .group(let groupID):
            return client.groups.first(where: { $0.id == groupID })?.name ?? groupID
        case .person(let personID):
            return personID
        }
    }
}

private struct MessageBubble: View {
    let message: ConversationMessage

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if message.isOutgoing {
                Spacer(minLength: 80)
            } else {
                AvatarView(title: message.sender, color: .purple)
                    .frame(width: 28, height: 28)
            }

            VStack(alignment: message.isOutgoing ? .trailing : .leading, spacing: 5) {
                if !message.isOutgoing {
                    Text(message.sender)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                Text(message.text)
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)
                Text(message.timestamp.formatted(date: .omitted, time: .shortened))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 9)
            .background(
                message.isOutgoing ? ChatTheme.outgoingBubble : ChatTheme.incomingBubble,
                in: RoundedRectangle(cornerRadius: 14)
            )
            .shadow(color: .black.opacity(0.04), radius: 3, y: 1)

            if !message.isOutgoing {
                Spacer(minLength: 80)
            }
        }
    }
}

private struct ConnectionPopover: View {
    @Binding var host: String
    @Binding var port: String
    @Binding var userID: String
    let isConnected: Bool
    let statusText: String
    let onConnect: () -> Void
    let onDisconnect: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Conexión TCP")
                .font(.headline)
            TextField("Host", text: $host)
                .textFieldStyle(.roundedBorder)
                .disabled(isConnected)
            TextField("Puerto", text: $port)
                .textFieldStyle(.roundedBorder)
                .disabled(isConnected)
            TextField("Usuario", text: $userID)
                .textFieldStyle(.roundedBorder)
                .disabled(isConnected)

            Button(isConnected ? "Desconectar" : "Conectar") {
                if isConnected {
                    onDisconnect()
                } else {
                    onConnect()
                }
            }
            .buttonStyle(.borderedProminent)

            Text(statusText)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(width: 260)
    }
}

private struct JoinGroupPopover: View {
    @Binding var groupID: String
    let onJoin: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Abrir grupo")
                .font(.headline)
            Text("Escribe un groupId para enviar GROUP_JOIN. El cliente abre y conserva la conversación aunque el servidor aún no publique GROUP_LIST.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("groupId", text: $groupID)
                .textFieldStyle(.roundedBorder)
            Button("Unirse y abrir") {
                let value = groupID.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !value.isEmpty else {
                    return
                }
                onJoin(value)
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(18)
        .frame(width: 280)
    }
}

private struct ActivityPopover: View {
    let events: [ChatEvent]
    let onClear: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Actividad")
                    .font(.headline)
                Spacer()
                Button("Limpiar", action: onClear)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }

            if events.isEmpty {
                Text("No hay ACK, errores ni eventos todavía.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(events) { event in
                            ActivityRow(event: event)
                        }
                    }
                }
            }
        }
        .padding(18)
        .frame(width: 390, height: 420)
    }
}

private struct ActivityRow: View {
    let event: ChatEvent

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(event.kind.label)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(eventColor)
                Text(event.title)
                    .font(.caption.weight(.semibold))
                Spacer()
                Text(event.timestamp.formatted(date: .omitted, time: .shortened))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Text(event.detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(8)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }

    private var eventColor: Color {
        switch event.kind {
        case .info:
            return .blue
        case .acknowledgement:
            return .green
        case .message:
            return .purple
        case .error:
            return .red
        }
    }
}

private struct SidebarSectionTitle: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.caption.weight(.bold))
            .tracking(1.1)
            .foregroundStyle(.white.opacity(0.55))
    }
}

private struct SidebarConversationRow: View {
    let icon: String
    let title: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .frame(width: 20)
                    .foregroundStyle(selected ? .white : .cyan.opacity(0.9))
                Text(title)
                    .font(.callout)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
            .background(selected ? ChatTheme.sidebarSelected : .clear, in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white)
    }
}

private struct PersonRow: View {
    let person: ChatUser
    let isCurrentUser: Bool
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 9) {
                AvatarView(title: person.displayName, color: isCurrentUser ? .green : .purple)
                    .frame(width: 30, height: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(person.displayName)
                        .font(.callout)
                        .lineLimit(1)
                    if isCurrentUser {
                        Text("Tú")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(7)
            .background(selected ? Color.blue.opacity(0.12) : .clear, in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .foregroundStyle(.primary)
    }
}

private struct SidebarPlaceholder: View {
    let icon: String
    let text: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: icon)
                .font(.title3)
            Text(text)
                .font(.caption)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct AvatarView: View {
    let title: String
    let color: Color

    var body: some View {
        Text(initial)
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(color.gradient, in: Circle())
    }

    private var initial: String {
        String(title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(1)).uppercased()
    }
}
