"""
Teclas en vivo (Windows): C = cobrar lo que va en positivo, X = cerrar todo, P = pausar entradas,
A = activar/desactivar recogida automatica. En otros sistemas no hay teclas (el bot funciona igual).
"""
import asyncio
import os
import threading


class KeyListener:
    def __init__(self):
        self.queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._thread = None

    def start(self):
        if os.name != "nt" or self._thread is not None:
            return
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._read, args=(loop,), daemon=True)
        self._thread.start()

    def _read(self, loop):
        try:
            import msvcrt
            while True:
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):      # tecla especial: descartar el segundo codigo
                    msvcrt.getwch()
                    continue
                loop.call_soon_threadsafe(self.queue.put_nowait, ch.lower())
        except Exception:
            return                                # sin consola interactiva: sin teclas

    def poll(self) -> list:
        keys = []
        while not self.queue.empty():
            keys.append(self.queue.get_nowait())
        return keys
