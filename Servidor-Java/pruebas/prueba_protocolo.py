"""Prueba de extremo a extremo del servidor de chat contra sockets reales."""
import json
import socket
import time

HOST, PORT = "127.0.0.1", 1803
fallos = []


class Cliente:
    def __init__(self, nombre):
        self.nombre = nombre
        self.sock = socket.create_connection((HOST, PORT), timeout=3)
        self.buf = b""

    def enviar(self, **campos):
        self.sock.sendall((json.dumps(campos) + "\n").encode())

    def enviar_crudo(self, texto):
        self.sock.sendall((texto + "\n").encode())

    def recibir(self, espera=1.0):
        """Devuelve todos los mensajes disponibles tras un breve reposo."""
        time.sleep(espera)
        self.sock.setblocking(False)
        try:
            while True:
                trozo = self.sock.recv(65536)
                if not trozo:
                    break
                self.buf += trozo
        except BlockingIOError:
            pass
        except OSError:
            pass
        self.sock.setblocking(True)

        mensajes = []
        while b"\n" in self.buf:
            linea, self.buf = self.buf.split(b"\n", 1)
            if linea.strip():
                mensajes.append(json.loads(linea))
        return mensajes

    def tipos(self, espera=1.0):
        return [m["type"] for m in self.recibir(espera)]

    def cerrar(self):
        self.sock.close()


def comprobar(descripcion, condicion, detalle=""):
    if condicion:
        print(f"  OK   {descripcion}")
    else:
        print(f"  FALLA {descripcion}  {detalle}")
        fallos.append(descripcion)


print("\n1. CONNECT y directorios")
ana = Cliente("ana")
ana.enviar(type="CONNECT", requestId="a1", userId="ana")
recibidos = ana.recibir()
tipos = [m["type"] for m in recibidos]
comprobar("ana recibe CONNECTED", "CONNECTED" in tipos, tipos)
comprobar("ana recibe USERS_LIST", "USERS_LIST" in tipos, tipos)
comprobar("ana recibe GROUP_LIST", "GROUP_LIST" in tipos, tipos)

bob = Cliente("bob")
bob.enviar(type="CONNECT", requestId="b1", userId="bob")
bob.recibir()
usuarios = [m for m in ana.recibir() if m["type"] == "USERS_LIST"]
comprobar("ana se entera de que bob entro", usuarios
          and {u["userId"] for u in usuarios[-1]["users"]} == {"ana", "bob"},
          usuarios[-1] if usuarios else "sin USERS_LIST")

print("\n2. Mensaje privado")
ana.enviar(type="PRIVATE_SEND", requestId="a2", to="bob", text="Hola Bob")
comprobar("ana recibe ACK", "ACK" in ana.tipos())
recibidos = bob.recibir()
privados = [m for m in recibidos if m["type"] == "PRIVATE_MESSAGE"]
comprobar("bob recibe el privado", privados and privados[0]["text"] == "Hola Bob",
          recibidos)
comprobar("el privado trae messageId", privados and "messageId" in privados[0])

print("\n3. Destinatario ausente")
ana.enviar(type="PRIVATE_SEND", requestId="a3", to="fantasma", text="hola")
errores = [m for m in ana.recibir() if m["type"] == "ERROR"]
comprobar("usuario inexistente da USER_NOT_FOUND",
          errores and errores[0]["code"] == "USER_NOT_FOUND", errores)
comprobar("no llega ACK junto al ERROR", len(errores) == 1, errores)

print("\n4. Grupos")
for cliente, rid in ((ana, "a4"), (bob, "b4")):
    cliente.enviar(type="GROUP_JOIN", requestId=rid, groupId="distribuidos")
ana.recibir()
bob.recibir()

ana.enviar(type="GROUP_SEND", requestId="a5", groupId="distribuidos", text="Hola grupo")
msg_ana = [m for m in ana.recibir() if m["type"] == "GROUP_MESSAGE"]
msg_bob = [m for m in bob.recibir() if m["type"] == "GROUP_MESSAGE"]
comprobar("el remitente tambien recibe el grupal", bool(msg_ana), msg_ana)
comprobar("bob recibe el grupal", msg_bob and msg_bob[0]["text"] == "Hola grupo", msg_bob)

bob.enviar(type="GROUP_LEAVE", requestId="b5", groupId="distribuidos")
bob.recibir()
ana.enviar(type="GROUP_SEND", requestId="a6", groupId="distribuidos", text="solo yo")
ana.recibir()
comprobar("quien salio no recibe mas del grupo",
          not [m for m in bob.recibir() if m["type"] == "GROUP_MESSAGE"])

