"""Jira Service Management, read-only: issues in, tickets out.

Every request in JSM is raised under one or more Organizations, and an
organization's name is a client's name. That is the whole mapping - no field on
the account, no table of pairs - so an organization that matches no account, or
more than one, is reported on the integrations page rather than guessed at.

Every issue type counts. A Bug scores three in the Open Tickets factor and
anything else scores one; which Jira types are "Bug" is
``settings.JIRA_BUG_ISSUE_TYPES``.

Nothing outside this package imports the HTTP client, so the day Atlassian
changes the API one package changes.
"""
