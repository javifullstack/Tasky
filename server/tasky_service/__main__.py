from .clients import TodoistClient, TrelloClient
from .config import Settings
from .http_server import make_server
from .service import TaskService


def main():
    settings = Settings.from_env()
    if not settings.device_token:
        raise SystemExit("Set TASKY_DEVICE_TOKEN before starting Tasky.")
    service = TaskService(
        TodoistClient(settings.todoist_token, settings.timezone),
        TrelloClient(settings.trello_key, settings.trello_token, settings.timezone),
        settings.timezone,
        settings.refresh_seconds,
    )
    server = make_server(settings, service)
    print(f"Tasky service listening on {settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


main()