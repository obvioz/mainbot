import json
import math
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from app.volatility import get_static_next_dca_drop

try:
    import fcntl  # POSIX advisory file locks (Linux/macOS)
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

DATA_DIR = Path("data")
PORTFOLIO_PATH = DATA_DIR / "portfolio_state.json"
JOURNAL_PATH = DATA_DIR / "journal.json"

# Sidecar lock files: stable inode, never replaced (unlike the state files which
# are swapped via os.replace). flock() is taken on these for cross-process
# mutual exclusion.
PORTFOLIO_LOCK_PATH = DATA_DIR / "portfolio_state.json.lock"
JOURNAL_LOCK_PATH = DATA_DIR / "journal.json.lock"

# portfolio_state.json is read-modify-written by many modules (monitor,
# bybit_sync, futures_lab, rotation_lab) so every transaction serializes on one
# re-entrant lock. journal.json has its own lock (append-only).
_PORTFOLIO_LOCK = threading.RLock()
_JOURNAL_LOCK = threading.RLock()


@contextmanager
def file_transaction(threading_lock: "threading.RLock", lock_path: Path) -> Iterator[None]:
    """Cross-process + cross-thread write critical section.

    Layering (outer → inner):
      1. fcntl.flock(LOCK_EX) on a stable sidecar lock file — serializes across
         *processes* (and, since each transaction opens its own fd, across
         threads too: separate open file descriptions on the same file conflict).
      2. the in-process ``threading.RLock`` — serializes across threads/coroutines
         within this process.

    Both are released on exit, even if the body raises. The lock file is created
    if missing. A fresh fd is opened per transaction so that one thread's
    LOCK_UN can never release a lock another thread still relies on.

    Only WRITE paths use this. Reads rely on os.replace() atomicity and take just
    the threading lock, so they can nest inside a transaction without deadlocking.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    if _HAS_FCNTL:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with threading_lock:
            yield
    finally:
        if fd != -1:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class StateCorruptionError(Exception):
    """Raised when a state file AND its .bak backup are both unreadable.

    We raise instead of returning an empty default so that a corrupt read can
    never silently overwrite real portfolio/journal data with a blank stub.
    """


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _log_backup_recovered(path: Path, exc: Exception) -> None:
    """Warn (non-fatal) that the primary file was corrupt and .bak was used."""
    try:
        from app.system_log import add_system_event
        add_system_event(
            "STATE_RECOVERY",
            f"Восстановление {path.name} из .bak",
            data={"file": str(path), "error": str(exc)},
            text=(
                f"⚠️ Основной файл {path.name} повреждён ({exc}). "
                f"Загружена резервная копия {_backup_path(path).name}."
            ),
        )
    except Exception:
        pass


def _log_corruption_critical(path: Path, exc: Exception, bak_exc: Exception | None) -> None:
    """Record an unrecoverable corruption as a critical error."""
    try:
        from app.error_log import log_error
        log_error(
            "storage.read",
            exc,
            context={
                "file": str(path),
                "backup_error": str(bak_exc) if bak_exc is not None else "no_backup",
                "severity": "CRITICAL",
                "note": "state file and backup both unreadable — state NOT reset",
            },
        )
    except Exception:
        pass


def _read_json(path: Path, default: Any) -> Any:
    """Resilient JSON read.

    - Missing file → return ``default`` (normal first-run case).
    - Corrupt file → try ``<path>.bak``. If the backup loads, log a warning to
      the system log and return it. If the backup is also corrupt or absent,
      log a critical error and raise :class:`StateCorruptionError`. We never
      silently return an empty default over real-but-corrupt state.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        bak = _backup_path(path)
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
            except Exception as bak_exc:
                _log_corruption_critical(path, exc, bak_exc)
                raise StateCorruptionError(
                    f"{path.name} и резервная копия {bak.name} оба повреждены"
                ) from exc
            _log_backup_recovered(path, exc)
            return data
        _log_corruption_critical(path, exc, None)
        raise StateCorruptionError(
            f"{path.name} повреждён, резервной копии {bak.name} нет"
        ) from exc


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomically persist JSON.

    Backs up the existing (good) target to ``<path>.bak`` first, then writes to
    a temp file in the same directory and ``os.replace()``s it onto the target —
    an atomic rename, so a crash mid-write can never leave a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    # Backup the current file before overwriting. Best-effort: a failed backup
    # must never block the write itself.
    if path.exists():
        try:
            shutil.copy2(path, _backup_path(path))
        except Exception:
            pass

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Backwards-compatible alias.
_write_json = _atomic_write_json


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_portfolio() -> dict:
    with _PORTFOLIO_LOCK:
        return _read_json(PORTFOLIO_PATH, {"positions": {}, "alerts": {}})


def save_portfolio(state: dict) -> None:
    with file_transaction(_PORTFOLIO_LOCK, PORTFOLIO_LOCK_PATH):
        _atomic_write_json(PORTFOLIO_PATH, state)


