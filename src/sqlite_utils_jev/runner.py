"""Bounded HTTP concurrency; the calling thread owns every journal write."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections.abc import Callable

from .client import BudgetExhausted, Client, PendingRequest
from .validation import JevError, digest


def run_requests(client: Client, payloads: list[dict], *, workers: int,
                 offline: bool, publish: Callable[[int, dict], None]) -> None:
    pending = {}
    failure = None

    def receive(done):
        nonlocal failure
        for future in done:
            index, request = pending[future]
            result = None
            try:
                result = client._complete(request, future.result(), payloads[index]["questions"])
            except Exception as exc:
                failure = failure or exc
            del pending[future]
            if result is not None:
                publish(index, result)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            for index, payload in enumerate(payloads):
                request_hash = digest(payload)
                # A duplicate waits for its first request, then takes the cache path.
                while pending and (len(pending) >= workers or any(
                    request.request_hash == request_hash for _, request in pending.values()
                )):
                    receive(wait(pending, return_when=FIRST_COMPLETED).done)
                    if failure:
                        break
                receive([future for future in pending if future.done()])
                if failure:
                    break
                try:
                    try:
                        prepared = client._prepare(payload, offline=offline)
                    except BudgetExhausted:
                        # Reservations may temporarily fill the allowance. Settle
                        # outstanding usage before deciding the budget is exhausted.
                        if not pending:
                            raise
                        receive(wait(pending).done)
                        if failure:
                            break
                        prepared = client._prepare(payload, offline=offline)
                except JevError as exc:
                    failure = exc
                    break
                if isinstance(prepared, PendingRequest):
                    pending[pool.submit(client._send, prepared)] = (index, prepared)
                else:
                    publish(index, prepared)
            if pending:
                receive(wait(pending).done)
            if failure:
                raise failure
        finally:
            # A callback, database error or Ctrl-C must not discard responses for
            # calls already sent. Save them for recovery; never start more work.
            for future, (index, request) in pending.items():
                try:
                    client._complete(request, future.result(), payloads[index]["questions"])
                except Exception:
                    # _complete persists failures before raising. A storage failure
                    # leaves the durable pending reservation for explicit recovery.
                    pass
