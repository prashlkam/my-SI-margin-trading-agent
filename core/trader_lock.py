"""
Single-trader lock (Azure.md §2a).

App Service restarts (deploys, settings changes, platform recycles) start the new container
BEFORE stopping the old one, so for ~20s two copies of the agent run side by side. With a
live broker both would scan, select and place orders - duplicate real trades.

This is a lease file on the persistent storage that every container of the app shares
(/home/site/data on Azure). Only the holder runs live trading ticks; any other instance
stays in STANDBY until the holder releases the lease (graceful shutdown) or stops renewing
it (crash / hard kill) and it expires.

Why a lease and not flock(): /home is an SMB (Azure Files) mount, where advisory locks are
not reliable across hosts. The lease only needs atomic rename + O_EXCL create, which SMB has.

Fencing: the holder stops trading as soon as it has failed to renew for
TTL - SAFETY_MARGIN seconds, i.e. before any other instance may treat the lease as expired.
"""
import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class TraderLock:
    def __init__(self, data_dir: str, ttl_sec: Optional[float] = None, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = os.environ.get("TRADER_LOCK", "true").strip().lower() not in ("0", "false", "no", "off")
        self.enabled = enabled
        self.path = os.environ.get("TRADER_LOCK_FILE") or os.path.join(data_dir, "trader.lock")
        self.ttl = float(ttl_sec if ttl_sec is not None else os.environ.get("TRADER_LOCK_TTL_SEC", 30))
        self.safety_margin = min(10.0, self.ttl / 3)
        self.renew_interval = max(1.0, self.ttl / 6)
        # Readable owner id: Azure instance + container hostname + pid + random suffix.
        instance = os.environ.get("WEBSITE_INSTANCE_ID", "")[:8]
        self.owner = f"{instance + '/' if instance else ''}{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:6]}"
        self._held_until = 0.0          # monotonic deadline: trading allowed only before this
        self._standby_since: Optional[float] = time.monotonic()
        self._last_holder: Dict[str, Any] = {}
        # Held by the caller for the whole of each trading tick; release() takes it so the
        # lease is never handed over while an order may still be in flight.
        self.tick_guard = threading.Lock()
        self._released = False

    # ------------------------------------------------------------------ file ops
    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            # Torn/partial write (shouldn't happen with replace) - treat as unreadable, not free.
            return {"owner": "?", "expires_at": time.time() + self.ttl}

    def _write(self, record: Dict[str, Any]):
        tmp = f"{self.path}.{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _record(self) -> Dict[str, Any]:
        now = time.time()
        return {"owner": self.owner, "expires_at": now + self.ttl, "renewed_at": now,
                "hostname": socket.gethostname(), "pid": os.getpid()}

    # ------------------------------------------------------------------ lease
    def refresh(self) -> bool:
        """Acquire or renew the lease. Call every `renew_interval` seconds. Returns is_holder()."""
        if not self.enabled:
            return True
        if self._released:
            return False
        start = time.monotonic()
        try:
            current = self._read()
            if current and current.get("owner") == self.owner:
                self._write(self._record())                       # renew
            elif current is None:
                try:                                              # free: create exclusively
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    with os.fdopen(fd, "w") as f:
                        json.dump(self._record(), f)
                except FileExistsError:
                    return self._lost(self._read())               # another instance won the race
                if not self._confirm():
                    return self._lost(self._read())
                self._acquired()
            elif float(current.get("expires_at", 0)) < time.time():
                logger.warning(f"Trader lock held by {current.get('owner')} expired - taking over.")
                self._write(self._record())                       # expired: take over
                if not self._confirm():
                    return self._lost(self._read())
                self._acquired()
            else:
                return self._lost(current)
        except OSError as e:
            # Storage hiccup: keep the existing deadline; trading stops by itself when it passes.
            logger.error(f"Trader lock refresh failed: {e}")
            return self.is_holder()
        self._held_until = start + self.ttl - self.safety_margin
        return True

    def _confirm(self) -> bool:
        """After a takeover, wait and re-read: if two standbys raced, only one id survives."""
        time.sleep(min(1.0, self.ttl / 10))
        current = self._read()
        return bool(current and current.get("owner") == self.owner)

    def _acquired(self):
        waited = time.monotonic() - self._standby_since if self._standby_since else 0
        self._standby_since = None
        logger.warning(f"Trader lock ACQUIRED by {self.owner} after {waited:.0f}s standby - this instance now trades.")

    def _lost(self, holder: Optional[Dict[str, Any]]) -> bool:
        if self._standby_since is None:
            logger.critical(f"Trader lock LOST to {holder and holder.get('owner')} - this instance stops trading.")
            self._standby_since = time.monotonic()
        self._held_until = 0.0
        self._last_holder = holder or {}
        return False

    def is_holder(self) -> bool:
        return (not self.enabled) or (not self._released and time.monotonic() < self._held_until)

    def release(self, timeout: float = 20.0):
        """Graceful shutdown: stop trading, wait for an in-flight tick, then free the lease."""
        if not self.enabled:
            return
        self._released = True
        self._held_until = 0.0
        got_guard = self.tick_guard.acquire(timeout=timeout)
        try:
            current = self._read()
            if current and current.get("owner") == self.owner:
                if not got_guard:
                    # A tick is still running (stuck broker call?). Don't hand over yet - the
                    # lease expires on its own, after that call has had more time to finish.
                    logger.error("Trader lock: tick still running at shutdown; leaving lease to expire.")
                    return
                os.remove(self.path)
                logger.warning(f"Trader lock released by {self.owner}.")
        except OSError as e:
            logger.error(f"Trader lock release failed (it will expire in {self.ttl:.0f}s): {e}")
        finally:
            if got_guard:
                self.tick_guard.release()

    def status(self) -> Dict[str, Any]:
        holder = self.is_holder()
        standby = (time.monotonic() - self._standby_since) if (self._standby_since and not holder) else 0.0
        return {
            "enabled": self.enabled,
            "role": "active" if holder else "standby",
            "owner": self.owner,
            "holder": None if holder else self._last_holder.get("owner"),
            "standby_sec": int(standby),
        }
