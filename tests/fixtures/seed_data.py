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
    {"name": "New", "category": "backlog", "position": 1},
    {"name": "In Progress", "category": "active", "position": 2},
    {"name": "Resolved", "category": "done", "position": 3},
    {"name": "Feedback", "category": "active", "position": 4},
    {"name": "Closed", "category": "closed", "position": 5},
    {"name": "Rejected", "category": "closed", "position": 6},
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
