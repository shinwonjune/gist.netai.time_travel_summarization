import threading
import time
import unittest

from gist.netai.time_travel_summarization.video_capture import FrameQueue


class FrameQueueTest(unittest.TestCase):
    def test_push_pop(self):
        queue = FrameQueue()
        queue.push("a")
        queue.push("b")
        queue.push("c")

        self.assertEqual(queue.pop(), "a")
        self.assertEqual(queue.pop(), "b")
        self.assertEqual(queue.pop(), "c")
        self.assertEqual(queue.dropped, 0)

    def test_drop_oldest_when_full(self):
        queue = FrameQueue(maxsize=2)
        for index in range(5):
            queue.push(index)

        self.assertEqual(queue.dropped, 3)
        self.assertEqual(queue.pop(), 3)
        self.assertEqual(queue.pop(), 4)

    def test_pop_blocks_then_unblocks_on_close(self):
        queue = FrameQueue()
        result = []

        def _consumer():
            result.append(queue.pop())

        consumer = threading.Thread(target=_consumer)
        consumer.start()
        time.sleep(0.1)
        queue.close()
        consumer.join(timeout=1.0)

        self.assertFalse(consumer.is_alive())
        self.assertEqual(result, [None])

    def test_push_after_close_is_noop(self):
        queue = FrameQueue(maxsize=1)
        queue.close()
        queue.push("ignored")

        self.assertEqual(queue.dropped, 0)
        self.assertIsNone(queue.pop())


if __name__ == "__main__":
    unittest.main()
