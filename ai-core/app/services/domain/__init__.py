"""Typed, async data-access layer for the SprintFlow domain.

One module per aggregate so dependent tasks import a focused surface and never
write their own queries:

- ``identity``       people and their Mattermost mapping
- ``channels``       channels, roles and channel roles
- ``sprints``        time-boxed sprints
- ``ceremonies``     ceremony types, scheduled ceremonies and their amendments
- ``standups``       daily progress entries
- ``escalations``    escalation tickets
- ``onboarding``     the onboarding delivery outbox
- ``reference_data`` idempotent seeding of the lookup tables

Every function is ``async``, accepts an optional ``session`` so callers can
compose a transaction, and otherwise opens, commits and closes its own.
"""
