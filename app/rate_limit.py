"""
Limitador de peticiones sencillo en memoria (ventana deslizante), sin dependencias.

Es por proceso: con gunicorn de 2 workers el límite efectivo puede llegar a ser
el doble del configurado, lo cual sigue frenando la fuerza bruta y el spam.
Si en el futuro hay más workers o varios servidores, conviene pasar a
Flask-Limiter con Redis.
"""

import threading
import time
from collections import deque
from functools import wraps

from flask import jsonify, request

_lock = threading.Lock()
_hits: dict[str, deque] = {}
_last_cleanup = 0.0


def client_ip() -> str:
    """
    IP del visitante. Detrás de Coolify/Traefik o Railway, el proxy añade la IP
    real al final de X-Forwarded-For; los valores de la izquierda los puede
    inventar el cliente, así que se toma el último.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "unknown"


def _cleanup(now: float, max_window: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < 300:
        return
    _last_cleanup = now
    for key in list(_hits.keys()):
        q = _hits[key]
        while q and now - q[0] > max_window:
            q.popleft()
        if not q:
            del _hits[key]


def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """Registra un intento para `key` y devuelve True si supera el límite."""
    now = time.monotonic()
    with _lock:
        _cleanup(now, 24 * 3600)
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def rate_limit(scope: str, limit: int, window_seconds: int, message: str | None = None):
    """Decorador: limita `limit` peticiones por IP cada `window_seconds` en `scope`."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method != "OPTIONS" and is_rate_limited(
                f"{scope}:{client_ip()}", limit, window_seconds
            ):
                return (
                    jsonify(
                        {
                            "error": message
                            or "Has realizado demasiados intentos. Espera unos minutos y vuelve a probar."
                        }
                    ),
                    429,
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator
