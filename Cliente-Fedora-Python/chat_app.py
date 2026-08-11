"""Interfaz Tkinter del cliente de chat."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import simpledialog

from chat_client import ChatClient
from chat_protocol import JsonObject
from chat_session import ChatSession


class ChatApp:
    """Interfaz de chat de escritorio; no modifica la capa TCP."""

    COLORS = {
        "background": "#f3f6fb",
        "surface": "#ffffff",
        "sidebar": "#172033",
        "sidebar_hover": "#263652",
        "sidebar_text": "#f4f7fb",
        "sidebar_muted": "#aab6c8",
        "border": "#e2e8f0",
        "text": "#172033",
        "muted": "#718096",
        "accent": "#2f80ed",
        "accent_dark": "#2368c4",
        "bubble_in": "#ffffff",
        "bubble_out": "#dceeff",
        "system": "#eef2f7",
        "success": "#23a36d",
        "danger": "#d9534f",
    }

    def __init__(self, root: tk.Tk, client: ChatClient) -> None:
        self.root = root
        self.client = client
        self._closing = False
        self._connect_thread: threading.Thread | None = None

        self.session = ChatSession(client)
        self._group_order: list[str] = []
        self._people_order: list[str] = []
        self._member_order: list[str] = []

        self.root.title(f"Chat de escritorio · {client.user_id}")
        self.root.geometry("1180x720")
        self.root.minsize(900, 560)
        self.root.configure(bg=self.COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_sidebar()
        self._build_conversation()
        self._build_participants()
        self._refresh_group_list()
        self._refresh_people_list()
        self._refresh_members()
        self._render_selected_conversation()
        self._poll_events()

    def start_connection(self) -> None:
        """Conecta en segundo plano para no bloquear la ventana."""

        self.connection_status_var.set(
            f"Conectando a {self.client.host}:{self.client.port}..."
        )
        self._connect_thread = threading.Thread(
            target=self._connect_in_background,
            name="chat-connect",
            daemon=True,
        )
        self._connect_thread.start()

    def _connect_in_background(self) -> None:
        try:
            self.client.connect()
        except (ConnectionError, OSError, RuntimeError):
            # El detalle ya fue publicado en client.events.
            pass

    def _build_sidebar(self) -> None:
        colors = self.COLORS
        self.sidebar = tk.Frame(
            self.root,
            bg=colors["sidebar"],
            width=250,
            highlightthickness=0,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.rowconfigure(3, weight=1)
        self.sidebar.rowconfigure(5, weight=1)

        profile = tk.Frame(self.sidebar, bg=colors["sidebar"], padx=18, pady=18)
        profile.grid(row=0, column=0, sticky="ew")
        profile.columnconfigure(1, weight=1)
        avatar = tk.Label(
            profile,
            text=self.client.user_id[:1].upper(),
            bg=colors["accent"],
            fg="white",
            width=3,
            height=1,
            font=("TkDefaultFont", 13, "bold"),
        )
        avatar.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        tk.Label(
            profile,
            text=self.client.user_id,
            bg=colors["sidebar"],
            fg=colors["sidebar_text"],
            font=("TkDefaultFont", 10, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew")
        self.connection_status_var = tk.StringVar(value="Desconectado")
        tk.Label(
            profile,
            textvariable=self.connection_status_var,
            bg=colors["sidebar"],
            fg=colors["sidebar_muted"],
            font=("TkDefaultFont", 9),
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", pady=(2, 0))

        separator = tk.Frame(self.sidebar, bg="#2b3952", height=1)
        separator.grid(row=1, column=0, sticky="ew", padx=18)

        groups_header = tk.Frame(self.sidebar, bg=colors["sidebar"], padx=0, pady=0)
        groups_header.grid(row=2, column=0, sticky="ew", padx=18, pady=(18, 7))
        groups_header.columnconfigure(0, weight=1)
        tk.Label(
            groups_header,
            text="GRUPOS",
            bg=colors["sidebar"],
            fg=colors["sidebar_muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.add_group_button = self._make_sidebar_button(
            groups_header,
            text="+",
            command=self._add_group_from_dialog,
            width=3,
        )
        self.add_group_button.grid(row=0, column=1, sticky="e")

        self.group_listbox = self._make_sidebar_listbox(self.sidebar, height=8)
        self.group_listbox.grid(row=3, column=0, sticky="nsew", padx=12)
        self.group_listbox.bind("<<ListboxSelect>>", self._on_group_selected)

        people_header = tk.Frame(self.sidebar, bg=colors["sidebar"], padx=0, pady=0)
        people_header.grid(row=4, column=0, sticky="ew", padx=18, pady=(18, 7))
        tk.Label(
            people_header,
            text="PERSONAS",
            bg=colors["sidebar"],
            fg=colors["sidebar_muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).pack(anchor="w")

        self.people_listbox = self._make_sidebar_listbox(self.sidebar, height=8)
        self.people_listbox.grid(row=5, column=0, sticky="nsew", padx=12, pady=(0, 14))
        self.people_listbox.bind("<<ListboxSelect>>", self._on_person_selected)

    def _build_conversation(self) -> None:
        colors = self.COLORS
        self.center = tk.Frame(self.root, bg=colors["background"])
        self.center.grid(row=0, column=1, sticky="nsew")
        self.center.rowconfigure(1, weight=1)
        self.center.columnconfigure(0, weight=1)

        header = tk.Frame(
            self.center,
            bg=colors["surface"],
            height=76,
            highlightbackground=colors["border"],
            highlightthickness=1,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(1, weight=1)

        self.header_avatar = tk.Label(
            header,
            text="·",
            bg="#e7edf6",
            fg=colors["accent"],
            width=3,
            height=1,
            font=("TkDefaultFont", 13, "bold"),
        )
        self.header_avatar.grid(row=0, column=0, rowspan=2, padx=(22, 12), pady=17)
        self.header_title_var = tk.StringVar(value="Selecciona un grupo o persona")
        self.header_subtitle_var = tk.StringVar(
            value="Las conversaciones aparecerán aquí"
        )
        tk.Label(
            header,
            textvariable=self.header_title_var,
            bg=colors["surface"],
            fg=colors["text"],
            font=("TkDefaultFont", 12, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="sw", pady=(15, 0))
        tk.Label(
            header,
            textvariable=self.header_subtitle_var,
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 9),
            anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(2, 15))

        actions = tk.Frame(header, bg=colors["surface"], padx=18)
        actions.grid(row=0, column=2, rowspan=2, sticky="e")
        self.join_button = self._make_action_button(
            actions, "Unirme", self._join_selected_group
        )
        self.join_button.pack(side="left", padx=(0, 6))
        self.leave_button = self._make_action_button(
            actions, "Salir", self._leave_selected_group, secondary=True
        )
        self.leave_button.pack(side="left")

        messages = tk.Frame(self.center, bg=colors["background"], padx=22, pady=18)
        messages.grid(row=1, column=0, sticky="nsew")
        messages.rowconfigure(0, weight=1)
        messages.columnconfigure(0, weight=1)

        self.message_canvas = tk.Canvas(
            messages,
            bg=colors["background"],
            bd=0,
            highlightthickness=0,
        )
        self.message_canvas.grid(row=0, column=0, sticky="nsew")
        message_scroll = tk.Scrollbar(
            messages,
            orient="vertical",
            command=self.message_canvas.yview,
            bd=0,
            relief="flat",
        )
        message_scroll.grid(row=0, column=1, sticky="ns")
        self.message_canvas.configure(yscrollcommand=message_scroll.set)
        self.message_inner = tk.Frame(self.message_canvas, bg=colors["background"])
        self.message_window = self.message_canvas.create_window(
            (0, 0),
            window=self.message_inner,
            anchor="nw",
        )
        self.message_inner.bind(
            "<Configure>",
            lambda _event: self.message_canvas.configure(
                scrollregion=self.message_canvas.bbox("all")
            ),
        )
        self.message_canvas.bind(
            "<Configure>",
            lambda event: self.message_canvas.itemconfigure(
                self.message_window,
                width=event.width,
            ),
        )

        composer = tk.Frame(
            self.center,
            bg=colors["surface"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            padx=18,
            pady=13,
        )
        composer.grid(row=2, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        self.composer_target_var = tk.StringVar(value="Selecciona una conversación")
        tk.Label(
            composer,
            textvariable=self.composer_target_var,
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.composer_entry = tk.Entry(
            composer,
            bg="#f7f9fc",
            fg=colors["text"],
            insertbackground=colors["text"],
            relief="flat",
            highlightbackground=colors["border"],
            highlightcolor=colors["accent"],
            highlightthickness=1,
            font=("TkDefaultFont", 10),
        )
        self.composer_entry.grid(row=1, column=0, sticky="ew", ipady=9, padx=(0, 10))
        self.composer_entry.bind("<Return>", self._send_current_message)
        self.send_button = self._make_action_button(
            composer,
            "Enviar",
            self._send_current_message,
        )
        self.send_button.grid(row=1, column=1, sticky="e", ipadx=10, ipady=4)

    def _build_participants(self) -> None:
        colors = self.COLORS
        self.participants = tk.Frame(
            self.root,
            bg=colors["surface"],
            width=225,
            highlightbackground=colors["border"],
            highlightthickness=1,
            padx=16,
            pady=18,
        )
        self.participants.grid(row=0, column=2, sticky="nsew")
        self.participants.grid_propagate(False)
        self.participants.rowconfigure(2, weight=1)

        self.participant_title_var = tk.StringVar(value="MIEMBROS")
        tk.Label(
            self.participants,
            textvariable=self.participant_title_var,
            bg=colors["surface"],
            fg=colors["text"],
            font=("TkDefaultFont", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        self.participant_hint_var = tk.StringVar(value="Selecciona un grupo para ver sus miembros")
        tk.Label(
            self.participants,
            textvariable=self.participant_hint_var,
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 8),
            justify="left",
            wraplength=185,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 12))

        member_box = tk.Frame(self.participants, bg=colors["surface"])
        member_box.grid(row=2, column=0, sticky="nsew")
        member_box.rowconfigure(0, weight=1)
        member_box.columnconfigure(0, weight=1)
        self.member_listbox = tk.Listbox(
            member_box,
            bg=colors["surface"],
            fg=colors["text"],
            selectbackground="#e5effd",
            selectforeground=colors["accent_dark"],
            activestyle="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("TkDefaultFont", 9),
        )
        self.member_listbox.grid(row=0, column=0, sticky="nsew")
        member_scroll = tk.Scrollbar(
            member_box,
            orient="vertical",
            command=self.member_listbox.yview,
            bd=0,
            relief="flat",
        )
        member_scroll.grid(row=0, column=1, sticky="ns")
        self.member_listbox.configure(yscrollcommand=member_scroll.set)
        self.member_listbox.bind("<<ListboxSelect>>", self._on_member_selected)

        self.participant_footer = tk.Label(
            self.participants,
            text="Los listados son opcionales en el protocolo actual.",
            bg=colors["surface"],
            fg=colors["muted"],
            font=("TkDefaultFont", 8),
            justify="left",
            wraplength=185,
            anchor="w",
        )
        self.participant_footer.grid(row=3, column=0, sticky="ew", pady=(16, 0))

        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

    def _make_sidebar_listbox(self, parent: tk.Widget, height: int) -> tk.Listbox:
        colors = self.COLORS
        return tk.Listbox(
            parent,
            height=height,
            bg=colors["sidebar"],
            fg=colors["sidebar_text"],
            selectbackground=colors["accent"],
            selectforeground="white",
            activestyle="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            exportselection=False,
            font=("TkDefaultFont", 10),
        )

    def _make_sidebar_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        width: int = 10,
    ) -> tk.Button:
        colors = self.COLORS
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=colors["sidebar_hover"],
            fg=colors["sidebar_text"],
            activebackground=colors["accent"],
            activeforeground="white",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("TkDefaultFont", 9, "bold"),
            cursor="hand2",
        )

    def _make_action_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        *,
        secondary: bool = False,
    ) -> tk.Button:
        colors = self.COLORS
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#eef4fc" if secondary else colors["accent"],
            fg=colors["accent_dark"] if secondary else "white",
            activebackground="#dce9f9" if secondary else colors["accent_dark"],
            activeforeground=colors["accent_dark"] if secondary else "white",
            disabledforeground="#9aa5b3",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("TkDefaultFont", 9, "bold"),
            cursor="hand2",
        )

    def _poll_events(self) -> None:
        if self._closing:
            return

        while True:
            try:
                event = self.client.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)

        self.root.after(100, self._poll_events)

    def _handle_event(self, event: JsonObject) -> None:
        status = self.session.handle_event(event)

        if status is not None:
            self.connection_status_var.set(status)

        # Un evento puede cambiar directorios, mensajes, selección no leída o
        # el estado de los controles. La vista se actualiza en un solo punto.
        self._refresh_group_list()
        self._refresh_people_list()
        self._refresh_members()
        self._render_messages()
        self._update_controls()

    def _refresh_group_list(self) -> None:
        self._group_order = list(self.session.groups)
        self.group_listbox.delete(0, tk.END)
        if not self._group_order:
            self.group_listbox.insert(tk.END, "Esperando GROUP_LIST...")
            self.group_listbox.itemconfig(0, fg=self.COLORS["sidebar_muted"])
            return

        for group_id in self._group_order:
            label = self.session.groups[group_id]
            marker = "  •" if ("group", group_id) in self.session.unread else ""
            self.group_listbox.insert(tk.END, f"#  {label}{marker}")

        if self.session.selected and self.session.selected[0] == "group":
            self._select_listbox_item(
                self.group_listbox,
                self._group_order,
                self.session.selected[1],
            )

    def _refresh_people_list(self) -> None:
        self._people_order = [
            user_id
            for user_id in self.session.users
            if user_id != self.client.user_id
        ]
        self.people_listbox.delete(0, tk.END)
        if not self._people_order:
            self.people_listbox.insert(tk.END, "Esperando USERS_LIST...")
            self.people_listbox.itemconfig(0, fg=self.COLORS["sidebar_muted"])
            return

        for user_id in self._people_order:
            label = self.session.users[user_id]
            marker = "  •" if ("private", user_id) in self.session.unread else ""
            self.people_listbox.insert(tk.END, f"●  {label}{marker}")

        if self.session.selected and self.session.selected[0] == "private":
            self._select_listbox_item(
                self.people_listbox,
                self._people_order,
                self.session.selected[1],
            )

    @staticmethod
    def _select_listbox_item(
        listbox: tk.Listbox,
        ordered_ids: list[str],
        selected_id: str,
    ) -> None:
        if selected_id in ordered_ids:
            index = ordered_ids.index(selected_id)
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(index)
            listbox.see(index)

    def _refresh_members(self) -> None:
        self.member_listbox.delete(0, tk.END)
        self._member_order = []

        if self.session.selected and self.session.selected[0] == "group":
            group_id = self.session.selected[1]
            self.participant_title_var.set("MIEMBROS")
            self.participant_hint_var.set(
                f"Integrantes de {self.session.groups.get(group_id, group_id)}"
            )
            members = self.session.members_by_group.get(group_id)
            if members is None:
                self.member_listbox.insert(
                    tk.END, "Esperando GROUP_MEMBERS..."
                )
                self.member_listbox.itemconfig(
                    0, fg=self.COLORS["muted"]
                )
                return
            if not members:
                self.member_listbox.insert(tk.END, "Sin miembros publicados")
                self.member_listbox.itemconfig(0, fg=self.COLORS["muted"])
                return
            for user_id, label in members:
                self._member_order.append(user_id)
                self.member_listbox.insert(tk.END, f"●  {label}")
            return

        if self.session.selected and self.session.selected[0] == "private":
            user_id = self.session.selected[1]
            self.participant_title_var.set("PERSONA")
            self.participant_hint_var.set(
                "Haz clic en personas de la barra lateral para cambiar de conversación."
            )
            self._member_order = [user_id]
            self.member_listbox.insert(
                tk.END, f"●  {self.session.users.get(user_id, user_id)}"
            )
            return

        self.participant_title_var.set("MIEMBROS")
        self.participant_hint_var.set(
            "Selecciona un grupo para ver sus miembros."
        )
        self.member_listbox.insert(tk.END, "Aún no hay una conversación")
        self.member_listbox.itemconfig(0, fg=self.COLORS["muted"])

    def _on_group_selected(self, _event: tk.Event) -> None:
        selection = self.group_listbox.curselection()
        if not selection or selection[0] >= len(self._group_order):
            return
        self._select_group(self._group_order[selection[0]])

    def _on_person_selected(self, _event: tk.Event) -> None:
        selection = self.people_listbox.curselection()
        if not selection or selection[0] >= len(self._people_order):
            return
        self._select_private(self._people_order[selection[0]])

    def _on_member_selected(self, _event: tk.Event) -> None:
        if not self.session.selected or self.session.selected[0] != "group":
            return
        selection = self.member_listbox.curselection()
        if not selection or selection[0] >= len(self._member_order):
            return
        self._select_private(self._member_order[selection[0]])

    def _select_group(self, group_id: str) -> None:
        if not self.session.select_group(group_id):
            return
        self._refresh_group_list()
        self._render_selected_conversation()

    def _select_private(self, user_id: str) -> None:
        if not self.session.select_private(user_id):
            return
        self._refresh_people_list()
        self._render_selected_conversation()

    def _render_selected_conversation(self) -> None:
        self._update_header()
        self._refresh_members()
        self._render_messages()
        self._update_controls()

    def _update_header(self) -> None:
        if self.session.selected is None:
            self.header_avatar.configure(text="·", bg="#e7edf6")
            self.header_title_var.set("Selecciona un grupo o persona")
            self.header_subtitle_var.set(
                "La conversación aparecerá en este espacio"
            )
            self.composer_target_var.set("Selecciona una conversación")
            return

        kind, identifier = self.session.selected
        if kind == "group":
            self.header_avatar.configure(text="#", bg="#e6f0ff")
            self.header_title_var.set(self.session.groups.get(identifier, identifier))
            self.header_subtitle_var.set(
                f"Grupo · {identifier} · elige un miembro para escribirle en privado"
            )
            self.composer_target_var.set(
                f"Mensaje para #{self.session.groups.get(identifier, identifier)}"
            )
        else:
            label = self.session.users.get(identifier, identifier)
            self.header_avatar.configure(
                text=label[:1].upper(),
                bg="#e8f7ef",
            )
            self.header_title_var.set(label)
            self.header_subtitle_var.set(
                f"Conversación privada · {identifier}"
            )
            self.composer_target_var.set(f"Mensaje privado para {label}")

    def _render_messages(self) -> None:
        for child in self.message_inner.winfo_children():
            child.destroy()

        if self.session.selected is None:
            self._add_empty_conversation(
                "Tu chat está listo",
                "Selecciona una persona o un grupo desde las barras laterales.",
            )
            return

        messages = self.session.messages_for(self.session.selected)
        if not messages:
            title = (
                "Aún no hay mensajes en este grupo"
                if self.session.selected[0] == "group"
                else "Aún no hay mensajes con esta persona"
            )
            self._add_empty_conversation(
                title,
                "Los mensajes recibidos y enviados aparecerán aquí.",
            )
            return

        for message in messages:
            self._add_message_bubble(message)

        self.message_inner.update_idletasks()
        self.message_canvas.configure(
            scrollregion=self.message_canvas.bbox("all")
        )
        self.message_canvas.yview_moveto(1.0)

    def _add_empty_conversation(self, title: str, detail: str) -> None:
        empty = tk.Frame(self.message_inner, bg=self.COLORS["background"])
        empty.pack(expand=True, fill="both", pady=120)
        tk.Label(
            empty,
            text=title,
            bg=self.COLORS["background"],
            fg=self.COLORS["text"],
            font=("TkDefaultFont", 12, "bold"),
        ).pack()
        tk.Label(
            empty,
            text=detail,
            bg=self.COLORS["background"],
            fg=self.COLORS["muted"],
            font=("TkDefaultFont", 9),
        ).pack(pady=(7, 0))

    def _add_message_bubble(self, message: JsonObject) -> None:
        direction = message["direction"]
        row = tk.Frame(self.message_inner, bg=self.COLORS["background"])
        row.pack(fill="x", pady=(0, 10))

        if direction == "system":
            label = tk.Label(
                row,
                text=message["text"],
                bg=self.COLORS["system"],
                fg=self.COLORS["muted"],
                font=("TkDefaultFont", 8),
                padx=12,
                pady=6,
                wraplength=430,
            )
            label.pack(anchor="center")
            return

        outgoing = direction == "outgoing"
        failed = bool(message.get("failed"))
        bubble = tk.Frame(
            row,
            bg=self.COLORS["bubble_out"] if outgoing else self.COLORS["bubble_in"],
            padx=13,
            pady=8,
            highlightbackground=(
                self.COLORS["danger"]
                if failed
                else ("#c8e1fb" if outgoing else self.COLORS["border"])
            ),
            highlightthickness=1,
        )
        bubble.pack(
            anchor="e" if outgoing else "w",
            padx=(90 if outgoing else 0, 0 if outgoing else 90),
        )
        tk.Label(
            bubble,
            text=message.get("sender", "Tú" if outgoing else ""),
            bg=bubble["bg"],
            fg=self.COLORS["accent_dark"] if outgoing else self.COLORS["muted"],
            font=("TkDefaultFont", 8, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            bubble,
            text=message["text"],
            bg=bubble["bg"],
            fg=self.COLORS["text"],
            font=("TkDefaultFont", 10),
            justify="left",
            anchor="w",
            wraplength=430,
        ).pack(anchor="w", pady=(3, 0))

        if failed:
            tk.Label(
                bubble,
                text="⚠ El servidor rechazó este mensaje",
                bg=bubble["bg"],
                fg=self.COLORS["danger"],
                font=("TkDefaultFont", 8, "bold"),
                anchor="w",
            ).pack(anchor="w", pady=(4, 0))

    def _add_system_to_current(self, text: str) -> None:
        self.session.add_system_to_current(text)
        if self.session.selected is not None:
            self._render_messages()
        else:
            self.connection_status_var.set(str(text))

    def _add_group_from_dialog(self) -> None:
        if not self.session.is_connected:
            self.connection_status_var.set(
                "Conecta primero para unirte a un grupo"
            )
            return
        group_id = simpledialog.askstring(
            "Agregar grupo",
            "Escribe el groupId que debe usar el servidor:",
            parent=self.root,
        )
        if not group_id or not group_id.strip():
            return
        group_id = group_id.strip()
        self.session.add_manual_group(group_id)
        self._select_group(group_id)
        self._request_group_action("GROUP_JOIN", group_id)

    def _join_selected_group(self) -> None:
        if self.session.selected and self.session.selected[0] == "group":
            self._request_group_action("GROUP_JOIN", self.session.selected[1])

    def _leave_selected_group(self) -> None:
        if self.session.selected and self.session.selected[0] == "group":
            self._request_group_action("GROUP_LEAVE", self.session.selected[1])

    def _request_group_action(self, operation: str, group_id: str) -> None:
        try:
            self.session.request_group_action(operation, group_id)
        except (ConnectionError, OSError) as exc:
            self._add_system_to_current(f"No se pudo enviar {operation}: {exc}")
            return

        self.connection_status_var.set(
            f"{operation} enviado para {group_id}"
        )

    def _send_current_message(self, _event: tk.Event | None = None) -> str:
        if not self.session.is_connected or self.session.selected is None:
            self.connection_status_var.set(
                "Selecciona una conversación después de conectar"
            )
            return "break"

        text = self.composer_entry.get()
        if not text.strip():
            return "break"

        kind, identifier = self.session.selected
        try:
            self.session.send_message(kind, identifier, text)
        except (ConnectionError, OSError, ValueError) as exc:
            self._add_system_to_current(f"No se pudo enviar el mensaje: {exc}")
            return "break"

        self.composer_entry.delete(0, tk.END)
        return "break"

    def _update_controls(self) -> None:
        connected = self.session.is_connected
        selected_group = (
            self.session.selected is not None
            and self.session.selected[0] == "group"
        )
        can_send = connected and self.session.selected is not None
        self.add_group_button.configure(
            state=tk.NORMAL if connected else tk.DISABLED
        )
        self.join_button.configure(
            state=tk.NORMAL if connected and selected_group else tk.DISABLED
        )
        self.leave_button.configure(
            state=tk.NORMAL if connected and selected_group else tk.DISABLED
        )
        self.send_button.configure(
            state=tk.NORMAL if can_send else tk.DISABLED
        )
        self.composer_entry.configure(
            state=tk.NORMAL if can_send else tk.DISABLED
        )

    def _on_close(self) -> None:
        self._closing = True
        self.client.close()
        self.root.destroy()
