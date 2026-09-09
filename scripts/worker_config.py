"""Persistent worker configuration for bionic-local."""
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MODEL = "qwen3.8-9b-distill"
MIN_MAX_TOKENS = 32768
DEFAULT_CONTEXT_WINDOW = 230000
DEFAULT_TTL_SECONDS = 900
DEFAULT_WORKERS = [
    {
        "id": "21",
        "name": "Bionic 21",
        "base_url": "http://192.168.88.21:1234/v1",
        "slots": 3,
        "model": "",
        "max_tokens": 0,
        "context_window": 0,
        "ttl": 0,
        "timeout": 0,
        "enabled": True,
    },
    {
        "id": "33",
        "name": "Bionic 33",
        "base_url": "http://192.168.88.33:1234/v1",
        "slots": 3,
        "model": "",
        "max_tokens": 0,
        "context_window": 0,
        "ttl": 0,
        "timeout": 0,
        "enabled": True,
    },
]


def default_config():
    return {
        "model": DEFAULT_MODEL,
        "max_tokens": MIN_MAX_TOKENS,
        "context_window": DEFAULT_CONTEXT_WINDOW,
        "ttl": DEFAULT_TTL_SECONDS,
        "timeout": 900,
        "workers": [dict(worker) for worker in DEFAULT_WORKERS],
    }


def private_http_v1(value):
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Worker URL must be plain HTTP without auth/query/fragment.")
    if parsed.path.rstrip("/") != "/v1":
        raise ValueError("Worker URL must end with /v1.")
    host = parsed.hostname
    if not host:
        raise ValueError("Worker URL must include a host.")
    allowed = host in {"localhost", "127.0.0.1", "::1"}
    if not allowed:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Worker host must be loopback or a private LAN IP.") from exc
        allowed = ip.is_private or ip.is_loopback
    if not allowed:
        raise ValueError("Worker host must be loopback or a private LAN IP.")
    if parsed.port is None:
        raise ValueError("Worker URL must include an explicit port.")
    return value.rstrip("/")


def normalize_config(config):
    if not isinstance(config, dict):
        raise ValueError("Config must be an object.")
    model = str(config.get("model") or DEFAULT_MODEL).strip()
    if not model:
        raise ValueError("Model must not be empty.")
    max_tokens = int(config.get("max_tokens") or MIN_MAX_TOKENS)
    context_window = int(config.get("context_window") or DEFAULT_CONTEXT_WINDOW)
    ttl = int(config.get("ttl") or DEFAULT_TTL_SECONDS)
    timeout = int(config.get("timeout") or 900)
    if max_tokens < MIN_MAX_TOKENS:
        max_tokens = MIN_MAX_TOKENS
    if context_window < 1:
        raise ValueError("Context window must be positive.")
    if ttl < 1:
        raise ValueError("TTL must be positive.")
    if timeout < 1:
        raise ValueError("Timeout must be positive.")
    workers = config.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ValueError("At least one worker is required.")
    seen = set()
    normalized = []
    for worker in workers:
        if not isinstance(worker, dict):
            raise ValueError("Each worker must be an object.")
        worker_id = str(worker.get("id") or "").strip()
        if not worker_id or any(ch.isspace() for ch in worker_id):
            raise ValueError("Worker id must be a nonempty token without spaces.")
        if worker_id in seen:
            raise ValueError("Duplicate worker id: " + worker_id)
        seen.add(worker_id)
        slots = int(worker.get("slots") or 0)
        if slots < 0 or slots > 8:
            raise ValueError("Worker slots must be between 0 and 8.")
        worker_model = str(worker.get("model") or "").strip()
        worker_max_tokens = int(worker.get("max_tokens") or 0)
        worker_context_window = int(worker.get("context_window") or 0)
        worker_ttl = int(worker.get("ttl") or 0)
        worker_timeout = int(worker.get("timeout") or 0)
        if worker_max_tokens < 0 or worker_context_window < 0 or worker_ttl < 0 or worker_timeout < 0:
            raise ValueError("Per-worker token budget, context window, TTL, and timeout must not be negative.")
        if worker_max_tokens and worker_max_tokens < MIN_MAX_TOKENS:
            worker_max_tokens = MIN_MAX_TOKENS
        normalized.append(
            {
                "id": worker_id,
                "name": str(worker.get("name") or worker_id).strip(),
                "base_url": private_http_v1(str(worker.get("base_url") or "").strip()),
                "slots": slots,
                "model": worker_model,
                "max_tokens": worker_max_tokens,
                "context_window": worker_context_window,
                "ttl": worker_ttl,
                "timeout": worker_timeout,
                "enabled": bool(worker.get("enabled")) and slots > 0,
            }
        )
    if not any(worker["enabled"] for worker in normalized):
        raise ValueError("At least one worker must be enabled with slots > 0.")
    return {
        "model": model,
        "max_tokens": max_tokens,
        "context_window": context_window,
        "ttl": ttl,
        "timeout": timeout,
        "workers": normalized,
    }


def config_path(home):
    return Path(home) / "config.json"


def load_config(home):
    path = config_path(home)
    if not path.exists():
        return default_config()
    try:
        return normalize_config(json.loads(path.read_text(encoding="utf-8-sig")))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        backup = path.with_suffix(path.suffix + ".bad")
        if backup.exists():
            backup.unlink()
        path.replace(backup)
        return default_config()


def save_config(home, config):
    normalized = normalize_config(config)
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        backup.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    tmp.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return normalized


def enabled_workers(config):
    return [worker for worker in config["workers"] if worker["enabled"] and worker["slots"] > 0]
