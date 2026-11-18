"""Storage errors. Messages name the selected backend and never include passwords."""


class StorageError(RuntimeError):
    pass


class StaleWriteError(StorageError):
    def __init__(self, kind: str, expected_version: int):
        super().__init__(f"stale_write:{kind}:expected_version={expected_version}")
        self.kind = kind
        self.expected_version = expected_version


class StoreUnavailable(StorageError):
    pass


class CacheUnavailable(StorageError):
    pass
