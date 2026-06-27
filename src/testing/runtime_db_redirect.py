from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCTION_RUNTIME_DBS: dict[str, Path] = {
    "paper_trading": (REPO_ROOT / "data" / "paper_trading.db").resolve(),
    "trader_signals": (REPO_ROOT / "data" / "trader_signals.db").resolve(),
    "traders": (REPO_ROOT / "data" / "traders.db").resolve(),
    "wallet_activity": (REPO_ROOT / "data" / "wallet_activity.db").resolve(),
}

PRODUCTION_RUNTIME_DB_BY_NAME: dict[str, Path] = {
    path.name: path for path in PRODUCTION_RUNTIME_DBS.values()
}

RedirectResolver = Callable[[str | Path], str | Path]

_redirect_resolver: RedirectResolver | None = None


def install_runtime_db_redirect(resolver: RedirectResolver | None) -> None:
    global _redirect_resolver
    _redirect_resolver = resolver


def runtime_db_redirect_active() -> bool:
    return _redirect_resolver is not None


def canonical_production_runtime_path(db_path: str | Path) -> Path | None:
    if isinstance(db_path, str) and db_path.startswith("file:"):
        parsed = urlsplit(db_path)
        candidate = Path(unquote(parsed.path))
    else:
        candidate = Path(db_path)
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    else:
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = candidate.absolute()
    for production_path in PRODUCTION_RUNTIME_DBS.values():
        if candidate == production_path:
            return production_path
    return None


def redirected_target_path(db_path: str | Path) -> Path:
    """Resolve redirect target and return a concrete filesystem path."""
    target = resolve_runtime_db_target(db_path)
    if isinstance(target, str) and target.startswith("file:"):
        parsed = urlsplit(target)
        return Path(unquote(parsed.path))
    return Path(target)


def assert_runtime_db_redirect_applied(db_path: str | Path) -> None:
    """Fail fast when a production runtime DB path was not redirected."""
    if _redirect_resolver is None:
        return
    production = canonical_production_runtime_path(db_path)
    if production is None:
        return
    target = redirected_target_path(db_path)
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target.absolute()
    if resolved_target == production:
        raise RuntimeError(f"Production runtime DB was not redirected: {db_path!r}")


def resolve_runtime_db_target(db_path: str | Path) -> str | Path:
    if _redirect_resolver is None:
        return db_path
    return _redirect_resolver(db_path)


def build_isolated_runtime_db_redirect(isolated_paths: dict[Path, Path]) -> RedirectResolver:
    def _redirect(db_path: str | Path) -> str | Path:
        if isinstance(db_path, str) and db_path.startswith("file:"):
            parsed = urlsplit(db_path)
            candidate = Path(unquote(parsed.path))
            if not candidate.is_absolute():
                candidate = (REPO_ROOT / candidate).resolve()
            else:
                candidate = candidate.resolve()
            production = canonical_production_runtime_path(candidate)
            if production is None:
                return db_path
            isolated = isolated_paths.get(production)
            if isolated is None:
                return db_path
            return urlunsplit((parsed.scheme, parsed.netloc, str(isolated), parsed.query, parsed.fragment))
        production = canonical_production_runtime_path(db_path)
        if production is None:
            return db_path
        isolated = isolated_paths.get(production)
        if isolated is None:
            return db_path
        return isolated

    return _redirect