bob.enviar(type="GROUP_SEND", requestId="b6", groupId="distribuidos", text="intruso")
errores = [m for m in bob.recibir() if m["type"] == "ERROR"]
comprobar("enviar a un grupo ajeno da NOT_GROUP_MEMBER",
          errores and errores[0]["code"] == "NOT_GROUP_MEMBER", errores)

print("\n5. Sesion unica")
impostor = Cliente("impostor")
impostor.enviar(type="CONNECT", requestId="i1", userId="ana")
recibidos = impostor.recibir()
comprobar("la conexion nueva se rechaza",
          recibidos and recibidos[0].get("code") == "USER_ALREADY_CONNECTED", recibidos)
time.sleep(0.5)
impostor.sock.setblocking(False)
try:
    cerrado = impostor.sock.recv(1) == b""
except BlockingIOError:
    cerrado = False
except OSError:
    cerrado = True
comprobar("el servidor cierra la conexion rechazada", cerrado)
ana.enviar(type="PRIVATE_SEND", requestId="a7", to="bob", text="sigo viva")
comprobar("la sesion original sobrevive", "ACK" in ana.tipos())
impostor.cerrar()

print("\n6. Validaciones")
bob.enviar_crudo("esto no es json")
errores = [m for m in bob.recibir() if m["type"] == "ERROR"]
comprobar("JSON invalido da INVALID_JSON",
          errores and errores[0]["code"] == "INVALID_JSON", errores)
comprobar("INVALID_JSON no inventa requestId",
          errores and "requestId" not in errores[0], errores)

bob.enviar(type="PRIVATE_SEND", requestId="b7", text="sin destinatario")
errores = [m for m in bob.recibir() if m["type"] == "ERROR"]
comprobar("faltan campos da INVALID_MESSAGE",
          errores and errores[0]["code"] == "INVALID_MESSAGE", errores)

bob.enviar(type="PRIVATE_SEND", requestId="b7", to="ana", text="repetido")
errores = [m for m in bob.recibir() if m["type"] == "ERROR"]
comprobar("requestId repetido da DUPLICATE_REQUEST",
          errores and errores[0]["code"] == "DUPLICATE_REQUEST", errores)

sin_saludar = Cliente("mudo")
sin_saludar.enviar(type="PRIVATE_SEND", requestId="x1", to="ana", text="hola")
errores = [m for m in sin_saludar.recibir() if m["type"] == "ERROR"]
comprobar("operar sin CONNECT da NOT_CONNECTED",
          errores and errores[0]["code"] == "NOT_CONNECTED", errores)
sin_saludar.cerrar()

print("\n7. Orden de mensajes (el punto del ejercicio)")
ana.recibir()
bob.recibir()
lote = "".join(
    json.dumps({"type": "PRIVATE_SEND", "requestId": f"orden{i}",
                "to": "bob", "text": f"mensaje-{i}"}) + "\n"
    for i in range(40)
)
ana.sock.sendall(lote.encode())   # los 40 salen juntos, en pocas lecturas TCP
recibidos = [m for m in bob.recibir(2.5) if m["type"] == "PRIVATE_MESSAGE"]
textos = [m["text"] for m in recibidos]
esperado = [f"mensaje-{i}" for i in range(40)]
comprobar(f"llegan los 40 mensajes (llegaron {len(textos)})", textos == esperado,
          textos[:6] if textos != esperado else "")

print("\n8. Texto con saltos de linea y comillas")
raro = 'linea1\nlinea2 "con comillas" y \\barra'
ana.enviar(type="PRIVATE_SEND", requestId="a99", to="bob", text=raro)
recibidos = [m for m in bob.recibir() if m["type"] == "PRIVATE_MESSAGE"]
comprobar("el texto viaja intacto", recibidos and recibidos[-1]["text"] == raro,
          repr(recibidos[-1]["text"]) if recibidos else "nada")

print("\n9. Desconexion")
bob.cerrar()
time.sleep(0.8)
ana.recibir()
ana.enviar(type="PRIVATE_SEND", requestId="a100", to="bob", text="sigues ahi?")
errores = [m for m in ana.recibir() if m["type"] == "ERROR"]
comprobar("un usuario que se fue da USER_DISCONNECTED",
          errores and errores[0]["code"] == "USER_DISCONNECTED", errores)

ana.cerrar()

print("\n" + "=" * 55)
if fallos:
    print(f"FALLARON {len(fallos)} comprobaciones:")
    for f in fallos:
        print(f"  - {f}")
else:
    print("Todas las comprobaciones pasaron.")
print("=" * 55)
