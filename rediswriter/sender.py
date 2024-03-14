import logging
import sys
import time
from collections import deque
from threading import Event, Thread
from typing import Any

import backoff
from log_rate_limit import StreamRateLimitFilter
from prometheus_client import Counter, Histogram
from redis.exceptions import ConnectionError, TimeoutError
from visionlib.pipeline.publisher import RedisPublisher

from .config import RedisWriterConfig

logging.basicConfig(format='%(asctime)s %(name)-15s %(levelname)-8s %(processName)-10s %(message)s')
logger = logging.getLogger(__name__)
logger.addFilter(StreamRateLimitFilter(period_sec=10))

BACKOFF_COUNTER = Counter('redis_writer_backoff_counter', 'How often publishing to Redis has to be backed off (i.e. retried)')
GIVEUP_COUNTER = Counter('redis_writer_giveup_counter', 'How many messages were discarded due to exhausted retries')
DISCARD_BUFFER_COUNTER = Counter('redis_writer_discard_buffer_counter', 'How many input messages have to be discarded because sender cannot keep up')
REDIS_PUBLISH_DURATION = Histogram('redis_writer_target_redis_publish_duration', 'The time it takes to push a message onto the Redis stream',
                                   buckets=(0.0025, 0.005, 0.0075, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25))

def _on_backoff(_):
    logger.debug(f'Failed to send message. Backing off...')
    BACKOFF_COUNTER.inc()


class Sender:
    def __init__(self, config: RedisWriterConfig) -> None:
        self._config = config.target_redis
        logger.setLevel(config.log_level.value)

        self._buffer = deque(maxlen=self._config.buffer_length)

        self._stop_event = Event()
        self._sender_thread = Thread(target=self._run)
        
    def publish(self, msg_bytes):
        if len(self._buffer) == self._buffer.maxlen:
            logger.debug(f'Message buffer full (maxlen={self._buffer.maxlen}). Discarding message.')
            DISCARD_BUFFER_COUNTER.inc()

        self._buffer.append(msg_bytes)

    def __enter__(self):
        self._sender_thread.start()
        return self.publish
    
    def _run(self):
        publisher = RedisPublisher(
            host=self._config.host,
            port=self._config.port,
            stream_maxlen=self._config.target_stream_maxlen
        )

        with publisher as publish:
            while not self._stop_event.is_set():
                try:
                    msg_bytes = self._buffer.popleft()
                except IndexError:
                    time.sleep(0.05)
                    continue

                try:
                    self._publish_with_backoff(publish, self._config.stream_key, msg_bytes)
                except (ConnectionError, TimeoutError) as e:
                    logger.error(f'Gave up sending message', exc_info=True)
                    GIVEUP_COUNTER.inc()

    @backoff.on_exception(
            backoff.expo, 
            exception=(ConnectionError, TimeoutError), 
            max_tries=7,
            on_backoff=_on_backoff,
            logger=None,
            factor=0.1, 
            max_value=5,
    )
    def _publish_with_backoff(self, publish: callable, stream_key: str, msg_bytes: bytes):
        with REDIS_PUBLISH_DURATION.time():
            publish(self._config.stream_key, msg_bytes)

    def __exit__(self, _, __, ___):
        self._stop_event.set()
        self._sender_thread.join(timeout=10)
        return False