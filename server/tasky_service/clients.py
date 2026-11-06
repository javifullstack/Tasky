from datetime import date, datetime
from email.utils import parsedate_to_datetime
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ProviderError(Exception):
    pass


class NotConfigured(ProviderError):
    pass


class ApiError(ProviderError):
    def __init__(self, provider, status=None):
        self.provider = provider
        self.status = status
        super().__init__(f"{provider} request failed" + (f" (HTTP {status})" if status else ""))


def _local_due(value, timezone):
    if not value or len(value) == 10:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.astimezone(timezone).isoformat()
    except (TypeError, ValueError):
        pass
    return value


def _date_of_due(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


class JsonApi:
    def __init__(self, provider, base_url, timeout=8):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method, path, query=None, headers=None, retry_get=True):
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlencode(query)}"
        request = Request(url, method=method, headers={"Accept": "application/json", **(headers or {})})
        for attempt in range(2 if method == "GET" and retry_get else 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                return json.loads(payload) if payload else None
            except HTTPError as error:
                retryable = error.code == 429 or error.code >= 500
                if retryable and attempt == 0 and method == "GET":
                    delay = self._retry_delay(error.headers.get("Retry-After"))
                    time.sleep(delay)
                    continue
                raise ApiError(self.provider, error.code) from None
            except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                if attempt == 0 and method == "GET":
                    time.sleep(0.25)
                    continue
                raise ApiError(self.provider) from None
        raise ApiError(self.provider)

    @staticmethod
    def _retry_delay(value):
        if value:
            try:
                return min(1.0, max(0.0, float(value)))
            except ValueError:
                try:
                    return min(1.0, max(0.0, (parsedate_to_datetime(value) - datetime.now().astimezone()).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return 0.25


class TodoistClient:
    def __init__(self, token, timezone):
        self.token = token
        self.timezone = timezone
        self.api = JsonApi("Todoist", "https://api.todoist.com/api/v1")

    def _get(self, path, query):
        if not self.token:
            raise NotConfigured("Todoist is not configured")
        return self.api.request("GET", path, query, {"Authorization": f"Bearer {self.token}"})

    def _pages(self, path, query):
        cursor = None
        results = []
        for _ in range(100):
            page_query = {**query, "limit": 200}
            if cursor:
                page_query["cursor"] = cursor
            page = self._get(path, page_query)
            results.extend(page.get("results", []))
            cursor = page.get("next_cursor")
            if not cursor:
                return results
        raise ApiError("Todoist pagination limit exceeded")

    def fetch_today(self, today):
        tasks = self._pages("tasks/filter", {"query": "today | overdue", "lang": "en"})
        projects = self._pages("projects", {})
        project_names = {str(project["id"]): project.get("name", "") for project in projects}
        normalized = []
        for task in tasks:
            due = task.get("due") or {}
            due_value = _local_due(due.get("datetime") or due.get("date"), self.timezone)
            due_date = _date_of_due(due_value)
            if not due_date or due_date > today:
                continue
            normalized.append({
                "id": str(task["id"]),
                "source": "todoist",
                "title": task.get("content", ""),
                "due": due_value,
                "context": project_names.get(str(task.get("project_id", "")), ""),
                "url": "",
            })
        return normalized

    def complete(self, task_id):
        if not self.token:
            raise NotConfigured("Todoist is not configured")
        return self.api.request(
            "POST", f"tasks/{task_id}/close", headers={"Authorization": f"Bearer {self.token}"}, retry_get=False
        )

class TrelloClient:
    def __init__(self, key, token, timezone):
        self.key = key
        self.token = token
        self.timezone = timezone
        self.api = JsonApi("Trello", "https://api.trello.com/1")

    def _get(self, path, query=None):
        if not self.key or not self.token:
            raise NotConfigured("Trello is not configured")
        return self.api.request("GET", path, {**(query or {}), "key": self.key, "token": self.token})

    def fetch_today(self, today):
        boards = self._get("members/me/boards", {"filter": "open", "fields": "id,name", "limit": 1000})
        normalized = []
        for board in boards:
            board_id = board["id"]
            cards = self._get(f"boards/{board_id}/cards", {
                "filter": "open",
                "fields": "id,name,due,dueComplete,idList,idBoard,closed,url",
                "limit": 1000,
            })
            lists = self._get(f"boards/{board_id}/lists", {"filter": "all", "fields": "id,name"})
            list_names = {item["id"]: item.get("name", "") for item in lists}
            for card in cards:
                due = _local_due(card.get("due"), self.timezone)
                due_date = _date_of_due(due)
                if card.get("closed") or card.get("dueComplete") or not due_date or due_date > today:
                    continue
                list_name = list_names.get(card.get("idList", ""), "")
                board_name = board.get("name", "")
                normalized.append({
                    "id": str(card["id"]),
                    "source": "trello",
                    "title": card.get("name", ""),
                    "due": due,
                    "context": " · ".join(part for part in (board_name, list_name) if part),
                    "url": card.get("url", ""),
                })
        return normalized

    def archive(self, card_id):
        if not self.key or not self.token:
            raise NotConfigured("Trello is not configured")
        return self.api.request(
            "PUT", f"cards/{card_id}", {"closed": "true", "key": self.key, "token": self.token}, retry_get=False
        )
