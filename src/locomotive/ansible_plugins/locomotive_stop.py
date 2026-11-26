"""Ansible callback plugin behind locomotive's "Stop after this task".

Loaded into ansible-playbook (never into locomotive itself) through ANSIBLE_CALLBACK_PLUGINS. Once the file
named by LOCOMOTIVE_STOP_FILE exists, the next task to start doesn't run:

- If no handler is notified, the plugin raises KeyboardInterrupt. Ansible's callback dispatch only catches
  Exception, so the interrupt ends the Playbook ("User interrupted execution", exit 99) before the new task
  is queued on any host: the task that was running has finished everywhere and nothing is left half-done.
- If handlers are notified (and not yet run), they still run first: the new task is skipped on every host
  (it gets an always-false condition; its uuid goes in the ``.done`` file below so locomotive's grid
  leaves it out) and every host goes
  straight to its handlers, then ends the play. The next task to start after that (in a later play or
  batch) ends the Playbook as above. Handlers themselves are never stopped: they are what was notified.

Either way the stop file gets a ``.done`` sibling, so locomotive records the Run as stopped even if Ansible
reached its final stats (the stop came during the last play). When Ansible is interrupted no final stats are
emitted, so locomotive builds the recap itself.

Reaching the handlers needs the play's iterator, which Ansible doesn't hand to callbacks: it is found on the
stack (the strategy's frame). Seam 2 (``tests/test_smoke_real_ansible.py``) guards this on a pin bump.
"""

from __future__ import annotations

import os
import sys

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: locomotive_stop
    type: aggregate
    short_description: End the Playbook between tasks when locomotive asks
    description:
      - Once the file named by LOCOMOTIVE_STOP_FILE exists, runs the handlers already notified and ends the
        Playbook before another task starts.
"""

STOP_CONDITION = "'locomotive' == 'stopped after a task'"
META_ACTIONS = ("meta", "ansible.builtin.meta", "ansible.legacy.meta")


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "locomotive_stop"
    CALLBACK_NEEDS_ENABLED = False
    CALLBACK_NEEDS_WHITELIST = False

    def v2_playbook_on_task_start(self, task, is_conditional):  # noqa: ARG002
        path = os.environ.get("LOCOMOTIVE_STOP_FILE")
        if not path or not os.path.exists(path) or task.action in META_ACTIONS:
            return  # meta tasks (flush_handlers among them) run as they are
        found = _strategy_and_iterator()
        if found is not None and _flush_to_handlers(*found):
            _mark_done(path, task._uuid)
            task.when = [_trusted(STOP_CONDITION)]
            self._display.display(
                f"locomotive: stop requested: skipping {task.get_name()!r} and running the handlers already notified"
            )
            return
        _mark_done(path)
        raise KeyboardInterrupt


def _trusted(expression: str) -> str:
    """Conditions must be trusted templates since ansible-core 2.19 (data tagging)."""
    try:
        from ansible.template import trust_as_template
    except ImportError:
        return expression
    return trust_as_template(expression)


def _mark_done(path: str, skipped_task: str | None = None) -> None:
    """``<stop file>.done`` says the stop took effect; it lists the uuid of each task skipped for it."""
    try:
        with open(path + ".done", "a") as f:
            if skipped_task:
                f.write(skipped_task + "\n")
    except OSError:
        pass


def _strategy_and_iterator():
    """The running strategy plugin and its PlayIterator, from the strategy's ``run`` frame."""
    from ansible.executor.play_iterator import PlayIterator
    from ansible.plugins.strategy import StrategyBase

    frame = sys._getframe(1)
    while frame is not None:
        iterator = frame.f_locals.get("iterator")
        strategy = frame.f_locals.get("self")
        if isinstance(iterator, PlayIterator) and isinstance(strategy, StrategyBase):
            return strategy, iterator
        frame = frame.f_back
    return None


def _flush_to_handlers(strategy, iterator) -> bool:
    """Send every live host to its notified handlers, then to the end of the play (what ``meta:
    flush_handlers`` followed by the end of the play would do). False if no handler is notified anywhere."""
    from ansible.executor.play_iterator import IteratingStates

    hosts = [
        host for host in strategy._inventory.get_hosts(iterator._play.hosts)
        if host.name in iterator._host_states
        and host.name not in strategy._tqm._unreachable_hosts
        and not iterator.is_failed(host)
    ]
    pending = any(iterator.get_state_for_host(h.name).handler_notifications for h in hosts) or any(
        handler.notified_hosts for handler in iterator.handlers
    )
    if not pending:
        return False
    for host in hosts:
        state = iterator.get_state_for_host(host.name)
        for notification in list(state.handler_notifications):
            # (No v2_playbook_on_notify: send_callback holds a non-reentrant lock while this plugin runs.)
            for handler in strategy.search_handlers_by_notification(notification, iterator):
                handler.notify_host(host)
            iterator.clear_notification(host.name, notification)
        if state.run_state != IteratingStates.HANDLERS:
            state.pre_flushing_run_state = IteratingStates.COMPLETE
            state.run_state = IteratingStates.HANDLERS
    if isinstance(getattr(iterator, "all_tasks", None), list):
        # The linear strategy walks the play's tasks in lockstep through ``all_tasks``; it splices the
        # handlers in after a flush_handlers, so do the same after the task being skipped.
        iterator.all_tasks[iterator.cur_task:iterator.cur_task] = [
            h for b in iterator._play.handlers for h in b.block
        ]
    return True
