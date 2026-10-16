"""Every Redis key the app uses, in one place, so writers and invalidators can't drift apart."""


def board(board_id: str) -> str:
    return f"board:{board_id}"


def tiles(board_id: str) -> str:
    return f"tiles:{board_id}"


def tile_data(tile_id: str, hour: str) -> str:
    return f"tile:{tile_id}:{hour}"


def user_boards_prefix(user_id: str) -> str:
    return f"boards:{user_id}:"


CATALOG = "catalog:v1"
METRIC_PAGES_PREFIX = "metrics:list:"
TILE_DATA_PREFIX = "tile:"
