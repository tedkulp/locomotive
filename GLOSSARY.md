# Glossary

**Controller**:
Where a Playbook Run starts. locomotive is one: a web app plus MCP server that runs Playbooks from one Ansible repo and keeps a history of every Run.
_Avoid_: control node, runner

**Playbook**:
One of the runnable top-level files in the Ansible repo's `playbooks/`, the unit a Controller offers to run.
_Avoid_: task, template, job

**Tag**:
A label on plays and tasks in the Ansible repo. Choosing Tags for a Run limits it to the tasks that carry them. Choosing none runs everything.
_Avoid_: label, filter

**Description**:
Text written in the Ansible repo that says what a Playbook or Tag is for, shown when choosing what to run. A Playbook's Description lives in the Playbook itself. A Tag's Description is repo-wide: a Tag means the same thing in every Playbook.
_Avoid_: comment, help text, summary

**Run**:
One execution of a Playbook from a Controller against a chosen set of hosts, with its own log and outcome. It runs the Ansible repo as of one commit, or the Working Copy.
_Avoid_: task (that is one step inside a play), job

**Banner**:
The line in a Run's log that opens a task's output, `TASK [<name>] ***` (or `RUNNING HANDLER [<name>] ***` for a handler). Clicking a task in the Run page's grid jumps to it.
_Avoid_: header, task line

**Working Copy**:
The clone of the Ansible repo on the Controller where edits are made, which a Run can use with uncommitted changes.
_Avoid_: dev checkout, local repo

**Schedule**:
A saved set of Run settings (Playbook, hosts, tags, check mode, extra vars) with a cron expression; each firing produces one Run.
_Avoid_: job, cron, timer

**Scheduler Ping**:
The periodic ping from the Controller's scheduler to a healthchecks URL, whose absence means Schedules have stopped firing.
_Avoid_: heartbeat, dead-man switch
