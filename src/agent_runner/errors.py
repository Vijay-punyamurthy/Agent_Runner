"""Explicit error classes.

Worker-side errors are matched BY CLASS NAME by Step Functions Retry/Catch, so:
  * TransientError  -> retried with backoff
  * anything else   -> permanent (Catch -> mark_failed)
Do NOT subclass TransientError (the Lambda errorType would change and stop matching).
"""


class RunnerError(Exception):
    """Client-facing control-plane error (bad request / invalid operation)."""


class RunNotFound(RunnerError):
    pass


class InvalidTransition(RunnerError):
    pass


class UnknownRequest(RunnerError):
    pass


class Conflict(RunnerError):
    """E.g. a human request was already answered with a different decision."""


class BadInput(RunnerError):
    pass


class TransientError(Exception):
    """Worth retrying: throttling, timeouts, flaky downstream."""


class PermanentError(Exception):
    """Will not succeed on retry: bad input, agent gave up."""


class RunNotRunning(Exception):
    """The run is no longer RUNNING (stopped/failed/answered elsewhere). Not a failure."""
