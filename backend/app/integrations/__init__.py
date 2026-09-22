"""Outbound ticketing and inbound ticket state.

The shape, once, so the parts below are readable in isolation:

    alarm raised/cleared (ingest worker, inside unit_of_work)
        -> policy decides whether this condition earns a ticket
        -> outbox row, SAME transaction as the alarm
                    [commit]
        -> dispatcher claims with FOR UPDATE SKIP LOCKED
        -> JiraTarget: create / update / transition, keyed by fingerprint
        -> jira_link remembers fingerprint -> issue key

Three rules hold everything together and are each enforced in one place:

1. The fingerprint is the alarm's own identity - (device, alarm_type,
   instance) - and nothing else. `fingerprint.py`.
2. Not every alarm is a ticket. `policy.py`.
3. A closed ticket acknowledges an alarm; only the poll clears one. Enforced
   on the inbound side, which is phase 2.
"""
