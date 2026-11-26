# Glossary

**Controller**:
Where a Playbook Run starts. locomotive is one: a web app plus MCP server that runs Playbooks from one Ansible repo and keeps a history of every Run.
_Avoid_: control node, runner

**Playbook**:
One of the runnable top-level files in the Ansible repo's `playbooks/`, the unit a Controller offers to run.
_Avoid_: task, template, job

**Run**:
One execution of a Playbook from a Controller against a chosen set of hosts, with its own log and outcome. It runs the Ansible repo as of one commit, or the Working Copy.
_Avoid_: task (that is one step inside a play), job

**Working Copy**:
The clone of the Ansible repo on the Controller where edits are made, which a Run can use with uncommitted changes.
_Avoid_: dev checkout, local repo

**Schedule**:
A saved set of Run settings (Playbook, hosts, tags, check mode, extra vars) with a cron expression; each firing produces one Run.
_Avoid_: job, cron, timer

**Scheduler Ping**:
The periodic ping from the Controller's scheduler to a healthchecks URL, whose absence means Schedules have stopped firing.
_Avoid_: heartbeat, dead-man switch
