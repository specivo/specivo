"""Seed data for tests — trackers, statuses, priorities, roles.

Used by conftest.py to populate lookup tables before each test.
Populated once models exist (Milestone 1.4).
"""

TRACKERS = [
    {"name": "Bug", "position": 1},
    {"name": "Feature", "position": 2},
    {"name": "Task", "position": 3},
    {"name": "Support", "position": 4},
]

STATUSES = [
    {"name": "New", "is_closed": False, "position": 1},
    {"name": "In Progress", "is_closed": False, "position": 2},
    {"name": "Resolved", "is_closed": False, "position": 3},
    {"name": "Feedback", "is_closed": False, "position": 4},
    {"name": "Closed", "is_closed": True, "position": 5},
    {"name": "Rejected", "is_closed": True, "position": 6},
]

PRIORITIES = [
    {"name": "Low", "position": 1, "is_default": False},
    {"name": "Normal", "position": 2, "is_default": True},
    {"name": "High", "position": 3, "is_default": False},
    {"name": "Urgent", "position": 4, "is_default": False},
    {"name": "Immediate", "position": 5, "is_default": False},
]

ROLES = [
    {"name": "Manager", "position": 1, "permissions": ["*"]},
    {"name": "Developer", "position": 2, "permissions": ["add_issues", "edit_issues", "add_issue_notes"]},
    {"name": "Reporter", "position": 3, "permissions": ["add_issues", "add_issue_notes"]},
    {"name": "Agent", "position": 4, "permissions": ["add_issues", "edit_issues", "add_issue_notes"]},
]