def update_portfolio(mutator: Callable[[dict], Any]) -> Any:
    """Atomic read-modify-write of portfolio_state.json.

    Reads the current state, applies ``mutator(state)`` in place, then writes it
    back — all while holding the global portfolio lock. This is the only safe
    way to update the portfolio: concurrent writers (monitor, bybit_sync,
    futures_lab, rotation_lab) can no longer clobber each other's keys because
    each transaction reads the freshest state before mutating it.

    Returns whatever ``mutator`` returns. If the mutator raises, nothing is
    written.
    """
    with file_transaction(_PORTFOLIO_LOCK, PORTFOLIO_LOCK_PATH):
        state = _read_json(PORTFOLIO_PATH, {"positions": {}, "alerts": {}})
        result = mutator(state)
        _atomic_write_json(PORTFOLIO_PATH, state)
        return result


def load_journal() -> list[dict]:
    with _JOURNAL_LOCK:
        return _read_json(JOURNAL_PATH, [])


def save_journal(rows: list[dict]) -> None:
    with file_transaction(_JOURNAL_LOCK, JOURNAL_LOCK_PATH):
        _atomic_write_json(JOURNAL_PATH, rows)


def add_journal(row: dict) -> None:
    with file_transaction(_JOURNAL_LOCK, JOURNAL_LOCK_PATH):
        rows = _read_json(JOURNAL_PATH, [])
        rows.append({"date": now_iso(), **row})
        _atomic_write_json(JOURNAL_PATH, rows)


def _normalize_old_position(pos: dict) -> dict:
    """Поддержка старых тестовых данных, если они были записаны в рублях."""
    if "invested_usdt" not in pos and "invested_rub" in pos:
        pos["invested_usdt"] = float(pos.get("invested_rub", 0))
    return pos


def record_buy(coin: str, amount_usdt: float, price: float, reason: str = "manual", dca_levels: list[float] | None = None) -> dict:
    coin = coin.upper().strip()
    amount_usdt = float(amount_usdt)
    price = float(price)

    # Санити против мусора (NaN/inf/<=0) — применяется и к ручным покупкам.
    # Примечание: `nan <= 0` это False, поэтому проверку конечности делаем явно.
    if not math.isfinite(amount_usdt) or amount_usdt <= 0:
        raise ValueError("Сумма покупки должна быть положительным числом USDT")
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Цена покупки должна быть положительным числом")

    qty = amount_usdt / price
    captured: dict = {}

    def _mut(state: dict) -> None:
        positions = state.setdefault("positions", {})
        pos = positions.get(coin, {
            "coin": coin,
            "qty": 0.0,
            "invested_usdt": 0.0,
            "avg_price": 0.0,
            "entry_count": 0,
            "last_entry_price": 0.0,
            "next_buy_price": 0.0,
            "take_profit_10_sent": False,
            "take_profit_15_sent": False,
            "entries": [],
        })
        pos = _normalize_old_position(pos)

        pos["qty"] = float(pos.get("qty", 0)) + qty
        pos["invested_usdt"] = float(pos.get("invested_usdt", 0)) + amount_usdt
        pos["avg_price"] = pos["invested_usdt"] / pos["qty"] if pos["qty"] else 0
        pos["tp1_price"] = round(pos["avg_price"] * 1.09, 8)
        pos["entry_count"] = int(pos.get("entry_count", 0)) + 1
        pos["last_entry_price"] = price

        # Единый источник DCA-уровней всей цепочки (первый вход, превью, доборы):
        # персональные ATR-уровни монеты из volatility_profile. Явно переданные
        # dca_levels (тесты/ручной override) имеют приоритет. Следующий добор
        # считается от фактической цены этой покупки (как и раньше по смыслу).
        if dca_levels:
            clean_levels = [float(x) for x in dca_levels][:4]
            pos["levels_source"] = pos.get("levels_source") or "explicit"
        else:
            from app.volatility_profile import get_entry_levels_for_coin
            clean_levels = [float(x) for x in get_entry_levels_for_coin(coin)["levels"]]
            pos["levels_source"] = "personal_atr"
        pos["dca_levels_used"] = clean_levels

        idx = int(pos.get("entry_count", 0))
        if clean_levels and idx < len(clean_levels):
            next_drop = float(clean_levels[idx])
        else:
            next_drop = get_static_next_dca_drop(coin, idx)

        pos["next_buy_drop_pct"] = next_drop
        pos["next_buy_price"] = round(price * (1 - next_drop / 100), 8)
        pos["take_profit_10_sent"] = False
        pos["take_profit_15_sent"] = False
        pos.setdefault("entries", []).append({
            "date": now_iso(),
            "price": price,
            "amount_usdt": amount_usdt,
            "qty": qty,
            "reason": reason,
        })

        # Удаляем старое рублевое поле, если оно осталось от прошлой версии.
        pos.pop("invested_rub", None)

        positions[coin] = pos
        captured["pos"] = pos

    update_portfolio(_mut)
    pos = captured["pos"]

    add_journal({
        "coin": coin,
        "action": "BUY",
        "price": price,
        "amount_usdt": amount_usdt,
        "qty": qty,
        "avg_price_after": pos["avg_price"],
        "reason": reason,
    })
    return pos


