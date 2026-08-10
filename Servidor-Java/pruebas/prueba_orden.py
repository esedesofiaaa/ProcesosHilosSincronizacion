"""Prueba de carga: varios remitentes en paralelo contra un receptor.

Verifica el invariante del diseno: los mensajes de un mismo remitente deben
llegar en su orden original aunque cuatro workers compitan por procesarlos.
"""
import json
import socket
import threading
import time

import sys
HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 1803
REMITENTES = 6
POR_REMITENTE = 60
GRUPO = "carga"


def conectar(user_id):
    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.sendall((json.dumps(
        {"type": "CONNECT", "requestId": f"{user_id}-c", "userId": user_id}) + "\n").encode())
    return sock


def unir_al_grupo(sock, user_id):
    sock.sendall((json.dumps(
        {"type": "GROUP_JOIN", "requestId": f"{user_id}-j", "groupId": GRUPO}) + "\n").encode())


receptor = conectar("receptor")
unir_al_grupo(receptor, "receptor")
time.sleep(0.5)

emisores = []
for i in range(REMITENTES):
    nombre = f"emisor{i}"
    sock = conectar(nombre)
    unir_al_grupo(sock, nombre)
    emisores.append((nombre, sock))
time.sleep(0.8)

arranque = threading.Barrier(REMITENTES + 1)


def rafaga(nombre, sock):
    """Envia una rafaga sin pausas; todos los hilos arrancan a la vez."""
    arranque.wait()
    for n in range(POR_REMITENTE):
        sock.sendall((json.dumps({
            "type": "GROUP_SEND",
            "requestId": f"{nombre}-{n}",
            "groupId": GRUPO,
            "text": f"{nombre}:{n}",
        }) + "\n").encode())


hilos = [threading.Thread(target=rafaga, args=(n, s)) for n, s in emisores]
for h in hilos:
    h.start()

inicio = time.time()
arranque.wait()
for h in hilos:
    h.join()

# Recolectar hasta que dejen de llegar mensajes.
esperados = REMITENTES * POR_REMITENTE
buf = b""
recibidos = []
receptor.settimeout(2.0)
try:
    while len(recibidos) < esperados:
        trozo = receptor.recv(65536)
        if not trozo:
            break
        buf += trozo
        while b"\n" in buf:
            linea, buf = buf.split(b"\n", 1)
            if not linea.strip():
                continue
            evento = json.loads(linea)
            if evento.get("type") == "GROUP_MESSAGE":
                recibidos.append(evento)
except socket.timeout:
    pass

transcurrido = time.time() - inicio

print(f"\nEnviados : {esperados} mensajes desde {REMITENTES} remitentes en paralelo")
print(f"Recibidos: {len(recibidos)}")
print(f"Tiempo   : {transcurrido:.2f} s")

fallos = []
if len(recibidos) != esperados:
    fallos.append(f"se perdieron {esperados - len(recibidos)} mensajes")

# Orden relativo por remitente: la secuencia de cada uno debe ser 0,1,2,...
por_remitente = {}
for evento in recibidos:
    nombre, indice = evento["text"].split(":")
    por_remitente.setdefault(nombre, []).append(int(indice))

print("\nOrden por remitente:")
for nombre in sorted(por_remitente):
    secuencia = por_remitente[nombre]
    correcto = secuencia == sorted(secuencia)
    completo = secuencia == list(range(POR_REMITENTE))
    estado = "en orden" if correcto else "DESORDENADO"
    print(f"  {nombre}: {len(secuencia):3d} mensajes, {estado}")
    if not correcto:
        primera = next(i for i in range(1, len(secuencia)) if secuencia[i] < secuencia[i - 1])
        print(f"      salto en la posicion {primera}: "
              f"...{secuencia[max(0, primera - 3):primera + 3]}...")
        fallos.append(f"{nombre} llego desordenado")
    elif not completo:
        fallos.append(f"{nombre} incompleto")

# Los messageId los asigna el servidor y no deben repetirse.
ids = [e["messageId"] for e in recibidos]
if len(ids) != len(set(ids)):
    fallos.append("hay messageId repetidos")

print("\n" + "=" * 55)
if fallos:
    print("FALLOS:")
    for f in fallos:
        print(f"  - {f}")
else:
    print(f"Los {esperados} mensajes llegaron completos y en orden por remitente.")
print("=" * 55)

receptor.close()
for _, s in emisores:
    s.close()
