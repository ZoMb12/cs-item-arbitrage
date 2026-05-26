import random
import time


def sleep_random(min_sec=0.5, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))
