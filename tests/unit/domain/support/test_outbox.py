from datetime import timedelta

from otto.domain.support import outbox


class TestRetryDelay:
    def test_backs_off_after_each_failure_and_gives_up_on_the_fifth(self):
        # Given the delivery failure counts one through five

        # When the wait before the next attempt is asked for each
        delays = [outbox.retry_delay(attempts=attempts) for attempts in range(1, 6)]

        # Then the waits grow from seconds to an hour, and the fifth is final
        assert delays == [
            timedelta(seconds=10),
            timedelta(minutes=1),
            timedelta(minutes=10),
            timedelta(hours=1),
            None,
        ]
