"""Ansible callback plugin behind locomotive's "Stop after this task".

Loaded into ansible-playbook (never into locomotive itself) through ANSIBLE_CALLBACK_PLUGINS. As each task
or handler is about to start, if the file named by LOCOMOTIVE_STOP_FILE exists, it raises
KeyboardInterrupt. Ansible's callback dispatch only catches Exception, so the interrupt ends the Playbook
("User interrupted execution", exit 99) before the new task is queued on any host: the task that was
running has finished everywhere and nothing is left half-done. No final stats are emitted, so locomotive
builds the recap itself.
"""

from __future__ import annotations

import os

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: locomotive_stop
    type: aggregate
    short_description: End the Playbook between tasks when locomotive asks
    description:
      - Raises KeyboardInterrupt at the start of a task once the file named by LOCOMOTIVE_STOP_FILE exists.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "locomotive_stop"
    CALLBACK_NEEDS_ENABLED = False
    CALLBACK_NEEDS_WHITELIST = False

    def _maybe_stop(self) -> None:
        path = os.environ.get("LOCOMOTIVE_STOP_FILE")
        if path and os.path.exists(path):
            raise KeyboardInterrupt

    def v2_playbook_on_task_start(self, task, is_conditional):  # noqa: ARG002
        self._maybe_stop()

    def v2_playbook_on_handler_task_start(self, task):  # noqa: ARG002
        self._maybe_stop()