def migrate_positions_to_personal_levels() -> int:
    """Перевести лесенку доборов открытых позиций на персональные ATR-уровни.

    Обновляет ТОЛЬКО dca_levels_used / next_buy_drop_pct / next_buy_price — то есть
    лесенку доборов, не трогая qty / invested_usdt / avg_price (деньги) и не
    затрагивая реконсиляцию продаж. Идемпотентно по флагу levels_source: уже
    мигрированные позиции пропускаются. Следующий уровень считается от последней
    фактической цены входа (last_entry_price), как и в record_buy.

    Возвращает число мигрированных позиций.
    """
    from app.volatility_profile import get_entry_levels_for_coin

    def _mut(state: dict) -> int:
        positions = state.get("positions") or {}
        migrated = 0
        for coin, pos in positions.items():
            if pos.get("levels_source") == "personal_atr":
                continue
            levels = [float(x) for x in get_entry_levels_for_coin(coin)["levels"]]
            pos["dca_levels_used"] = levels
            entry_count = int(pos.get("entry_count", 0) or 0)
            anchor = float(pos.get("last_entry_price") or pos.get("avg_price") or 0)
            if levels and entry_count < len(levels):
                next_drop = float(levels[entry_count])
            else:
                next_drop = get_static_next_dca_drop(coin, entry_count)
            pos["next_buy_drop_pct"] = next_drop
            if anchor > 0:
                pos["next_buy_price"] = round(anchor * (1 - next_drop / 100), 8)
            pos["levels_source"] = "personal_atr"
            migrated += 1
        return migrated

    return update_portfolio(_mut)


def record_sell(coin: str, price: float, amount_usdt: float | None = None, sell_all: bool = False) -> dict:
    coin = coin.upper().strip()
    price = float(price)

    if price <= 0:
        raise ValueError("Цена продажи должна быть больше 0")

    captured: dict = {}

    def _mut(state: dict) -> None:
        positions = state.setdefault("positions", {})
        if coin not in positions:
            raise ValueError(f"По {coin} нет открытой позиции")

        pos = _normalize_old_position(positions[coin])
        qty_total = float(pos.get("qty", 0))
        if qty_total <= 0:
            raise ValueError(f"По {coin} нет количества для продажи")

        current_value_usdt = qty_total * price

        if sell_all or amount_usdt is None:
            qty_sell = qty_total
            amount_usdt_sell = current_value_usdt
        else:
            local_amount = float(amount_usdt)
            if local_amount <= 0:
                raise ValueError("Сумма продажи должна быть больше 0 USDT")
            qty_sell = min(local_amount / price, qty_total)
            amount_usdt_sell = qty_sell * price

        share = qty_sell / qty_total
        cost_usdt = float(pos.get("invested_usdt", 0)) * share
        proceeds_usdt = qty_sell * price
        pnl_usdt = proceeds_usdt - cost_usdt
        pnl_pct = (pnl_usdt / cost_usdt * 100) if cost_usdt else 0

        remaining_qty = qty_total - qty_sell
        if remaining_qty <= 1e-12 or share > 0.999:
            positions.pop(coin, None)
        else:
            pos["qty"] = remaining_qty
            pos["invested_usdt"] = float(pos.get("invested_usdt", 0)) - cost_usdt
            pos["avg_price"] = pos["invested_usdt"] / pos["qty"] if pos["qty"] else 0
            pos.pop("invested_rub", None)
            positions[coin] = pos

        captured["result"] = {
            "coin": coin,
            "action": "SELL",
            "price": price,
            "amount_usdt": amount_usdt_sell,
            "qty": qty_sell,
            "pnl_usdt": pnl_usdt,
            "pnl_pct": pnl_pct,
            "sell_all": sell_all or remaining_qty <= 1e-12,
        }

    update_portfolio(_mut)
    result = captured["result"]
    add_journal(result)
    return result


def get_position(coin: str) -> dict | None:
    pos = load_portfolio().get("positions", {}).get(coin.upper().strip())
    return _normalize_old_position(pos) if pos else None


def get_open_positions() -> dict:
    positions = load_portfolio().get("positions", {})
    return {coin: _normalize_old_position(pos) for coin, pos in positions.items()}


def export_journal_csv(path: Path | None = None) -> Path:
    """Экспорт журнала сделок в CSV для Excel/Google Sheets."""
    import csv

    rows = load_journal()
    if path is None:
        path = DATA_DIR / "journal_export.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "date", "action", "coin", "price", "amount_usdt", "qty",
        "avg_price_after", "pnl_usdt", "pnl_pct", "sell_all", "reason"
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
