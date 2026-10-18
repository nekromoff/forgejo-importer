"""Remembers where a migration step stopped, so the next run continues from there"""
from fg_migration.core.json_cache import JsonCache
from fg_migration.utils import fg_print


class Checkpoint:
    """Records the item a step is working on, before working on it. A rerun of the step
       starts at that item (it may be unfinished) instead of going over all items again.
       The checkpoint is removed when the step completes"""

    def __init__(self, cache: JsonCache | None, step: str, from_start: bool = False):
        self.cache = cache
        self.key = f"checkpoint_{step}"
        self.step = step
        self.from_start = from_start

    def resume_index(self, keys: list[str]) -> int:
        """index of the item to start at (0 to start from the beginning)"""
        if self.cache is None:
            return 0
        if self.from_start:
            self.cache.delete(self.key)
            return 0
        hit = self.cache.get(self.key)
        if hit is None:
            return 0
        last_key = hit[0]["key"]
        label = hit[0]["label"]
        if last_key not in keys:
            fg_print.warning(f"The {self.step} step stopped at {label} last time, which is no"
                             " longer in the list, so starting from the beginning")
            return 0
        index = keys.index(last_key)
        fg_print.warning(f"Resuming the {self.step} step at {index + 1}/{len(keys)} ({label}),"
                         f" where the last run stopped. The {index} before it are skipped,"
                         " run with --from-start to go over all of them again")
        return index

    def mark(self, key: str, label: str | None = None) -> None:
        """record the item about to be worked on (label is its name for messages)"""
        if self.cache is not None:
            self.cache.put(self.key, {"key": key, "label": label or key})

    def finish(self) -> None:
        """the step completed, the next run starts from the beginning"""
        if self.cache is not None:
            self.cache.delete(self.key)
