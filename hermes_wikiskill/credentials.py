"""Minimal, refreshed, key-less workspace profiles (D5, H4, R1-qa facts 45-49).

Upstream ``bootstrap_profile`` copied the WHOLE source ``.env``, ``auth.json`` and
``config.yaml`` once (messaging/bot tokens included, never refreshed, no chmod).
Here a workspace's Hermes home gets, on EVERY run:

- ``config.yaml``: ONLY ``model: {provider, default, base_url[, api_mode]}`` — no
  ``api_key``, no ``providers:``, no gateway/messaging sections, and no
  ``agent.disabled_toolsets`` (which could silently remove ``skills``, D17);
- ``.env`` (mode 0600): ONLY the provider's key variable (+ its base-url variable
  when the source has one), read from the source profile's ``.env`` (fallback:
  ``providers.<p>.api_key`` / ``model.api_key`` of the source config);
- no ``auth.json``.

Secret values are never returned in results, printed or logged.
"""

from __future__ import annotations

import json
import os

from . import mini_yaml
from .util import atomic_write_text

# provider -> (key env var(s), base-url env var); from Hermes' provider registry
# (hermes_cli/auth.py _REGISTRY_ROWS, qa-H82) for the providers seen on the host
# plus the common api-key providers.
PROVIDER_ENV: dict[str, tuple[tuple[str, ...], str | None]] = {
    "deepseek": (("DEEPSEEK_API_KEY",), "DEEPSEEK_BASE_URL"),
    "openrouter": (("OPENROUTER_API_KEY",), "OPENROUTER_BASE_URL"),
    "openai": (("OPENAI_API_KEY",), "OPENAI_BASE_URL"),
    "anthropic": (("ANTHROPIC_API_KEY",), "ANTHROPIC_BASE_URL"),
    "groq": (("GROQ_API_KEY",), "GROQ_BASE_URL"),
    "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "GEMINI_BASE_URL"),
    "google": (("GOOGLE_API_KEY", "GEMINI_API_KEY"), None),
    "xai": (("XAI_API_KEY",), "XAI_BASE_URL"),
    "mistral": (("MISTRAL_API_KEY",), "MISTRAL_BASE_URL"),
}

MODEL_KEYS = ("provider", "default", "base_url", "api_mode")


def source_home(source_profile: str | None = None) -> str:
    """The Hermes home credentials are read from (never written)."""
    override = os.environ.get("HWS_SOURCE_HOME")
    if override:
        return os.path.expanduser(override)
    name = source_profile or os.environ.get("HWS_SOURCE_PROFILE") or "default"
    root = os.path.expanduser("~/.hermes")
    return root if name == "default" else os.path.join(root, "profiles", name)


def read_dotenv(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        k, v = s.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def read_config(home: str) -> dict:
    try:
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as f:
            return mini_yaml.loads(f.read())
    except OSError:
        return {}


def env_names(provider: str | None) -> tuple[tuple[str, ...], str | None]:
    p = (provider or "").strip().lower()
    if p in PROVIDER_ENV:
        return PROVIDER_ENV[p]
    stem = "".join(c if c.isalnum() else "_" for c in p).upper() or "OPENROUTER"
    return ((f"{stem}_API_KEY",), f"{stem}_BASE_URL")


def plan_profile(src_home: str, *, model: str | None = None,
                 provider: str | None = None) -> dict:
    """Compute the minimal config + env for a workspace profile (no writes).

    Returns ``{"config": {...}, "env": {NAME: value}, "provider", "env_names",
    "missing": [...]}`` — callers must not print ``env`` values.
    """
    cfg = read_config(src_home)
    src_model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    if isinstance(cfg.get("model"), str):  # `model: some-id` short form
        src_model = {"default": cfg["model"]}
    new_model = {k: src_model[k] for k in MODEL_KEYS if src_model.get(k) not in (None, "")}
    if provider:
        new_model["provider"] = provider
    if model:
        new_model["default"] = model
    prov = new_model.get("provider") or "openrouter"
    keys, base_var = env_names(prov)
    dotenv = read_dotenv(os.path.join(src_home, ".env"))
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    prov_cfg = providers.get(prov) if isinstance(providers.get(prov), dict) else {}
    env: dict[str, str] = {}
    for k in keys:
        if dotenv.get(k):
            env[k] = dotenv[k]
            break
    if not env:
        fallback = prov_cfg.get("api_key") or (src_model.get("api_key") if
                                                src_model.get("provider", prov) == prov else None)
        if fallback:
            env[keys[0]] = str(fallback)
    if base_var and dotenv.get(base_var):
        env[base_var] = dotenv[base_var]
    return {"config": {"model": new_model}, "env": env, "provider": prov,
            "env_names": sorted(env), "missing": [] if env else [keys[0]]}


SECRET_KEYS = {"api_key", "access_token", "refresh_token", "token", "id_token", "secret",
               "client_secret", "password", "key", "bearer"}


def auth_json_issues(home: str, provider: str | None) -> list[str] | None:
    """Problems in ``<home>/auth.json`` (None when absent).

    Hermes itself (re)creates auth.json on the first chat with a fingerprint-only
    credential pool for the provider it resolved from .env (host receipt, session
    20260928_192604_b6d277) — that is fine. Flagged: provider/OAuth entries, pool
    entries for another provider, or any raw secret-looking field (names only, never values).
    """
    path = os.path.join(home, "auth.json")
    if not os.path.lexists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ["auth.json unreadable"]
    if not isinstance(data, dict):
        return ["auth.json is not an object"]
    issues = []
    if data.get("providers"):
        issues.append(f"auth.json providers entries: {sorted(data['providers'])}")
    allowed = (provider or "").lower()
    pool = data.get("credential_pool") if isinstance(data.get("credential_pool"), dict) else {}
    for name, entries in pool.items():
        if entries and name.lower() != allowed:
            issues.append(f"auth.json credential_pool holds provider {name!r}")

    def walk(obj, where):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in SECRET_KEYS and v not in (None, "", [], {}):
                    issues.append(f"auth.json field {where}{k} holds a secret")
                walk(v, f"{where}{k}.")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{where}{i}.")

    walk(data, "")
    return issues


def render_env(env: dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in sorted(env.items()))


def write_profile(home: str, plan: dict) -> bool:
    """Write config.yaml + .env (0600) + remove auth.json. Returns True if anything changed."""
    changed = False
    os.makedirs(home, exist_ok=True)
    targets = ((os.path.join(home, "config.yaml"), mini_yaml.dumps(plan["config"]), 0o644),
               (os.path.join(home, ".env"), render_env(plan["env"]), 0o600))
    for path, text, mode in targets:
        try:
            with open(path, encoding="utf-8") as f:
                same = f.read() == text
        except OSError:
            same = False
        if not same:
            atomic_write_text(path, text, mode=mode)
            changed = True
        elif (os.stat(path).st_mode & 0o777) != mode:
            os.chmod(path, mode)
            changed = True
    auth = os.path.join(home, "auth.json")
    if auth_json_issues(home, plan["provider"]):  # keep Hermes' own fingerprint-only pool
        os.remove(auth)
        changed = True
    return changed
