import unittest
from datetime import date, datetime, timezone
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from tasky_service.clients import TodoistClient, TrelloClient, _date_of_due
from tasky_service.http_server import make_server
from tasky_service.service import TaskService


class FakeClient:
    def __init__(self, tasks=None, failure=None):
        self.tasks = tasks or []
        self.failure = failure

    def fetch_today(self, _today):
        if self.failure:
            raise self.failure
        return self.tasks

    def complete(self, _item_id):
        return None

    def archive(self, _item_id):
        return None


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
        self.zone = ZoneInfo("UTC")

    def test_partial_provider_failure_keeps_successful_provider(self):
        todoist = FakeClient([{"id": "task-1", "title": "Review", "source": "todoist"}])
        trello = FakeClient(failure=RuntimeError("network unavailable"))
        service = TaskService(todoist, trello, self.zone, clock=lambda: self.now)

        response = service.get_today()

        self.assertEqual(response["providers"]["todoist"]["status"], "ok")
        self.assertEqual(len(response["providers"]["todoist"]["tasks"]), 1)
        self.assertEqual(response["providers"]["trello"]["status"], "error")

    def test_failed_refresh_retains_last_successful_data(self):
        todoist = FakeClient([{"id": "task-1", "title": "Review", "source": "todoist"}])
        trello = FakeClient([])
        service = TaskService(todoist, trello, self.zone, clock=lambda: self.now)
        service.get_today()
        todoist.failure = RuntimeError("network unavailable")

        response = service.get_today(force=True)

        self.assertEqual(response["providers"]["todoist"]["status"], "stale")
        self.assertEqual(response["providers"]["todoist"]["tasks"][0]["id"], "task-1")

    def test_due_date_parser_rejects_missing_dates(self):
        self.assertIsNone(_date_of_due(None))

    def test_device_page_is_bounded_and_reports_total(self):
        tasks = [{"id": f"task-{number}", "title": str(number)} for number in range(8)]
        service = TaskService(FakeClient(tasks), FakeClient([]), self.zone, clock=lambda: self.now)

        response = service.get_today(source="todoist", offset=5, limit=3)

        self.assertEqual(response["providers"]["todoist"]["total"], 8)
        self.assertEqual([task["id"] for task in response["providers"]["todoist"]["tasks"]], ["task-5", "task-6", "task-7"])
        self.assertEqual(response["providers"]["trello"]["tasks"], [])

    def test_successful_action_removes_item_from_cached_page(self):
        todoist = FakeClient([{"id": "task-1", "title": "Review", "source": "todoist"}])
        service = TaskService(todoist, FakeClient([]), self.zone, clock=lambda: self.now)
        service.get_today()

        service.complete_todoist("task-1")
        response = service.get_today(source="todoist", limit=5)

        self.assertEqual(response["providers"]["todoist"]["tasks"], [])
        self.assertEqual(response["providers"]["todoist"]["total"], 0)


class HttpServerTests(unittest.TestCase):
    def setUp(self):
        zone = ZoneInfo("UTC")
        todoist = FakeClient([{"id": "task-1", "title": "Review", "source": "todoist"}])
        self.service = TaskService(todoist, FakeClient([]), zone)
        settings = SimpleNamespace(host="127.0.0.1", port=0, device_token="device-secret")
        self.server = make_server(settings, self.service)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_api_rejects_missing_device_token(self):
        request = Request(self.url + "/api/v1/today")
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_api_returns_bounded_page_with_device_token(self):
        request = Request(
            self.url + "/api/v1/today?source=todoist&limit=5",
            headers={"Authorization": "Bearer device-secret"},
        )
        with urlopen(request) as response:
            payload = response.read().decode("utf-8")

        self.assertIn('"title": "Review"', payload)
        self.assertIn('"total": 1', payload)


class ProviderNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo("Europe/Madrid")
        self.today = date(2026, 11, 6)

    def test_todoist_includes_due_and_overdue_but_not_undated(self):
        client = TodoistClient("token", self.zone)
        pages = {
            "tasks/filter": [
                {"id": "late", "content": "Late", "project_id": "p1", "due": {"date": "2026-11-05"}},
                {"id": "today", "content": "Today", "project_id": "p1", "due": {"date": "2026-11-06"}},
                {"id": "none", "content": "No date", "project_id": "p1", "due": None},
                {"id": "future", "content": "Future", "project_id": "p1", "due": {"date": "2026-11-07"}},
                {"id": "utc-late", "content": "Local tomorrow", "project_id": "p1", "due": {"datetime": "2026-11-06T23:30:00Z"}},
            ],
            "projects": [{"id": "p1", "name": "Personal"}],
        }
        client._pages = lambda path, _query: pages[path]

        tasks = client.fetch_today(self.today)

        self.assertEqual([task["id"] for task in tasks], ["late", "today"])
        self.assertTrue(all(task["context"] == "Personal" for task in tasks))

    def test_trello_excludes_undated_completed_and_future_cards(self):
        client = TrelloClient("key", "token", self.zone)
        responses = {
            "members/me/boards": [{"id": "board-1", "name": "Home"}],
            "boards/board-1/cards": [
                {"id": "late", "name": "Late", "idList": "list-1", "due": "2026-11-05T10:00:00Z"},
                {"id": "today", "name": "Today", "idList": "list-1", "due": "2026-11-06T10:00:00Z"},
                {"id": "none", "name": "No date", "due": None},
                {"id": "done", "name": "Done", "due": "2026-11-06T10:00:00Z", "dueComplete": True},
                {"id": "future", "name": "Future", "due": "2026-11-07T10:00:00Z"},
                {"id": "utc-late", "name": "Local tomorrow", "due": "2026-11-06T23:30:00Z"},
            ],
            "boards/board-1/lists": [{"id": "list-1", "name": "Inbox"}],
        }
        client._get = lambda path, _query=None: responses[path]

        tasks = client.fetch_today(self.today)

        self.assertEqual([task["id"] for task in tasks], ["late", "today"])
        self.assertEqual(tasks[0]["context"], "Home · Inbox")
        self.assertEqual(tasks[1]["due"], "2026-11-06T11:00:00+01:00")


if __name__ == "__main__":
    unittest.main()