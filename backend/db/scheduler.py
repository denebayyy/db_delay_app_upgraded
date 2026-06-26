import asyncio
import heapq

from dataclasses import dataclass, field
from typing import Awaitable, Callable
import itertools

_counter = itertools.count()

@dataclass(order=True)
class Job:
    when: float
    func: Callable[[], Awaitable] = field(compare=False)
    order: int = field(default_factory=lambda: next(_counter), compare=False)
    interval: float | None = field(default=None, compare=False)
    cancelled: bool = field(default=False, compare=False)

class Scheduler:
    def __init__(self):
        self._jobs = []
        self._event = asyncio.Event()
        self._tasks = set()

    async def schedule_at(self, when, func):
        job = Job(when=when, func=func)
        heapq.heappush(self._jobs, job)
        self._event.set()
        return job

    async def schedule_after(self, delay, func):
        loop = asyncio.get_running_loop()
        return await self.schedule_at(loop.time() + delay, func)

    async def schedule_every(self, interval, func):
        loop = asyncio.get_running_loop()
        job = Job(
            when=loop.time() + interval,
            func=func,
            interval=interval,
        )
        heapq.heappush(self._jobs, job)
        self._event.set()
        return job

    def cancel(self, job):
        job.cancelled = True

    async def run(self):
        loop = asyncio.get_running_loop()

        while True:
            if not self._jobs:
                self._event.clear()
                await self._event.wait()
                continue

            job = self._jobs[0]

            delay = job.when - loop.time()

            if delay > 0:
                self._event.clear()

                try:
                    await asyncio.wait_for(
                        self._event.wait(),
                        timeout=delay,
                    )
                    continue
                except asyncio.TimeoutError:
                    pass

            heapq.heappop(self._jobs)

            if job.cancelled:
                continue

            task = asyncio.create_task(job.func())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

            if job.interval is not None and not job.cancelled:
                job.when += job.interval
                heapq.heappush(self._jobs, job)