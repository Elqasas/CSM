"""Microsoft 365: Outlook calendars into ``Meeting`` rows, Teams attendance into activities.

The only package that talks to Microsoft Graph. Everything outside it sees
``GraphEvent`` and ``FakeGraphClient``, never JSON.
"""
