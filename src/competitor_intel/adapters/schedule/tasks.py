"""Celery 任务与定时调度（P1 / TDD 7.4）。

- worker 执行 execute_run_task：内部用 asyncio.run 驱动现有 async 管线；
- beat 每天触发 daily_run：只创建 run 记录并投递任务，不在 beat 进程里跑采集；
- Playwright 必须在任务内部创建/关闭（现有 collector 已满足），
  因此 prefork 模式下不会继承父进程的浏览器实例。
"""

import asyncio
import os

from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CONFIG_PATH = os.getenv("TARGETS_CONFIG", "config/targets.yaml")
DAILY_HOUR = int(os.getenv("DAILY_CRON_HOUR", "9"))

celery_app = Celery("competitor_intel", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    "daily-competitor-run": {
        "task": "competitor_intel.daily_run",
        "schedule": crontab(hour=DAILY_HOUR, minute=0),
    }
}


@celery_app.task(name="competitor_intel.execute_run", bind=True, max_retries=2)
def execute_run_task(
    self, run_id: str, config_path: str, competitor: str | None, force: bool
) -> dict:
    from ...core.config import Settings
    from ...core.orchestrator import execute_run
    from .notifier import build_notifier

    settings = Settings.from_env()
    try:
        return asyncio.run(
            execute_run(
                settings,
                run_id=run_id,
                config_path=config_path,
                competitor=competitor,
                force=force,
                notify=build_notifier(settings),
            )
        )
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60) from exc


@celery_app.task(name="competitor_intel.daily_run")
def daily_run() -> dict:
    """定时入口：幂等创建当日全量 run，再投递执行任务。"""
    from ...core.config import Settings
    from ...core.orchestrator import enqueue_run

    settings = Settings.from_env()

    async def _enqueue() -> tuple[dict, bool]:
        return await enqueue_run(settings, config_path=CONFIG_PATH)

    record, created = asyncio.run(_enqueue())
    if created:
        execute_run_task.delay(record["id"], CONFIG_PATH, None, False)
    return {"run_id": record["id"], "created": created}
