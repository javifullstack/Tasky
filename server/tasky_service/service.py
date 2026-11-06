from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Lock

from .clients import NotConfigured, ProviderError


class TaskService:
    def __init__(self, todoist, trello, timezone, refresh_seconds=300, clock=None):
        self.clients = {"todoist": todoist, "trello": trello}
        self.timezone = timezone
        self.refresh_seconds = refresh_seconds
        self.clock = clock or (lambda: datetime.now(self.timezone))
        self._lock = Lock()
        self._snapshot = {name: {"tasks": [], "status": "not_configured", "updated_at": None, "error": ""}
                          for name in self.clients}
        self._last_refresh = None

    def get_today(self, force=False, source=None, offset=0, limit=None):
        now = self.clock()
        with self._lock:
            fresh = self._last_refresh and (now - self._last_refresh).total_seconds() < self.refresh_seconds
            if self._last_refresh and fresh and not force:
                return self._response(source, offset, limit)
            previous = deepcopy(self._snapshot)
            self._last_refresh = now

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(client.fetch_today, now.date()): name for name, client in self.clients.items()}
            updated = {}
            for future, name in ((future, name) for future, name in futures.items()):
                try:
                    tasks = future.result()
                    updated[name] = {"tasks": tasks, "status": "ok", "updated_at": now.isoformat(), "error": ""}
                except NotConfigured:
                    updated[name] = {"tasks": [], "status": "not_configured", "updated_at": None, "error": ""}
                except ProviderError:
                    had_cache = bool(previous[name]["updated_at"])
                    updated[name] = {
                        "tasks": previous[name]["tasks"],
                        "status": "stale" if had_cache else "error",
                        "updated_at": previous[name]["updated_at"],
                        "error": "No se pudo actualizar este servicio",
                    }
                except Exception:
                    updated[name] = {
                        "tasks": previous[name]["tasks"],
                        "status": "stale" if previous[name]["updated_at"] else "error",
                        "updated_at": previous[name]["updated_at"],
                        "error": "No se pudo actualizar este servicio",
                    }

        with self._lock:
            self._snapshot = updated
            return self._response(source, offset, limit)

    def complete_todoist(self, task_id):
        self.clients["todoist"].complete(task_id)
        self._remove_cached("todoist", task_id)

    def archive_trello(self, card_id):
        self.clients["trello"].archive(card_id)
        self._remove_cached("trello", card_id)

    def _remove_cached(self, source, item_id):
        with self._lock:
            self._snapshot[source]["tasks"] = [
                task for task in self._snapshot[source]["tasks"] if task["id"] != item_id
            ]
            self._snapshot[source]["total"] = len(self._snapshot[source]["tasks"])

    def _response(self, source=None, offset=0, limit=None):
        updated_times = [item["updated_at"] for item in self._snapshot.values() if item["updated_at"]]
        providers = deepcopy(self._snapshot)
        for name, provider in providers.items():
            provider["total"] = len(provider["tasks"])
            if source and name == source and limit is not None:
                provider["tasks"] = provider["tasks"][offset:offset + limit]
            elif source:
                provider["tasks"] = []
        return {
            "date": self.clock().date().isoformat(),
            "updated_at": max(updated_times) if updated_times else None,
            "refresh_interval_seconds": self.refresh_seconds,
            "providers": providers,
        }