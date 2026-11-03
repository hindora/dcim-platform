"""Alarm notifications by email: the mail relay, the routing rules, delivery.

Separate from `app.integrations` on purpose. That package opens and closes
TICKETS - work with a lifecycle of its own on somebody else's service desk. This
one tells a PERSON that a condition started or ended. They share the policy
clauses, the alarm fingerprint and the retry schedule, and nothing else.
"""
