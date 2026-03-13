import logging
import psutil
import os
from database.database import log

logger = logging.getLogger("memory")


def check_memory():
    process = psutil.Process(os.getpid())
    memory = process.memory_info().rss / 1024 / 1024

    msg = f"Memory usage: {memory:.2f} MB"
    logger.info(msg)
    log("INFO", "memory", msg)

    if memory > 500:
        msg = f"Memory usage exceeded 500 MB (current: {memory:.2f} MB)"
        logger.warning(msg)
        log("WARNING", "memory", msg)