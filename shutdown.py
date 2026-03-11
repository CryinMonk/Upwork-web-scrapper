import signal
import sys
from venv import logger
from database import close_db
from discordbot import close_bot

def signal_handler(signum, frame):
    logger.info("Shut down signal received")
    global shutdown_event
    shutdown_event = True
    close_db()
    close_bot()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

