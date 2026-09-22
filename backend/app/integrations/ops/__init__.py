"""JSM Operations alerts: the paging tier, as opposed to the work record.

A Jira issue is where WORK is tracked. An Operations alert is where somebody
gets woken up. They are different jobs, and this package exists because the
plan's §0 said so from the start: in real datacenter operations an alarm
reaches a human through an on-call tier and a ticket is the record of what was
done about it.

This is the target to choose when the customer pages from JSM.
"""
